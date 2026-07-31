from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import date
from pathlib import Path
from time import perf_counter
from typing import Any

from app.schemas.travel import (
    FlightSearchInput,
    FlyAIErrorCode,
    FlyAIExecutionDiagnostics,
    FlyAIResult,
    HotelSearchInput,
    PoiSearchInput,
    TrainSearchInput,
)

logger = logging.getLogger(__name__)

_ALLOWED_COMMANDS = frozenset(
    {
        "search-flight",
        "search-train",
        "search-hotel",
        "search-poi",
        "keyword-search",
        "ai-search",
    }
)
_RETRYABLE_ERRORS = frozenset(
    {
        FlyAIErrorCode.CLI_TIMEOUT,
        FlyAIErrorCode.RATE_LIMITED,
        FlyAIErrorCode.REMOTE_SERVICE_ERROR,
    }
)
_AUTH_MARKERS = (
    "authentication",
    "unauthorized",
    "invalid api key",
    "api key invalid",
    "forbidden",
    "status 401",
    "status 403",
)
_RATE_LIMIT_MARKERS = ("rate limit", "too many requests", "status 429", "http 429")
_REMOTE_ERROR_MARKERS = (
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "network error",
    "connection reset",
    "econnreset",
    "enotfound",
    "status 500",
    "status 502",
    "status 503",
    "status 504",
)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(api[_ -]?key|authorization|access[_ -]?token|secret)(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_STDERR_LIMIT = 1000

ProcessFactory = Callable[..., subprocess.Popen[str]]


class FlyAIClient:
    """Async facade over short-lived, UTF-8 FlyAI CLI subprocesses."""

    def __init__(
        self,
        cli_path: str | Path | None = None,
        *,
        default_timeout_seconds: float = 60,
        max_concurrency: int = 3,
        max_retries: int = 1,
        retry_delay_seconds: float = 0.25,
        process_factory: ProcessFactory | None = None,
    ) -> None:
        if default_timeout_seconds <= 0:
            raise ValueError("default_timeout_seconds must be positive")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds cannot be negative")

        self._cli_path = self.discover_cli(cli_path)
        self._default_timeout_seconds = default_timeout_seconds
        self._max_retries = max_retries
        self._retry_delay_seconds = retry_delay_seconds
        self._process_slots = threading.BoundedSemaphore(max_concurrency)
        self._process_factory = process_factory or subprocess.Popen

    @property
    def cli_path(self) -> str | None:
        """Absolute executable path cached when the client is created."""

        return self._cli_path

    @staticmethod
    def discover_cli(explicit_path: str | Path | None = None) -> str | None:
        """Resolve the Windows npm shim or platform executable once at startup."""

        candidates: tuple[str, ...]
        if explicit_path is not None:
            candidates = (str(explicit_path),)
        elif os.name == "nt":
            candidates = ("flyai.cmd", "flyai")
        else:
            candidates = ("flyai", "flyai.cmd")

        for candidate in candidates:
            direct_path = Path(candidate).expanduser()
            if direct_path.is_file():
                return str(direct_path.resolve())
            if resolved := shutil.which(candidate):
                return str(Path(resolved).resolve())
        return None

    async def execute(
        self,
        subcommand: str,
        arguments: Sequence[str] = (),
        *,
        timeout_seconds: float | None = None,
        allow_retry: bool = True,
    ) -> FlyAIResult:
        """Execute one allow-listed FlyAI command without invoking a shell."""

        started = perf_counter()
        validation_error = self._validate_execution(subcommand, arguments, timeout_seconds)
        intended_command = [self._cli_path or "flyai", subcommand, *map(str, arguments)]
        display_command = self._safe_command(intended_command)
        if validation_error:
            return self._failure(
                FlyAIErrorCode.INVALID_ARGUMENT,
                validation_error,
                display_command,
                started,
            )
        if self._cli_path is None:
            return self._failure(
                FlyAIErrorCode.CLI_NOT_FOUND,
                "FlyAI CLI was not found. Install it or configure FLYAI_CLI_PATH.",
                display_command,
                started,
            )

        timeout = timeout_seconds or self._default_timeout_seconds
        command = [self._cli_path, subcommand, *arguments]
        attempts = 1 + (self._max_retries if allow_retry else 0)
        result: FlyAIResult | None = None
        for attempt in range(attempts):
            result = await asyncio.to_thread(self._execute_with_slot, command, timeout)
            if result.success or result.error_code not in _RETRYABLE_ERRORS:
                break
            if attempt + 1 < attempts and self._retry_delay_seconds:
                await asyncio.sleep(self._retry_delay_seconds)

        assert result is not None
        return result.model_copy(update={"duration_ms": self._duration_ms(started)})

    async def search_flight(
        self,
        query: FlightSearchInput,
        *,
        timeout_seconds: float | None = None,
    ) -> FlyAIResult:
        arguments: list[str] = []
        self._add_argument(arguments, "--origin", query.origin)
        self._add_argument(arguments, "--destination", query.destination)
        self._add_argument(arguments, "--dep-date", query.departure_date)
        self._add_argument(arguments, "--back-date", query.return_date)
        self._add_argument(arguments, "--journey-type", query.journey_type)
        self._add_argument(arguments, "--seat-class-name", query.seat_classes)
        self._add_argument(arguments, "--transport-no", query.flight_numbers)
        self._add_argument(arguments, "--transfer-city", query.transfer_cities)
        self._add_argument(arguments, "--dep-hour-start", query.departure_hour_start)
        self._add_argument(arguments, "--dep-hour-end", query.departure_hour_end)
        self._add_argument(arguments, "--arr-hour-start", query.arrival_hour_start)
        self._add_argument(arguments, "--arr-hour-end", query.arrival_hour_end)
        self._add_argument(arguments, "--total-duration-hour", query.max_duration_hours)
        self._add_argument(arguments, "--max-price", query.max_price)
        self._add_argument(arguments, "--sort-type", query.sort_type)
        return await self.execute(
            "search-flight",
            arguments,
            timeout_seconds=timeout_seconds,
        )

    async def search_train(
        self,
        query: TrainSearchInput,
        *,
        timeout_seconds: float | None = None,
    ) -> FlyAIResult:
        arguments: list[str] = []
        self._add_argument(arguments, "--origin", query.origin)
        self._add_argument(arguments, "--destination", query.destination)
        self._add_argument(arguments, "--dep-date", query.departure_date)
        self._add_argument(arguments, "--back-date", query.return_date)
        self._add_argument(arguments, "--journey-type", query.journey_type)
        self._add_argument(arguments, "--seat-class-name", query.seat_classes)
        self._add_argument(arguments, "--transport-no", query.train_numbers)
        self._add_argument(arguments, "--transfer-city", query.transfer_cities)
        self._add_argument(arguments, "--dep-hour-start", query.departure_hour_start)
        self._add_argument(arguments, "--dep-hour-end", query.departure_hour_end)
        self._add_argument(arguments, "--arr-hour-start", query.arrival_hour_start)
        self._add_argument(arguments, "--arr-hour-end", query.arrival_hour_end)
        self._add_argument(arguments, "--total-duration-hour", query.max_duration_hours)
        self._add_argument(arguments, "--max-price", query.max_price)
        self._add_argument(arguments, "--sort-type", query.sort_type)
        return await self.execute(
            "search-train",
            arguments,
            timeout_seconds=timeout_seconds,
        )

    async def search_hotel(
        self,
        query: HotelSearchInput,
        *,
        timeout_seconds: float | None = None,
    ) -> FlyAIResult:
        arguments: list[str] = []
        self._add_argument(arguments, "--dest-name", query.destination)
        self._add_argument(arguments, "--key-words", query.keywords)
        self._add_argument(arguments, "--poi-name", query.nearby_poi)
        self._add_argument(arguments, "--hotel-types", query.hotel_types)
        self._add_argument(arguments, "--sort", query.sort)
        self._add_argument(arguments, "--check-in-date", query.check_in_date)
        self._add_argument(arguments, "--check-out-date", query.check_out_date)
        self._add_argument(arguments, "--hotel-stars", query.hotel_stars)
        self._add_argument(arguments, "--hotel-bed-types", query.bed_types)
        self._add_argument(arguments, "--max-price", query.max_price)
        return await self.execute(
            "search-hotel",
            arguments,
            timeout_seconds=timeout_seconds,
        )

    async def search_poi(
        self,
        query: PoiSearchInput,
        *,
        timeout_seconds: float | None = None,
    ) -> FlyAIResult:
        arguments: list[str] = []
        self._add_argument(arguments, "--city-name", query.city)
        self._add_argument(arguments, "--poi-level", query.poi_level)
        self._add_argument(arguments, "--keyword", query.keyword)
        self._add_argument(arguments, "--category", query.category)
        return await self.execute(
            "search-poi",
            arguments,
            timeout_seconds=timeout_seconds,
        )

    async def keyword_search(
        self,
        query: str,
        *,
        timeout_seconds: float | None = None,
    ) -> FlyAIResult:
        return await self._search_text("keyword-search", query, timeout_seconds)

    async def ai_search(
        self,
        query: str,
        *,
        timeout_seconds: float | None = None,
    ) -> FlyAIResult:
        return await self._search_text("ai-search", query, timeout_seconds)

    async def _search_text(
        self,
        subcommand: str,
        query: str,
        timeout_seconds: float | None,
    ) -> FlyAIResult:
        normalized = query.strip()
        if not normalized:
            started = perf_counter()
            return self._failure(
                FlyAIErrorCode.INVALID_ARGUMENT,
                "query cannot be empty",
                self._safe_command([self._cli_path or "flyai", subcommand, "--query", query]),
                started,
            )
        return await self.execute(
            subcommand,
            ["--query", normalized],
            timeout_seconds=timeout_seconds,
        )

    def _execute_with_slot(self, command: list[str], timeout_seconds: float) -> FlyAIResult:
        with self._process_slots:
            return self._execute_once(command, timeout_seconds)

    def _execute_once(self, command: list[str], timeout_seconds: float) -> FlyAIResult:
        started = perf_counter()
        popen_options: dict[str, Any] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "shell": False,
        }
        if os.name == "nt":
            popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_options["start_new_session"] = True

        try:
            process = self._process_factory(command, **popen_options)
            try:
                stdout, stderr = process.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                self._terminate_process_tree(process)
                result = self._failure(
                    FlyAIErrorCode.CLI_TIMEOUT,
                    f"FlyAI CLI timed out after {timeout_seconds:g} seconds.",
                    self._safe_command(command),
                    started,
                    diagnostics=FlyAIExecutionDiagnostics(
                        process_status="timeout",
                        parse_status="not_attempted",
                        business_status="unknown",
                    ),
                )
                self._log_execution(command, None, result, "")
                return result
        except FileNotFoundError:
            result = self._failure(
                FlyAIErrorCode.CLI_NOT_FOUND,
                "The cached FlyAI CLI executable no longer exists.",
                self._safe_command(command),
                started,
            )
            self._log_execution(command, None, result, "")
            return result
        except OSError as exc:
            result = self._failure(
                FlyAIErrorCode.UNKNOWN_ERROR,
                f"Could not start FlyAI CLI: {self._safe_excerpt(str(exc))}",
                self._safe_command(command),
                started,
            )
            self._log_execution(command, None, result, "")
            return result

        returncode = process.returncode if process.returncode is not None else -1
        normalized_stdout = stdout.lstrip("\ufeff").strip()
        if not normalized_stdout:
            diagnostics = FlyAIExecutionDiagnostics(
                process_status="success" if returncode == 0 else "failed",
                process_return_code=returncode,
                parse_status="empty",
                business_status="empty",
            )
            result = self._failure(
                (
                    FlyAIErrorCode.EMPTY_RESULT
                    if returncode == 0
                    else self._classify_exit_error(stdout, stderr)
                ),
                (
                    "FlyAI CLI returned an empty stdout response."
                    if returncode == 0
                    else (
                        self._safe_excerpt(stderr)
                        or f"FlyAI CLI exited with status {returncode}."
                    )
                ),
                self._safe_command(command),
                started,
                diagnostics=diagnostics,
            )
            self._log_execution(command, returncode, result, stderr)
            return result

        try:
            data = json.loads(normalized_stdout)
        except json.JSONDecodeError:
            diagnostics = FlyAIExecutionDiagnostics(
                process_status="success" if returncode == 0 else "failed",
                process_return_code=returncode,
                parse_status="invalid",
                business_status="invalid",
            )
            result = self._failure(
                (
                    FlyAIErrorCode.INVALID_JSON
                    if returncode == 0
                    else self._classify_exit_error(stdout, stderr)
                ),
                (
                    "FlyAI CLI stdout was not valid JSON."
                    if returncode == 0
                    else (
                        self._safe_excerpt(stderr)
                        or f"FlyAI CLI exited with status {returncode}."
                    )
                ),
                self._safe_command(command),
                started,
                diagnostics=diagnostics,
            )
            self._log_execution(command, returncode, result, stderr)
            return result

        if data is None:
            diagnostics = FlyAIExecutionDiagnostics(
                process_status="success" if returncode == 0 else "failed",
                process_return_code=returncode,
                parse_status="empty",
                business_status="empty",
            )
            result = self._failure(
                FlyAIErrorCode.EMPTY_RESULT,
                "FlyAI CLI returned a null JSON result.",
                self._safe_command(command),
                started,
                diagnostics=diagnostics,
            )
            self._log_execution(command, returncode, result, stderr)
            return result

        provider_status = self._provider_status(data)
        business_status = "usable" if self._has_usable_data(data) else "empty"
        diagnostics = FlyAIExecutionDiagnostics(
            process_status="success" if returncode == 0 else "failed",
            process_return_code=returncode,
            provider_status=provider_status,
            parse_status="success",
            business_status=business_status,
        )
        if provider_status == "failed":
            result = self._failure(
                FlyAIErrorCode.REMOTE_SERVICE_ERROR,
                "FlyAI provider returned an unsuccessful response.",
                self._safe_command(command),
                started,
                diagnostics=diagnostics,
            )
            self._log_execution(command, returncode, result, stderr)
            return result

        # Some FlyAI commands write usable JSON and still return a non-zero process code.
        # Preserve that business result while retaining the failed process verdict.
        if returncode != 0 and business_status != "usable":
            result = self._failure(
                self._classify_exit_error(stdout, stderr),
                self._safe_excerpt(stderr)
                or f"FlyAI CLI exited with status {returncode}.",
                self._safe_command(command),
                started,
                diagnostics=diagnostics,
            )
            self._log_execution(command, returncode, result, stderr)
            return result

        result = FlyAIResult(
            success=True,
            command=self._safe_command(command),
            data=self._sanitize_json(data),
            duration_ms=self._duration_ms(started),
            diagnostics=diagnostics,
        )
        self._log_execution(command, returncode, result, stderr)
        return result

    @staticmethod
    def _validate_execution(
        subcommand: str,
        arguments: Sequence[str],
        timeout_seconds: float | None,
    ) -> str | None:
        if subcommand not in _ALLOWED_COMMANDS:
            return f"unsupported FlyAI command: {subcommand}"
        if isinstance(arguments, (str, bytes)):
            return "arguments must be a sequence of separate strings"
        if any(not isinstance(argument, str) or "\x00" in argument for argument in arguments):
            return "each CLI argument must be a string without null bytes"
        if timeout_seconds is not None and timeout_seconds <= 0:
            return "timeout_seconds must be positive"
        return None

    @staticmethod
    def _add_argument(
        arguments: list[str],
        flag: str,
        value: object | None,
    ) -> None:
        if value is None or value == ():
            return
        if isinstance(value, date):
            rendered = value.isoformat()
        elif isinstance(value, tuple):
            rendered = ",".join(str(item) for item in value)
        else:
            rendered = str(value)
        arguments.extend((flag, rendered))

    @staticmethod
    def _classify_exit_error(stdout: str, stderr: str) -> FlyAIErrorCode:
        combined = f"{stderr}\n{stdout}".casefold()
        if any(marker in combined for marker in _AUTH_MARKERS):
            return FlyAIErrorCode.AUTH_ERROR
        if any(marker in combined for marker in _RATE_LIMIT_MARKERS):
            return FlyAIErrorCode.RATE_LIMITED
        if any(marker in combined for marker in _REMOTE_ERROR_MARKERS):
            return FlyAIErrorCode.REMOTE_SERVICE_ERROR
        return FlyAIErrorCode.CLI_EXIT_ERROR

    @staticmethod
    def _provider_status(data: Any) -> str:
        if not isinstance(data, dict):
            return "unknown"
        success = data.get("success")
        if success is True:
            return "success"
        if success is False:
            return "failed"
        status = data.get("status")
        if isinstance(status, str):
            normalized = status.strip().casefold()
            if normalized in {"success", "succeeded", "ok", "completed"}:
                return "success"
            if normalized in {"failed", "failure", "error", "rejected"}:
                return "failed"
        return "unknown"

    @classmethod
    def _has_usable_data(cls, value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, tuple)):
            return any(cls._has_usable_data(item) for item in value)
        if isinstance(value, dict):
            business_values = [
                item
                for key, item in value.items()
                if str(key).casefold()
                not in {"success", "status", "code", "message", "error", "errorcode"}
            ]
            return any(cls._has_usable_data(item) for item in business_values)
        return True

    @classmethod
    def _safe_command(cls, command: Sequence[str]) -> list[str]:
        return [cls._redact_sensitive(part) for part in command]

    @classmethod
    def _safe_excerpt(cls, value: str) -> str:
        normalized = cls._redact_sensitive(value).replace("\r", " ").replace("\n", " ")
        return normalized[:_STDERR_LIMIT]

    @staticmethod
    def _redact_sensitive(value: str) -> str:
        configured_key = os.getenv("FLYAI_API_KEY")
        if configured_key:
            value = value.replace(configured_key, "[REDACTED]")
        value = _SENSITIVE_ASSIGNMENT.sub(r"\1\2[REDACTED]", value)
        return _BEARER_TOKEN.sub("Bearer [REDACTED]", value)

    @classmethod
    def _sanitize_json(cls, value: Any) -> Any:
        if isinstance(value, dict):
            sanitized: dict[Any, Any] = {}
            for key, item in value.items():
                normalized_key = re.sub(r"[^a-z0-9]", "", str(key).casefold())
                if any(
                    marker in normalized_key
                    for marker in ("apikey", "token", "secret", "authorization", "credential")
                ):
                    sanitized[key] = "[REDACTED]"
                else:
                    sanitized[key] = cls._sanitize_json(item)
            return sanitized
        if isinstance(value, list):
            return [cls._sanitize_json(item) for item in value]
        if isinstance(value, str):
            return cls._redact_sensitive(value)
        return value

    @staticmethod
    def _duration_ms(started: float) -> int:
        return max(0, round((perf_counter() - started) * 1000))

    @classmethod
    def _failure(
        cls,
        error_code: FlyAIErrorCode,
        message: str,
        command: list[str],
        started: float,
        *,
        diagnostics: FlyAIExecutionDiagnostics | None = None,
    ) -> FlyAIResult:
        return FlyAIResult(
            success=False,
            command=command,
            error_code=error_code,
            error_message=cls._safe_excerpt(message),
            duration_ms=cls._duration_ms(started),
            diagnostics=diagnostics or FlyAIExecutionDiagnostics(),
        )

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            with suppress(OSError, subprocess.SubprocessError):
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                    shell=False,
                )
        else:
            with suppress(OSError):
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        with suppress(OSError):
            process.kill()
        with suppress(OSError, subprocess.SubprocessError):
            process.wait(timeout=5)

    @classmethod
    def _log_execution(
        cls,
        command: Sequence[str],
        returncode: int | None,
        result: FlyAIResult,
        stderr: str,
    ) -> None:
        flags = [part for part in command[2:] if part.startswith("--")]
        logger.info(
            "flyai_cli command=%s flags=%s returncode=%s process_status=%s "
            "provider_status=%s parse_status=%s business_status=%s duration_ms=%s "
            "error_code=%s stderr=%s",
            command[1] if len(command) > 1 else "unknown",
            ",".join(flags),
            returncode,
            result.diagnostics.process_status,
            result.diagnostics.provider_status,
            result.diagnostics.parse_status,
            result.diagnostics.business_status,
            result.duration_ms,
            result.error_code.value if result.error_code else None,
            cls._safe_excerpt(stderr),
        )
