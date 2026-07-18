from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from time import perf_counter
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ValidationError

from app.schemas.tool_execution import (
    ToolCallEvent,
    ToolError,
    ToolResult,
    ToolResultEvent,
    ToolResultMetadata,
)
from app.services.tool_call_log_service import (
    ToolCallLogEntry,
    ToolCallLogWriter,
    ToolCallQualityUpdate,
)

logger = logging.getLogger(__name__)

_SENSITIVE_KEY = re.compile(
    r"(?:api.?key|authorization|base.?url|command|cookie|credential|database.?url|directory|"
    r"file|header|password|path|proxy|secret|token|client.?ip|ip.?address)",
    re.IGNORECASE,
)
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_ASSIGNMENT_SECRET = re.compile(
    r"(?i)(api[_ -]?key|authorization|access[_ -]?token|password|secret)(\s*[:=]\s*)([^\s,;]+)"
)
_WINDOWS_PATH = re.compile(r"^[a-zA-Z]:[\\/]")

_TOOL_LABELS = {
    "search_flight": "查询航班",
    "search_train": "查询火车",
    "search_hotel": "查询酒店",
    "search_poi": "查询景点",
    "amap_get_current_city": "推测当前城市",
    "amap_search_places": "搜索地点",
    "amap_plan_route": "规划路线",
    "amap_travel_time_matrix": "计算行程时间",
    "amap_get_weather": "查询天气",
}

_CANONICAL_ERROR_CODES = {
    "CLI_TIMEOUT": "PROVIDER_TIMEOUT",
    "TIMEOUT": "PROVIDER_TIMEOUT",
    "RATE_LIMITED": "PROVIDER_RATE_LIMITED",
    "EMPTY_RESULT": "EMPTY_RESULT",
    "CLI_NOT_FOUND": "PROVIDER_UNAVAILABLE",
    "AUTH_ERROR": "PROVIDER_UNAVAILABLE",
    "CONFIGURATION_ERROR": "PROVIDER_UNAVAILABLE",
}


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    conversation_id: uuid.UUID
    assistant_message_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class PreparedToolCall:
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any] | None
    public_arguments: dict[str, Any]
    event: ToolCallEvent
    preflight_error: ToolError | None = None


@dataclass(frozen=True, slots=True)
class ToolExecutionOutcome:
    message: ToolMessage
    result: ToolResult
    event: ToolResultEvent


def tool_display_name(tool_name: str) -> str:
    return _TOOL_LABELS.get(tool_name, "执行工具")


def redact_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _redact_value(value, key=str(key)) for key, value in values.items()}


def _redact_value(value: Any, *, key: str = "") -> Any:
    if key and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return redact_mapping(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_redact_value(item) for item in value]
    if isinstance(value, BaseModel):
        return _redact_value(value.model_dump(mode="json"))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, str):
        if _WINDOWS_PATH.match(value.strip()):
            return "[REDACTED]"
        redacted = _ASSIGNMENT_SECRET.sub(r"\1\2[REDACTED]", value)
        return _BEARER_TOKEN.sub("Bearer [REDACTED]", redacted)
    if value is None or isinstance(value, (bool, int, float, date, Decimal, uuid.UUID)):
        return value
    return "[UNSUPPORTED_VALUE]"


