from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[4]


def _resolve_project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def configure_no_proxy() -> None:
    """Merge project-specific direct-connect hosts into standard proxy bypass variables."""
    project_hosts = os.getenv("TOUR_ASSISTANT_NO_PROXY_HOSTS", "")
    if not project_hosts.strip():
        return

    values = (
        os.getenv("NO_PROXY", ""),
        os.getenv("no_proxy", ""),
        project_hosts,
    )
    merged: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in value.split(","):
            host = item.strip()
            normalized = host.casefold()
            if host and normalized not in seen:
                seen.add(normalized)
                merged.append(host)

    no_proxy = ",".join(merged)
    os.environ["NO_PROXY"] = no_proxy
    os.environ["no_proxy"] = no_proxy


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str
    model_config_path: Path
    cors_origins: tuple[str, ...]
    log_level: str
    database_url: str | None = None
    flyai_cli_path: str | None = None
    flyai_timeout_seconds: float = 60
    flyai_max_concurrency: int = 3
    max_tool_rounds: int = 5
    tool_execution_timeout_seconds: float = 130
    amap_api_key: str | None = None
    amap_base_url: str = "https://restapi.amap.com"
    amap_timeout_seconds: float = 15
    amap_max_retries: int = 1
    amap_min_request_interval_seconds: float = 0.2
    app_timezone: str = "Asia/Shanghai"
    trusted_proxy_cidrs: tuple[str, ...] = ()
    trip_planner_enabled: bool = True
    trip_planner_max_days: int = 5
    trip_planner_model_timeout_seconds: float = 45
    trip_planner_request_extraction_timeout_seconds: float = 30
    xhs_mcp_transport: Literal["streamable-http", "stdio"] = "streamable-http"
    xhs_mcp_url: str = "http://127.0.0.1:8765/mcp"
    xhs_mcp_auth_token: str | None = field(default=None, repr=False)
    xhs_mcp_timeout_seconds: float = 75
    xhs_mcp_stdio_command: str = field(default_factory=lambda: sys.executable)
    xhs_mcp_stdio_args: tuple[str, ...] = (
        "-m",
        "xhs_read_mcp",
        "--transport",
        "stdio",
        "--headed",
    )
    xhs_mcp_stdio_cwd: Path | None = None
    xhs_min_post_content_chars: int = 200
    xhs_detail_candidate_limit: int = 5
    xhs_login_poll_seconds: float = 2
    xhs_sse_heartbeat_seconds: float = 15

    def __post_init__(self) -> None:
        positive_values = {
            "trip_planner_max_days": self.trip_planner_max_days,
            "trip_planner_model_timeout_seconds": self.trip_planner_model_timeout_seconds,
            "trip_planner_request_extraction_timeout_seconds": (
                self.trip_planner_request_extraction_timeout_seconds
            ),
            "xhs_mcp_timeout_seconds": self.xhs_mcp_timeout_seconds,
            "xhs_min_post_content_chars": self.xhs_min_post_content_chars,
            "xhs_detail_candidate_limit": self.xhs_detail_candidate_limit,
            "xhs_login_poll_seconds": self.xhs_login_poll_seconds,
            "xhs_sse_heartbeat_seconds": self.xhs_sse_heartbeat_seconds,
        }
        if any(value <= 0 for value in positive_values.values()):
            raise ValueError("Trip planner limits and timeouts must be positive.")
        if self.amap_min_request_interval_seconds < 0:
            raise ValueError("amap_min_request_interval_seconds cannot be negative.")
        if self.xhs_mcp_transport not in {"streamable-http", "stdio"}:
            raise ValueError("xhs_mcp_transport must be 'streamable-http' or 'stdio'.")
        if self.trip_planner_enabled:
            if self.xhs_mcp_transport == "streamable-http" and not self.xhs_mcp_url.startswith(
                ("http://", "https://")
            ):
                raise ValueError("xhs_mcp_url must use HTTP or HTTPS.")
            if self.xhs_mcp_transport == "stdio" and not self.xhs_mcp_stdio_command.strip():
                raise ValueError("xhs_mcp_stdio_command cannot be empty in stdio mode.")


