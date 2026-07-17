from __future__ import annotations

import json
import os
from datetime import date, timedelta

import pytest
from app.core.model_registry import ModelRegistry
from app.core.settings import get_settings
from app.main import create_app
from fastapi.testclient import TestClient


@pytest.mark.e2e
@pytest.mark.flyai
@pytest.mark.amap
@pytest.mark.skipif(
    os.getenv("RUN_TRIP_PLANNER_E2E") != "1",
    reason="Set RUN_TRIP_PLANNER_E2E=1 to consume real model and provider quotas.",
)
def test_real_structured_trip_planner_end_to_end() -> None:
    settings = get_settings()
    assert settings.database_url
    assert settings.amap_api_key
    default_model = ModelRegistry(settings.model_config_path).list_models().default_model
    model_id = os.getenv("TRIP_PLANNER_E2E_MODEL") or default_model
    assert model_id
    start = date.today() + timedelta(days=1)
    end = start + timedelta(days=2)
    prompt = (
        f"帮我规划 {start.isoformat()} 到 {end.isoformat()}，从南京去杭州，"
        "两个人，预算 6000 元，喜欢自然风景和人文景点，行程轻松一点。"
    )

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/chat/stream",
            json={"model_id": model_id, "message": prompt},
        )

        assert response.status_code == 200
        event_names = [
            line.removeprefix("event: ")
            for line in response.text.splitlines()
            if line.startswith("event: ")
        ]
        assert "planning_stage" in event_names
        assert "tool_call" in event_names
        assert "tool_result" in event_names
        assert "message_delta" in event_names
        assert "done" in event_names
        assert "error" not in event_names
        assert "行程概览" in response.text

        conversation_id = _conversation_id(response.text)
        delete_response = client.delete(f"/api/v1/conversations/{conversation_id}")
        assert delete_response.status_code == 204


@pytest.mark.e2e
@pytest.mark.skipif(
    os.getenv("RUN_TRIP_PLANNER_E2E") != "1",
    reason="Set RUN_TRIP_PLANNER_E2E=1 to consume real model quota.",
)
def test_real_destination_only_request_asks_for_origin_without_tools() -> None:
    settings = get_settings()
    assert settings.database_url
    default_model = ModelRegistry(settings.model_config_path).list_models().default_model
    model_id = os.getenv("TRIP_PLANNER_E2E_MODEL") or default_model
    assert model_id
    start = date.today() + timedelta(days=1)
    end = start + timedelta(days=2)
    prompt = f"规划一份去杭州的旅游攻略，{start.isoformat()} 到 {end.isoformat()}。"

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/chat/stream",
            json={"model_id": model_id, "message": prompt},
        )

        assert response.status_code == 200
        event_names = [
            line.removeprefix("event: ")
            for line in response.text.splitlines()
            if line.startswith("event: ")
        ]
        assert "planning_stage" in event_names
        assert "tool_call" not in event_names
        assert "tool_result" not in event_names
        assert "从哪个城市出发" in response.text
        assert "error" not in event_names

        conversation_id = _conversation_id(response.text)
        delete_response = client.delete(f"/api/v1/conversations/{conversation_id}")
        assert delete_response.status_code == 204


def _conversation_id(response_text: str) -> str:
    lines = response_text.splitlines()
    conversation_data = next(
        line.removeprefix("data: ")
        for index, line in enumerate(lines)
        if index > 0
        and lines[index - 1] == "event: conversation"
        and line.startswith("data: ")
    )
    return str(json.loads(conversation_data)["id"])
