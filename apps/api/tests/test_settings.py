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
    monkeypatch.setenv("MAX_TOOL_ROUNDS", "4")
    monkeypatch.setenv("TOOL_EXECUTION_TIMEOUT_SECONDS", "95")

    settings = get_settings()

    assert settings.flyai_cli_path == "C:\\tools\\flyai.cmd"
    assert settings.flyai_timeout_seconds == 90
    assert settings.flyai_max_concurrency == 2
    assert settings.max_tool_rounds == 4
    assert settings.tool_execution_timeout_seconds == 95


def test_get_settings_reads_amap_and_request_context_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AMAP_API_KEY", "test-key")
    monkeypatch.setenv("AMAP_BASE_URL", "https://restapi.amap.test/")
    monkeypatch.setenv("AMAP_TIMEOUT_SECONDS", "20")
    monkeypatch.setenv("AMAP_MAX_RETRIES", "2")
    monkeypatch.setenv("AMAP_MIN_REQUEST_INTERVAL_SECONDS", "0.4")
    monkeypatch.setenv("APP_TIMEZONE", "Asia/Shanghai")
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "10.0.0.0/8, 2001:db8::/32")

    settings = get_settings()

    assert settings.amap_api_key == "test-key"
    assert settings.amap_base_url == "https://restapi.amap.test"
    assert settings.amap_timeout_seconds == 20
    assert settings.amap_max_retries == 2
    assert settings.amap_min_request_interval_seconds == 0.4
    assert settings.app_timezone == "Asia/Shanghai"
    assert settings.trusted_proxy_cidrs == ("10.0.0.0/8", "2001:db8::/32")


def test_get_settings_reads_trip_planner_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIP_PLANNER_ENABLED", "false")
    monkeypatch.setenv("TRIP_PLANNER_MAX_DAYS", "4")
    monkeypatch.setenv("TRIP_PLANNER_MAX_REVISIONS", "1")
    monkeypatch.setenv("TRIP_PLANNER_MAX_POI_CANDIDATES", "12")
    monkeypatch.setenv("TRIP_PLANNER_MAX_TRANSPORT_OPTIONS", "8")
    monkeypatch.setenv("TRIP_PLANNER_MAX_HOTEL_OPTIONS", "6")
    monkeypatch.setenv("TRIP_PLANNER_MAX_DAILY_ACTIVITIES", "4")
    monkeypatch.setenv("TRIP_PLANNER_TOOL_TIMEOUT_SECONDS", "80")
    monkeypatch.setenv("TRIP_PLANNER_MODEL_TIMEOUT_SECONDS", "35")
    monkeypatch.setenv("TRIP_PLANNER_REQUEST_EXTRACTION_TIMEOUT_SECONDS", "25")
    monkeypatch.setenv("TRIP_PLANNER_RESULT_MAX_LENGTH", "12000")

    settings = get_settings()

    assert settings.trip_planner_enabled is False
    assert settings.trip_planner_max_days == 4
    assert settings.trip_planner_max_revisions == 1
    assert settings.trip_planner_max_poi_candidates == 12
    assert settings.trip_planner_max_transport_options == 8
    assert settings.trip_planner_max_hotel_options == 6
    assert settings.trip_planner_max_daily_activities == 4
    assert settings.trip_planner_tool_timeout_seconds == 80
    assert settings.trip_planner_model_timeout_seconds == 35
    assert settings.trip_planner_request_extraction_timeout_seconds == 25
    assert settings.trip_planner_result_max_length == 12000
