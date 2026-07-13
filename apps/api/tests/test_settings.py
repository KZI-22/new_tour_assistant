from __future__ import annotations

import os

import pytest
from app.core.settings import configure_no_proxy, get_settings


def test_configure_no_proxy_preserves_terminal_rules_and_adds_project_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setenv(
        "TOUR_ASSISTANT_NO_PROXY_HOSTS",
        "dashscope.aliyuncs.com,localhost",
    )

    configure_no_proxy()

    expected = "localhost,127.0.0.1,dashscope.aliyuncs.com"
    assert os.environ["NO_PROXY"] == expected
    assert os.environ["no_proxy"] == expected


def test_get_settings_reads_flyai_runtime_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLYAI_CLI_PATH", "C:\\tools\\flyai.cmd")
    monkeypatch.setenv("FLYAI_TIMEOUT_SECONDS", "90")
    monkeypatch.setenv("FLYAI_MAX_CONCURRENCY", "2")

    settings = get_settings()

    assert settings.flyai_cli_path == "C:\\tools\\flyai.cmd"
    assert settings.flyai_timeout_seconds == 90
    assert settings.flyai_max_concurrency == 2


def test_get_settings_reads_amap_and_request_context_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AMAP_API_KEY", "test-key")
    monkeypatch.setenv("AMAP_BASE_URL", "https://restapi.amap.test/")
    monkeypatch.setenv("AMAP_TIMEOUT_SECONDS", "20")
    monkeypatch.setenv("AMAP_MAX_RETRIES", "2")
    monkeypatch.setenv("APP_TIMEZONE", "Asia/Shanghai")
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "10.0.0.0/8, 2001:db8::/32")

    settings = get_settings()

    assert settings.amap_api_key == "test-key"
    assert settings.amap_base_url == "https://restapi.amap.test"
    assert settings.amap_timeout_seconds == 20
    assert settings.amap_max_retries == 2
    assert settings.app_timezone == "Asia/Shanghai"
    assert settings.trusted_proxy_cidrs == ("10.0.0.0/8", "2001:db8::/32")
