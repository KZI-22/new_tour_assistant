from __future__ import annotations

import os

import pytest
from app.core.settings import PROJECT_ROOT
from app.db.models import ToolCallLog
from app.db.session import create_database
from app.services.conversation_service import ConversationService
from app.services.tool_call_log_service import (
    ToolCallLogEntry,
    ToolCallLogService,
    ToolCallQualityUpdate,
)
from dotenv import load_dotenv
from sqlalchemy import select


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
    tool_log_service = ToolCallLogService(session_factory)
    conversation_id = None

    try:
        turn = await service.start_turn(None, "integration-test-model", "数据库集成测试")
        conversation_id = turn.conversation_id
        await service.finish_turn(
            turn.assistant_message_id,
            "数据库回复已保存",
            "completed",
        )
        await tool_log_service.record(
            ToolCallLogEntry(
                conversation_id=turn.conversation_id,
                assistant_message_id=turn.assistant_message_id,
                tool_call_id="database-tool-call",
                tool_name="search_flight",
                provider="flyai",
                arguments={"origin": "上海", "destination": "北京"},
                status="success",
                result_summary="已完成查询航班。",
                error_code=None,
                duration_ms=12,
            )
        )
        await tool_log_service.update_data_quality(
            ToolCallQualityUpdate(
                conversation_id=turn.conversation_id,
                assistant_message_id=turn.assistant_message_id,
                tool_call_id="database-tool-call",
                data_status="usable",
                provider_item_count=3,
                normalized_item_count=2,
                rejected_item_count=0,
                schema_version="flyai-transport-v1",
                result_summary="已取得 2 条可用数据。",
                error_code=None,
            )
        )
        await tool_log_service.record(
            ToolCallLogEntry(
                conversation_id=turn.conversation_id,
                assistant_message_id=turn.assistant_message_id,
                tool_call_id="database-rate-limit-call",
                tool_name="amap_plan_route",
                provider="amap",
                arguments={"mode": "driving"},
                status="failed",
                result_summary="计算行程时间服务请求过于频繁，请稍后重试。",
                error_code="PROVIDER_RATE_LIMITED",
                provider_error_code="10003",
                duration_ms=20,
            )
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
        assert len(detail.tool_calls) == 2
        tool_calls_by_id = {item.tool_call_id: item for item in detail.tool_calls}
        assert tool_calls_by_id["database-tool-call"].data_status == "usable"
        assert tool_calls_by_id["database-tool-call"].normalized_item_count == 2
        assert tool_calls_by_id["database-rate-limit-call"].error_code == (
            "PROVIDER_RATE_LIMITED"
        )
        assert tool_calls_by_id["database-rate-limit-call"].provider_error_code == "10003"
        async with session_factory() as session:
            tool_log = await session.scalar(
                select(ToolCallLog).where(
                    ToolCallLog.tool_call_id == "database-tool-call"
                )
            )
        assert tool_log is not None
        assert tool_log.arguments_json == {"origin": "上海", "destination": "北京"}
        assert tool_log.result_summary == "已取得 2 条可用数据。"
        assert tool_log.provider_error_code is None

    finally:
        if conversation_id is not None:
            await service.delete_conversation(conversation_id)
        await engine.dispose()
