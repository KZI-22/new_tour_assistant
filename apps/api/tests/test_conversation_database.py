from __future__ import annotations

import os

import pytest
from app.core.settings import PROJECT_ROOT
from app.db.models import ToolCallLog, TravelPlanVersion
from app.db.session import create_database
from app.schemas.itinerary import DayPlan, ItineraryPlan, TripRequest
from app.services.conversation_service import ConversationService
from app.services.tool_call_log_service import ToolCallLogEntry, ToolCallLogService
from app.services.trip_plan_service import TripPlanService
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
    trip_plan_service = TripPlanService(session_factory)
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
        async with session_factory() as session:
            tool_log = await session.scalar(
                select(ToolCallLog).where(
                    ToolCallLog.assistant_message_id == turn.assistant_message_id
                )
            )
        assert tool_log is not None
        assert tool_log.arguments_json == {"origin": "上海", "destination": "北京"}
        assert tool_log.result_summary == "已完成查询航班。"

        request = TripRequest(
            origin="南京",
            destinations=["杭州"],
            start_date="2026-07-20",
            end_date="2026-07-21",
        )
        plan = ItineraryPlan(
            title="杭州两日游",
            origin="南京",
            destination="杭州",
            start_date="2026-07-20",
            end_date="2026-07-21",
            days=[
                DayPlan(date="2026-07-20", day_index=1),
                DayPlan(date="2026-07-21", day_index=2),
            ],
        )
        version_one = await trip_plan_service.save_plan(turn.conversation_id, request, plan)
        updated_plan = plan.model_copy(deep=True)
        updated_plan.warnings.append("第二版")
        version_two = await trip_plan_service.save_plan(
            turn.conversation_id,
            request,
            updated_plan,
            change_summary="增加第二版提醒",
        )
        current = await trip_plan_service.get_current(turn.conversation_id)
        async with session_factory() as session:
            versions = list(
                await session.scalars(
                    select(TravelPlanVersion)
                    .where(TravelPlanVersion.plan_id == version_one.id)
                    .order_by(TravelPlanVersion.version)
                )
            )

        assert version_one.version == 1
        assert version_two.version == 2
        assert current is not None and current.plan == updated_plan
        assert [item.version for item in versions] == [1, 2]
        assert versions[0].plan_json["warnings"] == []
    finally:
        if conversation_id is not None:
            await service.delete_conversation(conversation_id)
        await engine.dispose()
