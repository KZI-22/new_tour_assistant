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
