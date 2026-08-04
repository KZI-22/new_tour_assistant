from __future__ import annotations

from pathlib import Path

import pytest
from app.clients.amap_cache import InMemoryAmapCache, RedisAmapCache
from app.clients.xhs_mcp_client import XhsMcpClient
from app.core.request_context import get_request_context
from app.core.settings import Settings
from app.main import create_app
from fastapi import Request
from fastapi.testclient import TestClient


def settings(tmp_path: Path, *, api_key: str | None, redis_url: str | None = None) -> Settings:
    config_path = tmp_path / "models.yaml"
    config_path.write_text("default_model: null\nmodels: []\n", encoding="utf-8")
    return Settings(
        app_name="Test API",
        model_config_path=config_path,
        cors_origins=("http://localhost:3000",),
        log_level="WARNING",
        amap_api_key=api_key,
        redis_url=redis_url,
    )


def test_configured_redis_backs_the_amap_cache_even_without_authentication(
    tmp_path: Path,
) -> None:
    application = create_app(
        settings(tmp_path, api_key="test-key", redis_url="redis://127.0.0.1:6379/0")
    )

    assert application.state.auth_service is None
    assert application.state.redis_client is not None
    assert isinstance(application.state.amap_cache, RedisAmapCache)


def test_without_redis_the_amap_cache_stays_process_local(tmp_path: Path) -> None:
    application = create_app(settings(tmp_path, api_key="test-key"))

    assert application.state.redis_client is None
    assert isinstance(application.state.amap_cache, InMemoryAmapCache)


def test_missing_amap_key_keeps_all_flyai_specialist_tools(tmp_path: Path) -> None:
    application = create_app(settings(tmp_path, api_key=None))

    assert application.state.amap_client is None
    assert not hasattr(application.state, "trip_plan_service")
    assert {tool.name for tool in application.state.travel_tools} == {
        "ai_search",
        "search_poi",
        "keyword_search",
        "search_flight",
        "search_train",
        "search_hotel",
    }
    assert {tool.name for tool in application.state.travel_assistant_tools} == {
        "search_poi",
        "keyword_search",
    }


def test_configured_amap_tools_are_registered_without_duplicate_names(tmp_path: Path) -> None:
    application = create_app(settings(tmp_path, api_key="test-key"))
    names = [tool.name for tool in application.state.travel_tools]

    assert len(names) == len(set(names)) == 12
    assert set(names) == {
        "ai_search",
        "search_poi",
        "keyword_search",
        "search_flight",
        "search_train",
        "search_hotel",
        "amap_get_current_city",
        "amap_search_places",
        "amap_search_restaurants",
        "amap_plan_route",
        "amap_travel_time_matrix",
        "amap_get_weather",
    }
    assert [tool.name for tool in application.state.travel_assistant_tools] == [
        "search_poi",
        "keyword_search",
        "amap_plan_route",
    ]


def test_stdio_xhs_settings_are_wired_into_the_app(tmp_path: Path) -> None:
    configured = settings(tmp_path, api_key=None)
    configured = Settings(
        app_name=configured.app_name,
        model_config_path=configured.model_config_path,
        cors_origins=configured.cors_origins,
        log_level=configured.log_level,
        xhs_mcp_transport="stdio",
        xhs_mcp_stdio_command="python",
        xhs_mcp_stdio_args=("-m", "xhs_read_mcp", "--transport", "stdio"),
        xhs_mcp_stdio_cwd=tmp_path,
    )

    application = create_app(configured)
    client = application.state.xhs_mcp_client

    assert client._transport == "stdio"
    assert client._stdio_parameters.command == "python"
    assert client._stdio_parameters.args == [
        "-m",
        "xhs_read_mcp",
        "--transport",
        "stdio",
    ]
    assert client._stdio_parameters.cwd == tmp_path


def test_fastapi_middleware_injects_trusted_ip_and_time_context(tmp_path: Path) -> None:
    configured = settings(tmp_path, api_key=None)
    configured = Settings(
        app_name=configured.app_name,
        model_config_path=configured.model_config_path,
        cors_origins=configured.cors_origins,
        log_level=configured.log_level,
        app_timezone="Asia/Shanghai",
        trusted_proxy_cidrs=("10.0.0.0/8",),
    )
    application = create_app(configured)

    @application.get("/_test/request-context")
    async def show_context(request: Request) -> dict[str, object]:
        context = get_request_context()
        assert context is request.state.travel_context
        assert context is not None
        return context.model_dump(mode="json")

    with TestClient(application, client=("10.0.0.2", 12345)) as client:
        response = client.get(
            "/_test/request-context",
            headers={"X-Forwarded-For": "8.8.8.8, 10.0.0.3"},
        )

    assert response.status_code == 200
    assert response.json()["client_ip"] == "8.8.8.8"
    assert response.json()["client_ip_is_public_ipv4"] is True
    assert response.json()["time"]["timezone"] == "Asia/Shanghai"


def test_app_lifespan_closes_xhs_mcp_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[XhsMcpClient] = []

    async def record_close(client: XhsMcpClient) -> None:
        closed.append(client)

    monkeypatch.setattr(XhsMcpClient, "aclose", record_close)
    application = create_app(settings(tmp_path, api_key=None))

    with TestClient(application):
        pass

    assert closed == [application.state.xhs_mcp_client]
