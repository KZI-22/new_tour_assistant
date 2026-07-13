from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Conversation, Message
from app.schemas.chat import ChatMessage
from app.schemas.conversation import (
    ConversationDetailResponse,
    ConversationMessageResponse,
    ConversationSummaryResponse,
)


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

    async def list_conversations(self) -> list[ConversationSummaryResponse]:
        async with self._session_factory() as session:
            result = await session.scalars(
                select(Conversation).order_by(Conversation.updated_at.desc()).limit(100)
            )
            return [
                ConversationSummaryResponse(
                    id=item.id,
                    title=item.title,
                    model_id=item.model_id,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                )
                for item in result
            ]

    async def get_conversation(self, conversation_id: uuid.UUID) -> ConversationDetailResponse:
        async with self._session_factory() as session:
            conversation = await session.get(Conversation, conversation_id)
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
                    created_at=item.created_at,
                )
                for item in result
            ]
            return ConversationDetailResponse(
                id=conversation.id,
                title=conversation.title,
                model_id=conversation.model_id,
                created_at=conversation.created_at,
                updated_at=conversation.updated_at,
                messages=messages,
            )

    async def delete_conversation(self, conversation_id: uuid.UUID) -> None:
        async with self._session_factory() as session, session.begin():
            deleted_id = await session.scalar(
                delete(Conversation)
                .where(Conversation.id == conversation_id)
                .returning(Conversation.id)
            )
            if deleted_id is None:
                raise ConversationNotFoundError(str(conversation_id))

    async def start_turn(
        self,
        conversation_id: uuid.UUID | None,
        model_id: str,
        user_content: str,
    ) -> TurnContext:
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            history: list[Message] = []
            if conversation_id is None:
                conversation = Conversation(
                    title=_conversation_title(user_content),
                    model_id=model_id,
                    created_at=now,
                    updated_at=now,
                )
                session.add(conversation)
                await session.flush()
                next_sequence = 1
            else:
                conversation = await session.scalar(
                    select(Conversation)
                    .where(Conversation.id == conversation_id)
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
    ) -> None:
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            conversation_id = await session.scalar(
                update(Message)
                .where(Message.id == assistant_message_id)
                .values(content=content, status=message_status)
                .returning(Message.conversation_id)
            )
            if conversation_id is not None:
                await session.execute(
                    update(Conversation)
                    .where(Conversation.id == conversation_id)
                    .values(updated_at=now)
                )

