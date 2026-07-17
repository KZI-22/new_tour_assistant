from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import yaml
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.schemas.chat import ModelInfo, ModelListResponse


class ModelRegistryError(RuntimeError):
    """Base error for model configuration and lookup failures."""


class UnknownModelError(ModelRegistryError):
    """Raised when a requested model id is not enabled."""


class UnavailableModelError(ModelRegistryError):
    """Raised when a model is enabled but lacks required runtime configuration."""


class ModelEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$", max_length=100)
    display_name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=300)
    provider: str = Field(min_length=1, max_length=50)
    model: str = Field(min_length=1, max_length=200)
    api_key_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    base_url: str | None = None
    base_url_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    enabled: bool = True
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    timeout_seconds: float = Field(default=60, gt=0, le=600)
    max_retries: int = Field(default=2, ge=0, le=10)
    parameters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return value

    @model_validator(mode="after")
    def validate_base_url_source(self) -> ModelEntry:
        if self.base_url and self.base_url_env:
            raise ValueError("Set only one of base_url and base_url_env")
        return self


class ModelCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_model: str | None = None
    router_model: str | None = None
    models: list[ModelEntry]

    @model_validator(mode="after")
    def validate_catalog(self) -> ModelCatalog:
        ids = [item.id for item in self.models]
        if len(ids) != len(set(ids)):
            raise ValueError("Model ids must be unique")
        if self.default_model is not None and self.default_model not in ids:
            raise ValueError("default_model must reference an item in models")
        enabled_ids = {item.id for item in self.models if item.enabled}
        if self.router_model is not None and self.router_model not in enabled_ids:
            raise ValueError("router_model must reference an enabled item in models")
        return self


class ModelRegistry:
    """Loads model definitions and creates LangChain chat model instances."""

    def __init__(self, config_path: Path) -> None:
        self._config_path = config_path
        self._lock = threading.RLock()
        self._catalog: ModelCatalog | None = None
        self._loaded_mtime_ns: int | None = None

    def _load_if_changed(self) -> ModelCatalog:
        with self._lock:
            try:
                mtime_ns = self._config_path.stat().st_mtime_ns
            except FileNotFoundError as exc:
                raise ModelRegistryError(
                    f"Model configuration file not found: {self._config_path}"
                ) from exc

            if self._catalog is not None and mtime_ns == self._loaded_mtime_ns:
                return self._catalog

            try:
                raw = yaml.safe_load(self._config_path.read_text(encoding="utf-8")) or {}
                catalog = ModelCatalog.model_validate(raw)
            except (OSError, yaml.YAMLError, ValidationError) as exc:
                raise ModelRegistryError(f"Invalid model configuration: {exc}") from exc

            self._catalog = catalog
            self._loaded_mtime_ns = mtime_ns
            return catalog

    @staticmethod
    def _availability(entry: ModelEntry) -> tuple[bool, str | None]:
        if entry.api_key_env and not os.getenv(entry.api_key_env):
            return False, f"Missing environment variable: {entry.api_key_env}"
        if entry.base_url_env and not os.getenv(entry.base_url_env):
            return False, f"Missing environment variable: {entry.base_url_env}"
        return True, None

    def list_models(self) -> ModelListResponse:
        catalog = self._load_if_changed()
        enabled = [item for item in catalog.models if item.enabled]
        models: list[ModelInfo] = []
        for item in enabled:
            available, reason = self._availability(item)
            models.append(
                ModelInfo(
                    id=item.id,
                    display_name=item.display_name,
                    description=item.description,
                    provider=item.provider,
                    available=available,
                    unavailable_reason=reason,
                )
            )

        default_model = catalog.default_model
        if default_model not in {item.id for item in enabled}:
            default_model = models[0].id if models else None
        return ModelListResponse(default_model=default_model, models=models)

    def _get_enabled(self, model_id: str) -> ModelEntry:
        catalog = self._load_if_changed()
        for item in catalog.models:
            if item.id == model_id and item.enabled:
                return item
        raise UnknownModelError(f"Unknown or disabled model: {model_id}")

    def create_model(self, model_id: str) -> BaseChatModel:
        entry = self._get_enabled(model_id)
        return self._create_model(entry)

    def create_router_model(self) -> tuple[BaseChatModel, float]:
        catalog = self._load_if_changed()
        if catalog.router_model is None:
            raise UnavailableModelError("Router model is not configured.")
        entry = next(
            item
            for item in catalog.models
            if item.id == catalog.router_model and item.enabled
        )
        model = self._create_model(entry, temperature_override=0.0)
        return model, entry.timeout_seconds

    def _create_model(
        self,
        entry: ModelEntry,
        *,
        temperature_override: float | None = None,
    ) -> BaseChatModel:
        available, reason = self._availability(entry)
        if not available:
            raise UnavailableModelError(reason or f"Model is unavailable: {entry.id}")

        kwargs: dict[str, Any] = dict(entry.parameters)
        kwargs.setdefault("timeout", entry.timeout_seconds)
        kwargs.setdefault("max_retries", entry.max_retries)
        if temperature_override is not None:
            kwargs["temperature"] = temperature_override
        elif entry.temperature is not None:
            kwargs.setdefault("temperature", entry.temperature)
        if entry.max_tokens is not None:
            kwargs.setdefault("max_tokens", entry.max_tokens)
        if entry.api_key_env:
            kwargs["api_key"] = os.environ[entry.api_key_env]

        base_url = entry.base_url
        if entry.base_url_env:
            base_url = os.environ[entry.base_url_env]
        if base_url:
            kwargs["base_url"] = base_url

        try:
            return init_chat_model(
                model=entry.model,
                model_provider=entry.provider,
                **kwargs,
            )
        except Exception as exc:
            raise UnavailableModelError(
                f"Could not initialize model '{entry.id}': {exc}"
            ) from exc
