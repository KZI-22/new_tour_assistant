from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
import uuid

from fastapi.testclient import TestClient

from app.core.settings import Settings
from app.main import create_app
from app.schemas.chat import ChatMessage
from app.services.conversation_service import TurnContext


def make_client(tmp_path: Path) -> TestClient:
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
    )
    return TestClient(create_app(settings))


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
        async def stream(self, model_id: str, messages: object) -> AsyncIterator[str]:
            assert model_id == "test-model"
            assert messages
            yield "你"
            yield "好"

    class FakeConversationService:
        finished: tuple[uuid.UUID, str, str] | None = None

        async def start_turn(
            self, requested_id: uuid.UUID | None, model_id: str, content: str
        ) -> TurnContext:
            assert requested_id is None
            assert model_id == "test-model"
            assert content == "你好"
            return TurnContext(
                conversation_id=conversation_id,
                conversation_title="你好",
                assistant_message_id=assistant_message_id,
                messages=[ChatMessage(role="user", content=content)],
            )

        async def finish_turn(
            self, message_id: uuid.UUID, content: str, message_status: str
        ) -> None:
            self.finished = (message_id, content, message_status)

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
    assert f'event: conversation\ndata: {{"id":"{conversation_id}","title":"你好"}}' in response.text
    assert 'event: token\ndata: {"delta":"你"}' in response.text
    assert 'event: token\ndata: {"delta":"好"}' in response.text
    assert f'event: done\ndata: {{"conversation_id":"{conversation_id}"}}' in response.text
    assert conversation_service.finished == (assistant_message_id, "你好", "completed")


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
