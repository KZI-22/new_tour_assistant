from __future__ import annotations

import json
import os
from collections.abc import Iterator

import pytest
from app.core.settings import get_settings
from app.main import create_app
from fastapi.testclient import TestClient


def _parse_sse(value: str) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []
    for frame in value.replace("\r\n", "\n").split("\n\n"):
        event = "message"
        data_lines: list[str] = []
        for line in frame.splitlines():
            if line.startswith("event:"):
                event = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data_lines.append(line.removeprefix("data:").lstrip())
        if data_lines:
            events.append((event, json.loads("\n".join(data_lines))))
    return events


@pytest.fixture(scope="module")
def live_client() -> Iterator[TestClient]:
    with TestClient(
        create_app(get_settings()),
        client=("220.181.38.148", 50000),
    ) as client:
        yield client


@pytest.mark.e2e
@pytest.mark.skipif(
    os.getenv("RUN_TOOL_CALL_E2E") != "1",
    reason="Set RUN_TOOL_CALL_E2E=1 to consume configured model and provider quotas.",
)
@pytest.mark.parametrize(
    ("prompt", "required_tools", "forbidden_tools", "answer_marker"),
    [
        (
            "帮我查 2026 年 7 月 20 日上海到北京的航班",
            {"search_flight"},
            set(),
            None,
        ),
        (
            "帮我查 2026 年 7 月 20 日上海到北京的火车",
            {"search_train"},
            set(),
            None,
        ),
        (
            "帮我查 2026 年 7 月 20 日入住、7 月 22 日退房的杭州酒店",
            {"search_hotel"},
            set(),
            None,
        ),
        (
            "今天天气怎么样？",
            {"amap_get_current_city", "amap_get_weather"},
            set(),
            None,
        ),
        (
            "从南京南站怎么去总统府？",
            {"amap_search_places", "amap_plan_route"},
            set(),
            None,
        ),
        (
            "比较 2026 年 7 月 20 日上海到北京的飞机和高铁",
            {"search_flight", "search_train"},
            set(),
            None,
        ),
        (
            "帮我查上海到北京的航班",
            set(),
            {"search_flight"},
            "日期",
        ),
        (
            "给我一句关于旅行的文案",
            set(),
            {
                "search_flight",
                "search_train",
                "search_hotel",
                "search_poi",
                "amap_get_current_city",
                "amap_search_places",
                "amap_plan_route",
                "amap_travel_time_matrix",
                "amap_get_weather",
            },
            None,
        ),
    ],
)
def test_real_chat_tool_call_scenarios(
    live_client: TestClient,
    prompt: str,
    required_tools: set[str],
    forbidden_tools: set[str],
    answer_marker: str | None,
) -> None:
    model_id = os.getenv("TOOL_CALL_E2E_MODEL", "mimo-v2.5-pro")
    response = live_client.post(
        "/api/v1/chat/stream",
        json={"model_id": model_id, "message": prompt},
    )
    assert response.status_code == 200, response.text
    events = _parse_sse(response.text)

    conversation_id = next(
        (
            str(payload["id"])
            for event, payload in events
            if event == "conversation" and payload.get("id")
        ),
        None,
    )
    try:
        tool_sequence = [
            str(payload["tool_name"])
            for event, payload in events
            if event == "tool_call" and payload.get("tool_name")
        ]
        called_tools = set(tool_sequence)
        assert required_tools <= called_tools
        assert not (forbidden_tools & called_tools)
        result_statuses = [
            (
                str(payload["tool_name"]),
                bool(payload.get("success")),
                payload.get("error_code"),
            )
            for event, payload in events
            if event == "tool_result" and payload.get("tool_name")
        ]
        assert required_tools <= {tool_name for tool_name, _, _ in result_statuses}

        answer = "".join(
            str(payload.get("delta", "")) for event, payload in events if event == "message_delta"
        )
        assert answer, events
        if answer_marker:
            assert answer_marker in answer
        assert any(event == "message_end" for event, _ in events)
        print(
            f"E2E prompt={prompt!r} tools={tool_sequence or ['none']} "
            f"results={result_statuses or ['none']}"
        )
    finally:
        if conversation_id:
            assert live_client.delete(f"/api/v1/conversations/{conversation_id}").status_code == 204
