from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from app.api.dependencies import require_csrf_protection, require_current_user
from app.core.settings import Settings
from app.main import create_app
from app.schemas.chat import ChatMessage
from app.schemas.tool_execution import (
    MessageDeltaEvent,
    MessagePreviewEvent,
    PlanningStageEvent,
    PlanningTraceEvent,
    ToolCallEvent,
    ToolResultEvent,
    XhsLoginRequiredEvent,
)
from app.services.agent_executor import ToolLoopLimitError
from app.services.auth_service import AuthenticatedUser, AuthenticationError
from app.services.conversation_service import TurnContext
from app.services.tool_execution import ToolExecutionContext
from fastapi.testclient import TestClient

_TEST_USER = AuthenticatedUser(
    id=uuid.UUID("10000000-0000-0000-0000-000000000001"),
    phone_e164="+8613812345678",
    display_name="Test User",
    session_id=uuid.UUID("20000000-0000-0000-0000-000000000001"),
)


def make_client(tmp_path: Path, *, heartbeat_seconds: float = 15) -> TestClient:
    config_path = tmp_path / "models.yaml"
    config_path.write_text(
        """
default_model: test-model
models:
  - id: test-model
    display_name: Test model
    provider: openai
    model: upstream-model
    enabled: true
""".strip(),
        encoding="utf-8",
    )
    settings = Settings(
        app_name="Test API",
        model_config_path=config_path,
        cors_origins=("http://localhost:3000",),
        log_level="WARNING",
        xhs_sse_heartbeat_seconds=heartbeat_seconds,
    )
    client = TestClient(create_app(settings))
    client.app.dependency_overrides[require_current_user] = lambda: _TEST_USER
    client.app.dependency_overrides[require_csrf_protection] = lambda: "test-csrf"
    return client