class ToolExecutor:
    def __init__(
        self,
        tools: Sequence[BaseTool],
        *,
        timeout_seconds: float = 130,
        log_writer: ToolCallLogWriter | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._tools = {tool.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("Tool names must be unique")
        self._timeout_seconds = timeout_seconds
        self._log_writer = log_writer

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def prepare_calls(
        self,
        tool_calls: Sequence[Mapping[str, Any]],
        *,
        round_index: int,
    ) -> list[PreparedToolCall]:
        return [
            self._prepare_call(tool_call, round_index=round_index, call_index=index)
            for index, tool_call in enumerate(tool_calls)
        ]

    async def execute_many(
        self,
        calls: Sequence[PreparedToolCall],
        context: ToolExecutionContext | None = None,
    ) -> list[ToolExecutionOutcome]:
        """Execute one decision round concurrently while preserving request order."""

        return list(
            await asyncio.gather(*(self._execute_one(call, context=context) for call in calls))
        )

    async def record_data_quality(
        self,
        event: ToolResultEvent,
        context: ToolExecutionContext | None,
    ) -> None:
        """Persist a post-normalization verdict when the configured writer supports it."""

        if self._log_writer is None or context is None or event.data_status is None:
            return
        update_quality = getattr(self._log_writer, "update_data_quality", None)
        if update_quality is None:
            return
        try:
            await update_quality(
                ToolCallQualityUpdate(
                    conversation_id=context.conversation_id,
                    assistant_message_id=context.assistant_message_id,
                    tool_call_id=event.tool_call_id,
                    data_status=event.data_status,
                    provider_item_count=event.provider_item_count or 0,
                    normalized_item_count=event.normalized_item_count or 0,
                    rejected_item_count=event.rejected_item_count or 0,
                    schema_version=event.schema_version or "unknown",
                    result_summary=event.summary,
                    error_code=event.error_code,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Could not persist normalized tool quality tool=%s exception_type=%s",
                event.tool_name,
                type(exc).__name__,
            )

    def _prepare_call(
        self,
        tool_call: Mapping[str, Any],
        *,
        round_index: int,
        call_index: int,
    ) -> PreparedToolCall:
        raw_id = tool_call.get("id")
        call_id = raw_id.strip() if isinstance(raw_id, str) else ""
        preflight_error: ToolError | None = None
        if not call_id:
            call_id = f"invalid_call_{round_index}_{call_index}"
            preflight_error = ToolError(
                code="TOOL_CALL_ID_MISSING",
                message="模型返回的工具调用缺少有效标识。",
            )

        raw_name = tool_call.get("name")
        tool_name = raw_name.strip() if isinstance(raw_name, str) else ""
        if not tool_name:
            tool_name = "unknown_tool"
            preflight_error = preflight_error or ToolError(
                code="TOOL_NAME_INVALID",
                message="模型返回的工具名称无效。",
            )

        raw_arguments = tool_call.get("args")
        arguments = dict(raw_arguments) if isinstance(raw_arguments, Mapping) else None
        if arguments is None:
            preflight_error = preflight_error or ToolError(
                code="TOOL_ARGUMENT_INVALID",
                message="工具参数必须是 JSON 对象。",
            )
        public_arguments = redact_mapping(arguments or {})

        return PreparedToolCall(
            tool_call_id=call_id,
            tool_name=tool_name,
            arguments=arguments,
            public_arguments=public_arguments,
            event=ToolCallEvent(
                tool_call_id=call_id,
                tool_name=tool_name,
                display_name=f"正在{tool_display_name(tool_name)}",
                arguments=public_arguments,
            ),
            preflight_error=preflight_error,
        )

    async def _execute_one(
        self,
        call: PreparedToolCall,
        *,
        context: ToolExecutionContext | None,
    ) -> ToolExecutionOutcome:
        started = perf_counter()
        provider = _provider_for(call.tool_name)
        result: ToolResult

        try:
            if call.preflight_error is not None:
                result = self._failure_result(call, call.preflight_error, provider, started)
            elif (tool := self._tools.get(call.tool_name)) is None:
                result = self._failure_result(
                    call,
                    ToolError(
                        code="TOOL_NOT_FOUND",
                        message="请求的工具不存在或当前不可用。",
                    ),
                    provider,
                    started,
                )
            else:
                assert call.arguments is not None
                self._validate_arguments(tool, call.arguments)
                raw_result = await asyncio.wait_for(
                    tool.ainvoke(call.arguments),
                    timeout=self._timeout_seconds,
                )
                try:
                    result = self._normalize_result(call, raw_result, provider, started)
                except ValidationError:
                    result = self._failure_result(
                        call,
                        ToolError(
                            code="TOOL_RESULT_INVALID",
                            message=f"{tool_display_name(call.tool_name)}返回了无效结果。",
                        ),
                        provider,
                        started,
                    )
        except asyncio.CancelledError:
            raise
        except ValidationError:
            result = self._failure_result(
                call,
                ToolError(
                    code="TOOL_ARGUMENT_INVALID",
                    message="工具参数不完整或格式不正确。",
                ),
                provider,
                started,
            )
        except TimeoutError:
            result = self._failure_result(
                call,
                ToolError(
                    code="PROVIDER_TIMEOUT",
                    message=f"{tool_display_name(call.tool_name)}服务响应超时，请稍后重试。",
                    retryable=True,
                ),
                provider,
                started,
            )
        except Exception as exc:
            logger.warning(
                "Tool execution failed tool=%s exception_type=%s",
                call.tool_name,
                type(exc).__name__,
            )
            result = self._failure_result(
                call,
                ToolError(
                    code="TOOL_EXECUTION_FAILED",
                    message=f"{tool_display_name(call.tool_name)}暂时不可用，请稍后重试。",
                    retryable=True,
                ),
                provider,
                started,
            )

        outcome = self._outcome(call, result)
        await self._record_safely(call, outcome.result, outcome.event.summary, context)
        return outcome

    @staticmethod
    def _validate_arguments(tool: BaseTool, arguments: dict[str, Any]) -> None:
        schema = tool.args_schema
        if isinstance(schema, type) and issubclass(schema, BaseModel):
            schema.model_validate(arguments)

    def _normalize_result(
        self,
        call: PreparedToolCall,
        raw_result: Any,
        fallback_provider: str,
        started: float,
    ) -> ToolResult:
        if isinstance(raw_result, BaseModel):
            raw_result = raw_result.model_dump(mode="json")

        if raw_result is None:
            return self._failure_result(
                call,
                ToolError(
                    code="EMPTY_RESULT",
                    message=f"{tool_display_name(call.tool_name)}没有返回可用数据。",
                ),
                fallback_provider,
                started,
            )

        if not isinstance(raw_result, Mapping) or "success" not in raw_result:
            return ToolResult(
                success=True,
                tool_name=call.tool_name,
                data=_redact_value(raw_result),
                metadata=ToolResultMetadata(
                    provider=fallback_provider,
                    duration_ms=_duration_ms(started),
                ),
            )

        payload = dict(raw_result)
        provider = _safe_provider(payload.get("provider"), fallback_provider)
        duration_ms = _safe_duration(payload.get("duration_ms"), started)
        queried_at = payload.get("finished_at") or datetime.now(UTC)
        metadata = ToolResultMetadata.model_validate(
            {
                "provider": provider,
                "duration_ms": duration_ms,
                "queried_at": queried_at,
            }
        )

        if payload.get("success") is True:
            data = payload.get("data")
            if data is None:
                return ToolResult(
                    success=False,
                    tool_name=call.tool_name,
                    error=ToolError(
                        code="EMPTY_RESULT",
                        message=f"{tool_display_name(call.tool_name)}没有返回可用数据。",
                    ),
                    metadata=metadata,
                )
            return ToolResult(
                success=True,
                tool_name=call.tool_name,
                data=_redact_value(data),
                metadata=metadata,
            )

        raw_code = payload.get("error_code")
        code = _canonical_error_code(raw_code)
        provider_error_code = _safe_provider_error_code(payload.get("provider_error_code"))
        return ToolResult(
            success=False,
            tool_name=call.tool_name,
            error=ToolError(
                code=code,
                message=_safe_error_message(call.tool_name, code),
                retryable=code in {"PROVIDER_TIMEOUT", "PROVIDER_RATE_LIMITED", "PROVIDER_ERROR"},
                provider_error_code=provider_error_code,
            ),
            metadata=metadata,
        )

    @staticmethod
    def _failure_result(
        call: PreparedToolCall,
        error: ToolError,
        provider: str,
        started: float,
    ) -> ToolResult:
        return ToolResult(
            success=False,
            tool_name=call.tool_name,
            error=error,
            metadata=ToolResultMetadata(
                provider=provider,
                duration_ms=_duration_ms(started),
            ),
        )

    @staticmethod
    def _outcome(call: PreparedToolCall, result: ToolResult) -> ToolExecutionOutcome:
        try:
            content = result.model_dump_json()
        except Exception as exc:
            logger.warning(
                "Could not serialize tool result tool=%s exception_type=%s",
                call.tool_name,
                type(exc).__name__,
            )
            result = ToolResult(
                success=False,
                tool_name=call.tool_name,
                error=ToolError(
                    code="TOOL_RESULT_INVALID",
                    message=f"{tool_display_name(call.tool_name)}返回了无法处理的结果。",
                ),
                metadata=result.metadata,
            )
            content = result.model_dump_json()
        summary = (
            f"已完成{tool_display_name(call.tool_name)}。"
            if result.success
            else result.error.message
        )
        error_code = result.error.code if result.error else None
        provider_error_code = result.error.provider_error_code if result.error else None
        return ToolExecutionOutcome(
            message=ToolMessage(
                content=content,
                tool_call_id=call.tool_call_id,
                name=call.tool_name,
            ),
            result=result,
            event=ToolResultEvent(
                tool_call_id=call.tool_call_id,
                tool_name=call.tool_name,
                success=result.success,
                summary=summary,
                duration_ms=result.metadata.duration_ms,
                error_code=error_code,
                provider_error_code=provider_error_code,
            ),
        )

    async def _record_safely(
        self,
        call: PreparedToolCall,
        result: ToolResult,
        summary: str,
        context: ToolExecutionContext | None,
    ) -> None:
        if self._log_writer is None or context is None:
            return
        try:
            await self._log_writer.record(
                ToolCallLogEntry(
                    conversation_id=context.conversation_id,
                    assistant_message_id=context.assistant_message_id,
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    provider=result.metadata.provider,
                    arguments=call.public_arguments,
                    status="success" if result.success else "failed",
                    result_summary=summary,
                    error_code=result.error.code if result.error else None,
                    duration_ms=result.metadata.duration_ms,
                    provider_error_code=(
                        result.error.provider_error_code if result.error else None
                    ),
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Could not persist tool call log tool=%s exception_type=%s",
                call.tool_name,
                type(exc).__name__,
            )


def _duration_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1000))


def _safe_duration(value: Any, started: float) -> int:
    return value if isinstance(value, int) and value >= 0 else _duration_ms(started)


def _safe_provider(value: Any, fallback: str) -> str:
    if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9._-]{1,50}", value):
        return value
    return fallback


def _safe_provider_error_code(value: Any) -> str | None:
    if isinstance(value, (str, int)):
        normalized = str(value).strip()
        if normalized and re.fullmatch(r"[a-zA-Z0-9._-]{1,100}", normalized):
            return normalized
    return None


def _provider_for(tool_name: str) -> str:
    if tool_name.startswith("amap_"):
        return "amap"
    if tool_name.startswith("search_"):
        return "flyai"
    return "unknown"


def _canonical_error_code(value: Any) -> str:
    raw = value.value if isinstance(value, Enum) else value
    if not isinstance(raw, str) or not raw:
        return "PROVIDER_ERROR"
    return _CANONICAL_ERROR_CODES.get(raw.upper(), "PROVIDER_ERROR")


def _safe_error_message(tool_name: str, code: str) -> str:
    label = tool_display_name(tool_name)
    if code == "PROVIDER_TIMEOUT":
        return f"{label}服务响应超时，请稍后重试。"
    if code == "PROVIDER_RATE_LIMITED":
        return f"{label}服务请求过于频繁，请稍后重试。"
    if code == "EMPTY_RESULT":
        return f"{label}没有返回可用数据。"
    if code == "PROVIDER_UNAVAILABLE":
        return f"{label}服务当前不可用。"
    return f"{label}服务暂时不可用，请稍后重试。"


__all__ = [
    "PreparedToolCall",
    "ToolExecutionContext",
    "ToolExecutionOutcome",
    "ToolExecutor",
    "redact_mapping",
    "tool_display_name",
]
