from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
from app.core.settings import PROJECT_ROOT
from app.db.models import ToolCallLog, User
from app.db.session import create_database
from app.services.conversation_service import ConversationNotFoundError, ConversationService
from app.services.tool_call_log_service import (
    ToolCallLogEntry,
    ToolCallLogService,
    ToolCallQualityUpdate,
)
from dotenv import load_dotenv
from sqlalchemy import delete, select


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
    owner_id = None
    other_user_id = None

    try:
        now = datetime.now(UTC)
        async with session_factory() as session, session.begin():
            owner = User(
                phone_e164=f"+86138{uuid.uuid4().int % 100_000_000:08d}",
                status="active",
                phone_verified_at=now,
                last_login_at=now,
                created_at=now,
                updated_at=now,
            )
            other_user = User(
                phone_e164=f"+86137{uuid.uuid4().int % 100_000_000:08d}",
                status="active",
                phone_verified_at=now,
                last_login_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add_all([owner, other_user])
            await session.flush()
            owner_id = owner.id
            other_user_id = other_user.id

        turn = await service.start_turn(
            owner_id,
            None,
            "integration-test-model",
            "数据库集成测试",
            "xhs",
        )
        other_turn = await service.start_turn(
            other_user_id,
            None,
            "integration-test-model",
            "另一个用户的会话",
        )
        await service.finish_turn(
            other_turn.assistant_message_id,
            "另一个用户的回复",
            "completed",
        )
        await service.finish_turn(
            turn.assistant_message_id,
            "数据库回复已保存",
            "completed",
            debug_trace=[
                {
                    "type": "planning_trace",
                    "sequence": 1,
                    "step": "search_query_built",
                    "title": "已生成小红书搜索词",
                    "status": "success",
                    "detail": None,
                    "duration_ms": None,
                    "data": {"keyword": "西安 两日游"},
                    "occurred_at": "2026-07-20T08:00:00Z",
                }
            ],
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

        detail = await service.get_conversation(owner_id, turn.conversation_id)
        assert detail.title == "数据库集成测试"
        assert detail.model_id == "integration-test-model"
        assert detail.planning_source == "xhs"
        assert [(message.role, message.content, message.status) for message in detail.messages] == [
            ("user", "数据库集成测试", "completed"),
            ("assistant", "数据库回复已保存", "completed"),
        ]
        assert detail.messages[1].debug_trace[0].data == {"keyword": "西安 两日游"}
        conversations = await service.list_conversations(owner_id)
        persisted = next(
            conversation
            for conversation in conversations
            if conversation.id == turn.conversation_id
        )
        assert persisted.planning_source == "xhs"
        assert other_turn.conversation_id not in {item.id for item in conversations}
        with pytest.raises(ConversationNotFoundError):
            await service.get_conversation(owner_id, other_turn.conversation_id)
        with pytest.raises(ConversationNotFoundError):
            await service.start_turn(
                owner_id,
                other_turn.conversation_id,
                "integration-test-model",
                "尝试越权继续",
            )
        with pytest.raises(ConversationNotFoundError):
            await service.delete_conversation(owner_id, other_turn.conversation_id)
        other_detail = await service.get_conversation(
            other_user_id,
            other_turn.conversation_id,
        )
        assert other_detail.title == "另一个用户的会话"
        assert [item.content for item in other_detail.messages] == [
            "另一个用户的会话",
            "另一个用户的回复",
        ]
        assert len(detail.tool_calls) == 2
        tool_calls_by_id = {item.tool_call_id: item for item in detail.tool_calls}
        assert tool_calls_by_id["database-tool-call"].data_status == "usable"
        assert tool_calls_by_id["database-tool-call"].normalized_item_count == 2
        assert tool_calls_by_id["database-rate-limit-call"].error_code == ("PROVIDER_RATE_LIMITED")
        assert tool_calls_by_id["database-rate-limit-call"].provider_error_code == "10003"
        async with session_factory() as session:
            tool_log = await session.scalar(
                select(ToolCallLog).where(ToolCallLog.tool_call_id == "database-tool-call")
            )
        assert tool_log is not None
        assert tool_log.arguments_json == {"origin": "上海", "destination": "北京"}
        assert tool_log.result_summary == "已取得 2 条可用数据。"
        assert tool_log.provider_error_code is None

    finally:
        user_ids = [item for item in (owner_id, other_user_id) if item is not None]
        if user_ids:
            async with session_factory() as session, session.begin():
                await session.execute(delete(User).where(User.id.in_(user_ids)))
        await engine.dispose()
