from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

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
    amap_api_key: str | None = None
    amap_base_url: str = "https://restapi.amap.com"
    amap_timeout_seconds: float = 15
    amap_max_retries: int = 1
    app_timezone: str = "Asia/Shanghai"
    trusted_proxy_cidrs: tuple[str, ...] = ()


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
        amap_api_key=os.getenv("AMAP_API_KEY") or None,
        amap_base_url=os.getenv("AMAP_BASE_URL", "https://restapi.amap.com").rstrip("/"),
        amap_timeout_seconds=float(os.getenv("AMAP_TIMEOUT_SECONDS", "15")),
        amap_max_retries=int(os.getenv("AMAP_MAX_RETRIES", "1")),
        app_timezone=os.getenv("APP_TIMEZONE", "Asia/Shanghai"),
        trusted_proxy_cidrs=trusted_proxy_cidrs,
    )
