from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from openai import AuthenticationError, RateLimitError

from app.core.model_registry import (
    ModelRegistryError,
    UnavailableModelError,
    UnknownModelError,
)
from app.schemas.chat import ChatRequest, HealthResponse, ModelListResponse
from app.schemas.conversation import ConversationDetailResponse, ConversationSummaryResponse
from app.services.conversation_service import ConversationNotFoundError, ConversationService


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")


def _sse(event: str, payload: dict[str, object]) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n"


def _conversation_service(request: Request) -> ConversationService:
    service = request.app.state.conversation_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Conversation storage is not configured.",
        )
    return service


async def _finish_safely(
    service: ConversationService,
    message_id: UUID,
    content: str,
    message_status: str,
) -> None:
    try:
        await service.finish_turn(message_id, content, message_status)
    except Exception:
        logger.exception("Could not persist assistant message status")


def _provider_http_exception(exc: Exception) -> HTTPException:
    if isinstance(exc, AuthenticationError):
        return HTTPException(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            detail="The model provider rejected the configured API credentials or base URL.",
        )
    if isinstance(exc, RateLimitError):
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="The model provider rate limit was reached. Please try again later.",
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="The model provider could not start the response.",
    )


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse()


@router.get("/models", response_model=ModelListResponse)
async def list_models(request: Request) -> ModelListResponse:
    try:
        return request.app.state.model_registry.list_models()
    except ModelRegistryError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


@router.get("/conversations", response_model=list[ConversationSummaryResponse])
async def list_conversations(request: Request) -> list[ConversationSummaryResponse]:
    return await _conversation_service(request).list_conversations()


@router.get("/conversations/{conversation_id}", response_model=ConversationDetailResponse)
async def get_conversation(conversation_id: UUID, request: Request) -> ConversationDetailResponse:
    try:
        return await _conversation_service(request).get_conversation(conversation_id)
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.") from exc


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(conversation_id: UUID, request: Request) -> Response:
    try:
        await _conversation_service(request).delete_conversation(conversation_id)
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/chat/stream")
async def stream_chat(payload: ChatRequest, request: Request) -> StreamingResponse:
    conversation_service = _conversation_service(request)
    try:
        turn = await conversation_service.start_turn(
            payload.conversation_id,
            payload.model_id,
            payload.message,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.") from exc
    except Exception as exc:
        logger.exception("Could not start persisted conversation turn")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Conversation storage is unavailable.",
        ) from exc

    service = request.app.state.chat_service
    try:
        stream = service.stream(payload.model_id, turn.messages)
        first_chunk = await anext(stream, None)
    except UnknownModelError as exc:
        await _finish_safely(
            conversation_service, turn.assistant_message_id, "", "failed"
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except UnavailableModelError as exc:
        await _finish_safely(
            conversation_service, turn.assistant_message_id, "", "failed"
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Exception as exc:
        await _finish_safely(
            conversation_service, turn.assistant_message_id, "", "failed"
        )
        logger.exception("Chat request failed before streaming")
        raise _provider_http_exception(exc) from exc

    async def events() -> AsyncIterator[str]:
        chunks: list[str] = []
        finalized = False
        try:
            yield _sse(
                "conversation",
                {
                    "id": str(turn.conversation_id),
                    "title": turn.conversation_title,
                },
            )
            if first_chunk:
                chunks.append(first_chunk)
                yield _sse("token", {"delta": first_chunk})
            async for chunk in stream:
                chunks.append(chunk)
                yield _sse("token", {"delta": chunk})
            await conversation_service.finish_turn(
                turn.assistant_message_id,
                "".join(chunks),
                "completed",
            )
            finalized = True
            yield _sse("done", {"conversation_id": str(turn.conversation_id)})
        except Exception:
            logger.exception("Chat stream failed")
            await _finish_safely(
                conversation_service,
                turn.assistant_message_id,
                "".join(chunks),
                "failed",
            )
            finalized = True
            yield _sse(
                "error",
                {"message": "The model provider interrupted the response."},
            )
        finally:
            if not finalized:
                await asyncio.shield(
                    _finish_safely(
                        conversation_service,
                        turn.assistant_message_id,
                        "".join(chunks),
                        "interrupted",
                    )
                )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

