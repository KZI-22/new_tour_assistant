from __future__ import annotations

import os
from pathlib import Path

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


def test_amap_cache_ttl_overrides_default_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AMAP_CACHE_TTL_OVERRIDES", raising=False)

    assert get_settings().amap_cache_ttl_overrides == {}


def test_amap_cache_ttl_overrides_are_parsed_from_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMAP_CACHE_TTL_OVERRIDES", '{"geocode": 60, "route_plan": 120.5}')

    assert get_settings().amap_cache_ttl_overrides == {"geocode": 60.0, "route_plan": 120.5}


@pytest.mark.parametrize(
    "raw",
    ['["geocode"]', '{"geocode": "soon"}', '{"geocode": 0}', '{"geocode": -1}', "{"],
)
def test_invalid_amap_cache_ttl_overrides_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    monkeypatch.setenv("AMAP_CACHE_TTL_OVERRIDES", raw)

    with pytest.raises(ValueError, match="AMAP_CACHE_TTL_OVERRIDES"):
        get_settings()


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
    monkeypatch.setenv("TRIP_PLANNER_MODEL_TIMEOUT_SECONDS", "35")
    monkeypatch.setenv("TRIP_PLANNER_REQUEST_EXTRACTION_TIMEOUT_SECONDS", "25")
    monkeypatch.setenv("AMAP_POI_MAX_CONCURRENCY", "4")
    monkeypatch.setenv("AMAP_ROUTE_MAX_CONCURRENCY", "3")
    monkeypatch.setenv("AMAP_POI_PAGE_SIZE", "9")
    monkeypatch.setenv("MAX_RAW_POI_CANDIDATES", "55")
    monkeypatch.setenv("MAX_WALK_DISTANCE_METERS", "1700")
    monkeypatch.setenv("MAX_TRANSIT_TRANSFERS", "2")
    monkeypatch.setenv("MAX_TRANSIT_DURATION_MINUTES", "80")
    monkeypatch.setenv("TRIP_PLANNING_CLUSTER_MAX_ITERATIONS", "12")
    monkeypatch.setenv("TRIP_PLANNING_DATA_TIMEOUT_SECONDS", "8")
    monkeypatch.setenv("XHS_MCP_URL", "http://xhs.internal:8765/mcp/")
    monkeypatch.setenv("XHS_MCP_AUTH_TOKEN", "private-token")
    monkeypatch.setenv("XHS_MCP_TIMEOUT_SECONDS", "70")
    monkeypatch.setenv("XHS_MIN_POST_CONTENT_CHARS", "240")
    monkeypatch.setenv("XHS_DETAIL_CANDIDATE_LIMIT", "4")
    monkeypatch.setenv("XHS_LOGIN_POLL_SECONDS", "2.5")
    monkeypatch.setenv("XHS_SSE_HEARTBEAT_SECONDS", "16")

    settings = get_settings()

    assert settings.trip_planner_enabled is False
    assert settings.trip_planner_max_days == 4
    assert settings.trip_planner_model_timeout_seconds == 35
    assert settings.trip_planner_request_extraction_timeout_seconds == 25
    assert settings.amap_poi_max_concurrency == 4
    assert settings.amap_route_max_concurrency == 3
    assert settings.amap_poi_page_size == 9
    assert settings.max_raw_poi_candidates == 55
    assert settings.max_walk_distance_meters == 1700
    assert settings.max_transit_transfers == 2
    assert settings.max_transit_duration_minutes == 80
    assert settings.trip_planning_cluster_max_iterations == 12
    assert settings.trip_planning_data_timeout_seconds == 8
    assert settings.xhs_mcp_url == "http://xhs.internal:8765/mcp"
    assert settings.xhs_mcp_auth_token == "private-token"
    assert settings.xhs_mcp_timeout_seconds == 70
    assert settings.xhs_min_post_content_chars == 240
    assert settings.xhs_detail_candidate_limit == 4
    assert settings.xhs_login_poll_seconds == 2.5
    assert settings.xhs_sse_heartbeat_seconds == 16
    assert "private-token" not in repr(settings)
    legacy_names = {
        "trip_planner_max_revisions",
        "trip_planner_max_poi_candidates",
        "trip_planner_max_transport_options",
        "trip_planner_max_hotel_options",
        "trip_planner_max_hotel_geocodes",
        "trip_planner_max_daily_activities",
        "trip_planner_tool_timeout_seconds",
        "trip_planner_result_max_length",
    }
    assert legacy_names.isdisjoint(settings.__dataclass_fields__)


def test_get_settings_parses_stdio_mcp_process_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XHS_MCP_TRANSPORT", "STDIO")
    monkeypatch.setenv("XHS_MCP_STDIO_COMMAND", "python")
    monkeypatch.setenv(
        "XHS_MCP_STDIO_ARGS",
        '["-m", "xhs_read_mcp", "--transport", "stdio", "--headed"]',
    )
    monkeypatch.setenv("XHS_MCP_STDIO_CWD", str(tmp_path))

    settings = get_settings()

    assert settings.xhs_mcp_transport == "stdio"
    assert settings.xhs_mcp_stdio_command == "python"
    assert settings.xhs_mcp_stdio_args == (
        "-m",
        "xhs_read_mcp",
        "--transport",
        "stdio",
        "--headed",
    )
    assert settings.xhs_mcp_stdio_cwd == tmp_path


def test_get_settings_rejects_non_array_stdio_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XHS_MCP_STDIO_ARGS", '"--transport stdio"')

    with pytest.raises(ValueError, match="JSON array"):
        get_settings()