def test_health_and_models(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    assert client.get("/api/v1/health").json() == {"status": "ok"}
    response = client.get("/api/v1/models")

    assert response.status_code == 200
    assert response.json() == {
        "default_model": "test-model",
        "models": [
            {
                "id": "test-model",
                "display_name": "Test model",
                "description": "",
                "provider": "openai",
                "available": True,
                "unavailable_reason": None,
            }
        ],
    }


def test_delete_conversation_cors_preflight(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.options(
        f"/api/v1/conversations/{uuid.uuid4()}",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "DELETE",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "DELETE" in response.headers["access-control-allow-methods"]


def test_stream_chat_returns_sse_events(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    conversation_id = uuid.uuid4()
    assistant_message_id = uuid.uuid4()

    class FakeChatService:
        async def stream(
            self,
            model_id: str,
            messages: object,
            *,
            planning_source: str,
            execution_context: ToolExecutionContext,
        ) -> AsyncIterator[
            MessageDeltaEvent | MessagePreviewEvent | PlanningStageEvent | PlanningTraceEvent
        ]:
            assert model_id == "test-model"
            assert planning_source == "standard"
            assert messages
            assert execution_context.conversation_id == conversation_id
            assert execution_context.assistant_message_id == assistant_message_id
            yield PlanningStageEvent(
                stage="understanding_request",
                display_name="正在理解旅行需求",
                status="success",
            )
            yield PlanningTraceEvent(
                sequence=1,
                step="search_query_built",
                title="已组装小红书搜索请求",
                status="success",
                data={"keyword": "你好 2日游 攻略"},
            )
            yield MessagePreviewEvent(content="确定性骨架")
            yield MessageDeltaEvent(delta="你")
            yield MessageDeltaEvent(delta="好")

    class FakeConversationService:
        finished: tuple[uuid.UUID, str, str, list[dict[str, object]]] | None = None

        async def start_turn(
            self,
            user_id: uuid.UUID,
            requested_id: uuid.UUID | None,
            model_id: str,
            content: str,
            planning_source: str,
        ) -> TurnContext:
            assert user_id == _TEST_USER.id
            assert requested_id is None
            assert model_id == "test-model"
            assert content == "你好"
            assert planning_source == "standard"
            return TurnContext(
                conversation_id=conversation_id,
                conversation_title="你好",
                assistant_message_id=assistant_message_id,
                messages=[ChatMessage(role="user", content=content)],
            )

        async def finish_turn(
            self,
            message_id: uuid.UUID,
            content: str,
            message_status: str,
            debug_trace: list[dict[str, object]] | None = None,
        ) -> None:
            self.finished = (message_id, content, message_status, debug_trace or [])

    conversation_service = FakeConversationService()
    client.app.state.chat_service = FakeChatService()
    client.app.state.conversation_service = conversation_service
    response = client.post(
        "/api/v1/chat/stream",
        json={
            "model_id": "test-model",
            "message": "你好",
            "conversation_id": None,
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    conversation_event = (
        f'event: conversation\ndata: {{"id":"{conversation_id}","title":"你好",'
        '"planning_source":"standard"}'
    )
    assert conversation_event in response.text
    assert 'event: message_start\ndata: {"type":"message_start"' in response.text
    assert 'event: planning_stage\ndata: {"type":"planning_stage"' in response.text
    assert 'event: planning_trace\ndata: {"type":"planning_trace"' in response.text
    assert (
        'event: message_preview\ndata: {"type":"message_preview","content":"确定性骨架"}'
        in response.text
    )
    assert 'event: message_delta\ndata: {"type":"message_delta","delta":"你"}' in response.text
    assert 'event: message_delta\ndata: {"type":"message_delta","delta":"好"}' in response.text
    assert 'event: message_end\ndata: {"type":"message_end"' in response.text
    assert f'event: done\ndata: {{"conversation_id":"{conversation_id}"}}' in response.text
    assert conversation_service.finished is not None
    assert conversation_service.finished[:3] == (assistant_message_id, "你好", "completed")
    assert conversation_service.finished[3][0]["data"] == {"keyword": "你好 2日游 攻略"}


def test_stream_chat_serializes_browser_login_without_persisting_it(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    conversation_id = uuid.uuid4()
    assistant_message_id = uuid.uuid4()

    class FakeChatService:
        async def stream(
            self,
            model_id: str,
            messages: object,
            *,
            planning_source: str,
            execution_context: ToolExecutionContext,
        ) -> AsyncIterator[XhsLoginRequiredEvent | MessageDeltaEvent]:
            del model_id, messages, planning_source, execution_context
            yield XhsLoginRequiredEvent(
                login_id="fixture-login",
                expires_at="2026-07-19T10:05:00+08:00",
                message="请在已打开的 Google Chrome 中完成验证码。",
                fallback_available=True,
                fallback_mode="map_weather",
            )
            yield MessageDeltaEvent(delta="登录后完成。")

    class FakeConversationService:
        finished: tuple[uuid.UUID, str, str] | None = None

        async def start_turn(
            self,
            _user_id: uuid.UUID,
            requested_id: uuid.UUID | None,
            model_id: str,
            content: str,
            planning_source: str,
        ) -> TurnContext:
            del requested_id, model_id, planning_source
            return TurnContext(
                conversation_id=conversation_id,
                conversation_title=content,
                assistant_message_id=assistant_message_id,
                messages=[ChatMessage(role="user", content=content)],
            )

        async def finish_turn(
            self,
            message_id: uuid.UUID,
            content: str,
            message_status: str,
            **_: object,
        ) -> None:
            self.finished = (message_id, content, message_status)

    conversation_service = FakeConversationService()
    client.app.state.chat_service = FakeChatService()
    client.app.state.conversation_service = conversation_service

    response = client.post(
        "/api/v1/chat/stream",
        json={"model_id": "test-model", "message": "做成都三日游攻略"},
    )

    assert response.status_code == 200
    assert 'event: xhs_login_required\ndata: {"type":"xhs_login_required"' in response.text
    assert "fixture-login" in response.text
    assert "Google Chrome" in response.text
    assert '"fallback_available":true' in response.text
    assert '"fallback_mode":"map_weather"' in response.text
    assert conversation_service.finished == (assistant_message_id, "登录后完成。", "completed")


def test_stream_chat_forwards_explicit_planning_source(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    conversation_id = uuid.uuid4()
    assistant_message_id = uuid.uuid4()
    received_sources: list[str] = []

    class FakeChatService:
        async def stream(
            self,
            model_id: str,
            messages: object,
            *,
            planning_source: str,
            execution_context: ToolExecutionContext,
        ) -> AsyncIterator[MessageDeltaEvent]:
            del model_id, messages, execution_context
            received_sources.append(planning_source)
            yield MessageDeltaEvent(delta="小红书方案")

    class FakeConversationService:
        async def start_turn(
            self,
            _user_id: uuid.UUID,
            requested_id: uuid.UUID | None,
            model_id: str,
            content: str,
            planning_source: str,
        ) -> TurnContext:
            del requested_id, model_id
            assert planning_source == "xhs"
            return TurnContext(
                conversation_id=conversation_id,
                conversation_title=content,
                assistant_message_id=assistant_message_id,
                messages=[ChatMessage(role="user", content=content)],
            )

        async def finish_turn(self, *_: object, **__: object) -> None:
            return None

    client.app.state.chat_service = FakeChatService()
    client.app.state.conversation_service = FakeConversationService()

    response = client.post(
        "/api/v1/chat/stream",
        json={
            "model_id": "test-model",
            "message": "参考小红书规划成都三日游",
            "planning_source": "xhs",
        },
    )

    assert response.status_code == 200
    assert received_sources == ["xhs"]
    assert "小红书方案" in response.text


def test_stream_chat_rejects_unknown_planning_source(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.post(
        "/api/v1/chat/stream",
        json={
            "model_id": "test-model",
            "message": "继续生成",
            "planning_source": "unknown",
        },
    )

    assert response.status_code == 422


def test_stream_chat_emits_heartbeat_without_persisting_it(tmp_path: Path) -> None:
    client = make_client(tmp_path, heartbeat_seconds=0.01)
    conversation_id = uuid.uuid4()
    assistant_message_id = uuid.uuid4()

    class FakeChatService:
        async def stream(
            self,
            model_id: str,
            messages: object,
            *,
            planning_source: str,
            execution_context: ToolExecutionContext,
        ) -> AsyncIterator[PlanningStageEvent | MessageDeltaEvent]:
            del model_id, messages, planning_source, execution_context
            yield PlanningStageEvent(
                stage="waiting_xhs_login",
                display_name="等待登录小红书",
                status="running",
            )
            await asyncio.sleep(0.035)
            yield MessageDeltaEvent(delta="登录后继续。")

    class FakeConversationService:
        finished: tuple[uuid.UUID, str, str] | None = None

        async def start_turn(
            self,
            _user_id: uuid.UUID,
            requested_id: uuid.UUID | None,
            model_id: str,
            content: str,
            planning_source: str,
        ) -> TurnContext:
            del requested_id, model_id, planning_source
            return TurnContext(
                conversation_id=conversation_id,
                conversation_title=content,
                assistant_message_id=assistant_message_id,
                messages=[ChatMessage(role="user", content=content)],
            )

        async def finish_turn(
            self,
            message_id: uuid.UUID,
            content: str,
            message_status: str,
            **_: object,
        ) -> None:
            self.finished = (message_id, content, message_status)

    conversation_service = FakeConversationService()
    client.app.state.chat_service = FakeChatService()
    client.app.state.conversation_service = conversation_service

    response = client.post(
        "/api/v1/chat/stream",
        json={"model_id": "test-model", "message": "登录后继续"},
    )

    assert response.status_code == 200
    assert ": heartbeat\n\n" in response.text
    assert conversation_service.finished == (assistant_message_id, "登录后继续。", "completed")


def test_stream_chat_orders_parallel_tool_events_before_final_text(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    conversation_id = uuid.uuid4()
    assistant_message_id = uuid.uuid4()

    class FakeChatService:
        async def stream(
            self,
            model_id: str,
            messages: object,
            *,
            planning_source: str,
            execution_context: ToolExecutionContext,
        ) -> AsyncIterator[ToolCallEvent | ToolResultEvent | MessageDeltaEvent]:
            del model_id, messages, planning_source, execution_context
            yield ToolCallEvent(
                tool_call_id="flight-call",
                tool_name="search_flight",
                display_name="正在查询航班",
                arguments={"origin": "上海", "destination": "北京"},
            )
            yield ToolCallEvent(
                tool_call_id="train-call",
                tool_name="search_train",
                display_name="正在查询火车",
                arguments={"origin": "上海", "destination": "北京"},
            )
            yield ToolResultEvent(
                tool_call_id="flight-call",
                tool_name="search_flight",
                success=True,
                summary="已完成查询航班。",
                duration_ms=10,
            )
            yield ToolResultEvent(
                tool_call_id="train-call",
                tool_name="search_train",
                success=False,
                summary="查询火车服务暂时不可用。",
                duration_ms=12,
                error_code="PROVIDER_ERROR",
            )
            yield MessageDeltaEvent(delta="航班查询成功，火车查询暂不可用。")

    class FakeConversationService:
        async def start_turn(
            self,
            _user_id: uuid.UUID,
            requested_id: uuid.UUID | None,
            model_id: str,
            content: str,
            planning_source: str,
        ) -> TurnContext:
            del requested_id, model_id, planning_source
            return TurnContext(
                conversation_id=conversation_id,
                conversation_title=content,
                assistant_message_id=assistant_message_id,
                messages=[ChatMessage(role="user", content=content)],
            )

        async def finish_turn(
            self,
            message_id: uuid.UUID,
            content: str,
            message_status: str,
            **_: object,
        ) -> None:
            assert message_id == assistant_message_id
            assert content == "航班查询成功，火车查询暂不可用。"
            assert message_status == "completed"

    client.app.state.chat_service = FakeChatService()
    client.app.state.conversation_service = FakeConversationService()
    response = client.post(
        "/api/v1/chat/stream",
        json={"model_id": "test-model", "message": "比较飞机和高铁"},
    )

    assert response.status_code == 200
    event_names = [
        line.removeprefix("event: ")
        for line in response.text.splitlines()
        if line.startswith("event: ")
    ]
    assert event_names == [
        "conversation",
        "message_start",
        "tool_call",
        "tool_call",
        "tool_result",
        "tool_result",
        "message_delta",
        "message_end",
        "done",
    ]


def test_stream_chat_returns_controlled_error_when_tool_loop_limit_is_reached(
    tmp_path: Path,
) -> None:
    client = make_client(tmp_path)
    conversation_id = uuid.uuid4()
    assistant_message_id = uuid.uuid4()

    class FakeChatService:
        async def stream(
            self,
            model_id: str,
            messages: object,
            *,
            planning_source: str,
            execution_context: ToolExecutionContext,
        ) -> AsyncIterator[ToolCallEvent]:
            del model_id, messages, planning_source, execution_context
            yield ToolCallEvent(
                tool_call_id="loop-call",
                tool_name="search_flight",
                display_name="正在查询航班",
            )
            raise ToolLoopLimitError

    class FakeConversationService:
        finished: tuple[uuid.UUID, str, str] | None = None

        async def start_turn(
            self,
            _user_id: uuid.UUID,
            requested_id: uuid.UUID | None,
            model_id: str,
            content: str,
            planning_source: str,
        ) -> TurnContext:
            del requested_id, model_id, planning_source
            return TurnContext(
                conversation_id=conversation_id,
                conversation_title=content,
                assistant_message_id=assistant_message_id,
                messages=[ChatMessage(role="user", content=content)],
            )

        async def finish_turn(
            self,
            message_id: uuid.UUID,
            content: str,
            message_status: str,
            **_: object,
        ) -> None:
            self.finished = (message_id, content, message_status)

    conversation_service = FakeConversationService()
    client.app.state.chat_service = FakeChatService()
    client.app.state.conversation_service = conversation_service
    response = client.post(
        "/api/v1/chat/stream",
        json={"model_id": "test-model", "message": "重复查询"},
    )

    assert response.status_code == 200
    assert 'event: error\ndata: {"type":"error","code":"TOOL_LOOP_LIMIT"' in response.text
    assert "message_end" not in response.text
    assert conversation_service.finished == (assistant_message_id, "", "failed")


def test_chat_requires_non_empty_message(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.post(
        "/api/v1/chat/stream",
        json={
            "model_id": "test-model",
            "message": "",
        },
    )

    assert response.status_code == 422


def test_private_routes_reject_missing_authentication(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    class RejectingAuthService:
        async def authenticate_access(self, _: str | None) -> AuthenticatedUser:
            raise AuthenticationError("missing")

    client.app.dependency_overrides.pop(require_current_user)
    client.app.state.auth_service = RejectingAuthService()

    assert client.get("/api/v1/conversations").status_code == 401
    assert (
        client.post(
            "/api/v1/chat/stream",
            json={"model_id": "test-model", "message": "未登录请求"},
        ).status_code
        == 401
    )


def test_private_writes_reject_missing_csrf(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    client.app.dependency_overrides.pop(require_csrf_protection)

    chat_response = client.post(
        "/api/v1/chat/stream",
        json={"model_id": "test-model", "message": "缺少 CSRF"},
    )
    delete_response = client.delete(f"/api/v1/conversations/{uuid.uuid4()}")

    assert chat_response.status_code == 403
    assert delete_response.status_code == 403
