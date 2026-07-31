from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Conversation, Message, ToolCallLog
from app.schemas.chat import ChatMessage
from app.schemas.conversation import (
    ConversationDetailResponse,
    ConversationMessageResponse,
    ConversationSummaryResponse,
    ConversationToolCallResponse,
)
from app.schemas.trip_planning import PlanningSource


class ConversationNotFoundError(LookupError):
    """Raised when a conversation id does not exist."""


@dataclass(frozen=True, slots=True)
class TurnContext:
    conversation_id: uuid.UUID
    conversation_title: str
    assistant_message_id: uuid.UUID
    messages: list[ChatMessage]


def _conversation_title(content: str) -> str:
    normalized = re.sub(r"\s+", " ", content).strip()
    if len(normalized) <= 48:
        return normalized
    return f"{normalized[:48].rstrip()}…"


class ConversationService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_conversations(
        self,
        user_id: uuid.UUID,
    ) -> list[ConversationSummaryResponse]:
        async with self._session_factory() as session:
            result = await session.scalars(
                select(Conversation)
                .where(Conversation.user_id == user_id)
                .order_by(Conversation.updated_at.desc())
                .limit(100)
            )
            return [
                ConversationSummaryResponse(
                    id=item.id,
                    title=item.title,
                    model_id=item.model_id,
                    planning_source=item.planning_source,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                )
                for item in result
            ]

    async def get_conversation(
        self,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> ConversationDetailResponse:
        async with self._session_factory() as session:
            conversation = await session.scalar(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.user_id == user_id,
                )
            )
            if conversation is None:
                raise ConversationNotFoundError(str(conversation_id))
            result = await session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.sequence)
            )
            messages = [
                ConversationMessageResponse(
                    id=item.id,
                    sequence=item.sequence,
                    role=item.role,
                    content=item.content,
                    status=item.status,
                    debug_trace=item.debug_trace_json or [],
                    created_at=item.created_at,
                )
                for item in result
            ]
            tool_result = await session.scalars(
                select(ToolCallLog)
                .where(ToolCallLog.conversation_id == conversation_id)
                .order_by(ToolCallLog.created_at, ToolCallLog.id)
            )
            tool_calls = [
                ConversationToolCallResponse(
                    id=item.id,
                    assistant_message_id=item.assistant_message_id,
                    process_status=item.process_status,
                    process_return_code=item.process_return_code,
                    provider_status=item.provider_status,
                    parse_status=item.parse_status,
                    business_status=item.business_status,
                    tool_call_id=item.tool_call_id,
                    tool_name=item.tool_name,
                    provider=item.provider,
                    status=item.status,
                    result_summary=item.result_summary,
                    error_code=item.error_code,
                    provider_error_code=item.provider_error_code,
                    duration_ms=item.duration_ms,
                    data_status=item.data_status,
                    provider_item_count=item.provider_item_count,
                    normalized_item_count=item.normalized_item_count,
                    rejected_item_count=item.rejected_item_count,
                    schema_version=item.schema_version,
                    created_at=item.created_at,
                )
                for item in tool_result
            ]
            return ConversationDetailResponse(
                id=conversation.id,
                title=conversation.title,
                model_id=conversation.model_id,
                planning_source=conversation.planning_source,
                created_at=conversation.created_at,
                updated_at=conversation.updated_at,
                messages=messages,
                tool_calls=tool_calls,
            )

    async def delete_conversation(
        self,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            deleted_id = await session.scalar(
                delete(Conversation)
                .where(
                    Conversation.id == conversation_id,
                    Conversation.user_id == user_id,
                )
                .returning(Conversation.id)
            )
            if deleted_id is None:
                raise ConversationNotFoundError(str(conversation_id))

    async def start_turn(
        self,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
        model_id: str,
        user_content: str,
        planning_source: PlanningSource = "standard",
    ) -> TurnContext:
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            history: list[Message] = []
            if conversation_id is None:
                conversation = Conversation(
                    user_id=user_id,
                    title=_conversation_title(user_content),
                    model_id=model_id,
                    planning_source=planning_source,
                    created_at=now,
                    updated_at=now,
                )
                session.add(conversation)
                await session.flush()
                next_sequence = 1
            else:
                conversation = await session.scalar(
                    select(Conversation)
                    .where(
                        Conversation.id == conversation_id,
                        Conversation.user_id == user_id,
                    )
                    .with_for_update()
                )
                if conversation is None:
                    raise ConversationNotFoundError(str(conversation_id))
                history_result = await session.scalars(
                    select(Message)
                    .where(
                        Message.conversation_id == conversation_id,
                        or_(
                            and_(Message.role == "user", Message.status == "completed"),
                            and_(Message.role == "assistant", Message.status == "completed"),
                        ),
                    )
                    .order_by(Message.sequence.desc())
                    .limit(100)
                )
                history = list(reversed(list(history_result)))
                max_sequence = await session.scalar(
                    select(func.max(Message.sequence)).where(
                        Message.conversation_id == conversation_id
                    )
                )
                next_sequence = (max_sequence or 0) + 1
                conversation.model_id = model_id
                conversation.planning_source = planning_source
                conversation.updated_at = now

            user_message = Message(
                conversation_id=conversation.id,
                sequence=next_sequence,
                role="user",
                content=user_content,
                status="completed",
                created_at=now,
            )
            assistant_message = Message(
                conversation_id=conversation.id,
                sequence=next_sequence + 1,
                role="assistant",
                content="",
                status="streaming",
                debug_trace_json=[],
                created_at=now,
            )
            session.add_all([user_message, assistant_message])
            await session.flush()

            llm_messages = [
                ChatMessage(role=item.role, content=item.content)
                for item in history
                if item.content
            ]
            llm_messages.append(ChatMessage(role="user", content=user_content))
            return TurnContext(
                conversation_id=conversation.id,
                conversation_title=conversation.title,
                assistant_message_id=assistant_message.id,
                messages=llm_messages,
            )

    async def finish_turn(
        self,
        assistant_message_id: uuid.UUID,
        content: str,
        message_status: str,
        debug_trace: list[dict[str, object]] | None = None,
    ) -> None:
        now = datetime.now(UTC)
        values: dict[str, object] = {
            "content": content,
            "status": message_status,
        }
        if debug_trace is not None:
            values["debug_trace_json"] = debug_trace
        async with self._session_factory() as session, session.begin():
            conversation_id = await session.scalar(
                update(Message)
                .where(Message.id == assistant_message_id)
                .values(**values)
                .returning(Message.conversation_id)
            )
            if conversation_id is not None:
                await session.execute(
                    update(Conversation)
                    .where(Conversation.id == conversation_id)
                    .values(updated_at=now)
                )