def get_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    configure_no_proxy()
    origins = tuple(
        origin.strip()
        for origin in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
        if origin.strip()
    )
    trusted_proxy_cidrs = tuple(
        item.strip() for item in os.getenv("TRUSTED_PROXY_CIDRS", "").split(",") if item.strip()
    )
    return Settings(
        app_name="Tour Assistant API",
        model_config_path=_resolve_project_path(
            os.getenv("MODEL_CONFIG_PATH", "config/models.yaml")
        ),
        cors_origins=origins,
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        database_url=os.getenv("DATABASE_URL") or None,
        flyai_cli_path=os.getenv("FLYAI_CLI_PATH") or None,
        flyai_timeout_seconds=float(os.getenv("FLYAI_TIMEOUT_SECONDS", "60")),
        flyai_max_concurrency=int(os.getenv("FLYAI_MAX_CONCURRENCY", "3")),
        max_tool_rounds=int(os.getenv("MAX_TOOL_ROUNDS", "5")),
        tool_execution_timeout_seconds=float(os.getenv("TOOL_EXECUTION_TIMEOUT_SECONDS", "130")),
        amap_api_key=os.getenv("AMAP_API_KEY") or None,
        amap_base_url=os.getenv("AMAP_BASE_URL", "https://restapi.amap.com").rstrip("/"),
        amap_timeout_seconds=float(os.getenv("AMAP_TIMEOUT_SECONDS", "15")),
        amap_max_retries=int(os.getenv("AMAP_MAX_RETRIES", "1")),
        amap_min_request_interval_seconds=float(
            os.getenv("AMAP_MIN_REQUEST_INTERVAL_SECONDS", "0.2")
        ),
        app_timezone=os.getenv("APP_TIMEZONE", "Asia/Shanghai"),
        trusted_proxy_cidrs=trusted_proxy_cidrs,
        trip_planner_enabled=_environment_bool("TRIP_PLANNER_ENABLED", True),
        trip_planner_max_days=int(os.getenv("TRIP_PLANNER_MAX_DAYS", "5")),
        trip_planner_model_timeout_seconds=float(
            os.getenv("TRIP_PLANNER_MODEL_TIMEOUT_SECONDS", "45")
        ),
        trip_planner_request_extraction_timeout_seconds=float(
            os.getenv("TRIP_PLANNER_REQUEST_EXTRACTION_TIMEOUT_SECONDS", "30")
        ),
        xhs_mcp_transport=(os.getenv("XHS_MCP_TRANSPORT", "streamable-http").strip().casefold()),
        xhs_mcp_url=(os.getenv("XHS_MCP_URL") or "http://127.0.0.1:8765/mcp").rstrip("/"),
        xhs_mcp_auth_token=os.getenv("XHS_MCP_AUTH_TOKEN") or None,
        xhs_mcp_timeout_seconds=float(os.getenv("XHS_MCP_TIMEOUT_SECONDS", "75")),
        xhs_mcp_stdio_command=(os.getenv("XHS_MCP_STDIO_COMMAND") or sys.executable).strip(),
        xhs_mcp_stdio_args=_environment_json_string_tuple(
            "XHS_MCP_STDIO_ARGS",
            ("-m", "xhs_read_mcp", "--transport", "stdio", "--headed"),
        ),
        xhs_mcp_stdio_cwd=_environment_optional_path("XHS_MCP_STDIO_CWD"),
        xhs_min_post_content_chars=int(os.getenv("XHS_MIN_POST_CONTENT_CHARS", "200")),
        xhs_detail_candidate_limit=int(os.getenv("XHS_DETAIL_CANDIDATE_LIMIT", "5")),
        xhs_login_poll_seconds=float(os.getenv("XHS_LOGIN_POLL_SECONDS", "2")),
        xhs_sse_heartbeat_seconds=float(os.getenv("XHS_SSE_HEARTBEAT_SECONDS", "15")),
    )


def _environment_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value.")


def _environment_json_string_tuple(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be a JSON string array.") from exc
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) or not item for item in parsed
    ):
        raise ValueError(f"{name} must be a JSON array of non-empty strings.")
    return tuple(parsed)


def _environment_optional_path(name: str) -> Path | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    return _resolve_project_path(raw.strip())
