from __future__ import annotations

import os

import pytest
from app.core.settings import PROJECT_ROOT
from app.db.session import create_database
from app.services.conversation_service import ConversationService
from dotenv import load_dotenv


@pytest.mark.database
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_DATABASE_TESTS") != "1",
    reason="Set RUN_DATABASE_TESTS=1 to run PostgreSQL integration tests.",
)
async def test_conversation_round_trip_in_postgres() -> None:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    database_url = os.environ["DATABASE_URL"]
    engine, session_factory = create_database(database_url)
    service = ConversationService(session_factory)
    conversation_id = None

    try:
        turn = await service.start_turn(None, "integration-test-model", "数据库集成测试")
        conversation_id = turn.conversation_id
        await service.finish_turn(
            turn.assistant_message_id,
            "数据库回复已保存",
            "completed",
        )

        detail = await service.get_conversation(turn.conversation_id)
        assert detail.title == "数据库集成测试"
        assert detail.model_id == "integration-test-model"
        assert [(message.role, message.content, message.status) for message in detail.messages] == [
            ("user", "数据库集成测试", "completed"),
            ("assistant", "数据库回复已保存", "completed"),
        ]
        assert turn.conversation_id in {
            conversation.id for conversation in await service.list_conversations()
        }
    finally:
        if conversation_id is not None:
            await service.delete_conversation(conversation_id)
        await engine.dispose()
