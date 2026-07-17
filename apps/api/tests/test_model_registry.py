from __future__ import annotations

import os
from pathlib import Path

import pytest
from app.core.model_registry import (
    ModelRegistry,
    ModelRegistryError,
    UnavailableModelError,
)


def write_catalog(path: Path, *, enabled: bool = True) -> None:
    path.write_text(
        f"""
default_model: test-model
models:
  - id: test-model
    display_name: Test model
    description: Used by tests
    provider: openai
    model: upstream-model
    api_key_env: TEST_MODEL_API_KEY
    enabled: {str(enabled).lower()}
    temperature: 0.2
    max_tokens: 128
""".strip(),
        encoding="utf-8",
    )


def test_list_models_marks_missing_credentials_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "models.yaml"
    write_catalog(config_path)
    monkeypatch.delenv("TEST_MODEL_API_KEY", raising=False)

    catalog = ModelRegistry(config_path).list_models()

    assert catalog.default_model == "test-model"
    assert len(catalog.models) == 1
    assert catalog.models[0].available is False
    assert "TEST_MODEL_API_KEY" in (catalog.models[0].unavailable_reason or "")


def test_create_model_uses_environment_secret_and_configured_parameters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "models.yaml"
    write_catalog(config_path)
    monkeypatch.setenv("TEST_MODEL_API_KEY", "secret-for-test")
    captured: dict[str, object] = {}
    marker = object()

    def fake_init_chat_model(**kwargs: object) -> object:
        captured.update(kwargs)
        return marker

    monkeypatch.setattr("app.core.model_registry.init_chat_model", fake_init_chat_model)

    result = ModelRegistry(config_path).create_model("test-model")

    assert result is marker
    assert captured["model"] == "upstream-model"
    assert captured["model_provider"] == "openai"
    assert captured["api_key"] == "secret-for-test"
    assert captured["temperature"] == 0.2
    assert captured["max_tokens"] == 128


def test_create_model_preserves_provider_generation_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "models.yaml"
    config_path.write_text(
        """
models:
  - id: test-model
    display_name: Test model
    provider: openai
    model: upstream-model
    api_key_env: TEST_MODEL_API_KEY
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_MODEL_API_KEY", "secret-for-test")
    captured: dict[str, object] = {}

    def fake_init_chat_model(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("app.core.model_registry.init_chat_model", fake_init_chat_model)

    ModelRegistry(config_path).create_model("test-model")

    assert "temperature" not in captured
    assert "max_tokens" not in captured


def test_create_model_rejects_missing_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "models.yaml"
    write_catalog(config_path)
    monkeypatch.delenv("TEST_MODEL_API_KEY", raising=False)

    with pytest.raises(UnavailableModelError, match="TEST_MODEL_API_KEY"):
        ModelRegistry(config_path).create_model("test-model")


def test_disabled_models_are_not_exposed(tmp_path: Path) -> None:
    config_path = tmp_path / "models.yaml"
    write_catalog(config_path, enabled=False)

    catalog = ModelRegistry(config_path).list_models()

    assert catalog.models == []
    assert catalog.default_model is None


def test_router_model_must_reference_an_enabled_catalog_entry(tmp_path: Path) -> None:
    config_path = tmp_path / "models.yaml"
    config_path.write_text(
        """
router_model: disabled-router
models:
  - id: disabled-router
    display_name: Disabled router
    provider: openai
    model: upstream-router
    enabled: false
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ModelRegistryError, match="router_model"):
        ModelRegistry(config_path).list_models()


def test_create_router_model_uses_fixed_entry_and_low_temperature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "models.yaml"
    config_path.write_text(
        """
default_model: chat-model
router_model: router-model
models:
  - id: chat-model
    display_name: Chat model
    provider: openai
    model: upstream-chat
    api_key_env: TEST_MODEL_API_KEY
  - id: router-model
    display_name: Router model
    provider: openai
    model: upstream-router
    api_key_env: TEST_MODEL_API_KEY
    temperature: 0.8
    timeout_seconds: 17
    max_retries: 4
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_MODEL_API_KEY", "secret-for-test")
    captured: dict[str, object] = {}
    marker = object()

    def fake_init_chat_model(**kwargs: object) -> object:
        captured.update(kwargs)
        return marker

    monkeypatch.setattr("app.core.model_registry.init_chat_model", fake_init_chat_model)

    model, timeout_seconds = ModelRegistry(config_path).create_router_model()

    assert model is marker
    assert timeout_seconds == 17
    assert captured["model"] == "upstream-router"
    assert captured["temperature"] == 0.0
    assert captured["timeout"] == 17
    assert captured["max_retries"] == 4
    assert captured["api_key"] == "secret-for-test"


def test_router_model_selection_hot_reloads_with_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "models.yaml"

    def write_router(router_id: str) -> None:
        config_path.write_text(
            f"""
router_model: {router_id}
models:
  - id: router-a
    display_name: Router A
    provider: openai
    model: upstream-a
  - id: router-b
    display_name: Router B
    provider: openai
    model: upstream-b
""".strip(),
            encoding="utf-8",
        )

    monkeypatch.setattr(
        "app.core.model_registry.init_chat_model",
        lambda **kwargs: kwargs["model"],
    )
    write_router("router-a")
    registry = ModelRegistry(config_path)

    first_model, _ = registry.create_router_model()
    previous_stat = config_path.stat()
    write_router("router-b")
    os.utime(
        config_path,
        ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns + 1_000_000),
    )
    second_model, _ = registry.create_router_model()

    assert first_model == "upstream-a"
    assert second_model == "upstream-b"
