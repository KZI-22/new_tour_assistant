from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from openai import AuthenticationError, RateLimitError

from app.api.dependencies import require_csrf_protection, require_current_user
from app.core.model_registry import (
    ModelRegistryError,
    UnavailableModelError,
    UnknownModelError,
)
from app.schemas.chat import ChatRequest, HealthResponse, ModelListResponse
from app.schemas.conversation import ConversationDetailResponse, ConversationSummaryResponse
from app.schemas.tool_execution import (
    ChatStreamEvent,
    MessageDeltaEvent,
    PlanningTraceEvent,
)
from app.schemas.travel_plan import TravelPlanDetailResponse
from app.services.agent_executor import AgentExecutionError
from app.services.auth_service import AuthenticatedUser
from app.services.conversation_service import ConversationNotFoundError, ConversationService
from app.services.tool_execution import ToolExecutionContext
from app.services.trip_plan_persistence_service import (
    TripPlanNotFoundError,
    TripPlanPersistenceService,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")


def _sse(event: str, payload: dict[str, object]) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n"


def _chat_event_sse(event: ChatStreamEvent | str) -> str:
    normalized = MessageDeltaEvent(delta=event) if isinstance(event, str) else event
    return _sse(normalized.type, normalized.model_dump(mode="json"))


def _event_text(event: ChatStreamEvent | str) -> str | None:
    if isinstance(event, str):
        return event
    if isinstance(event, MessageDeltaEvent):
        return event.delta
    return None


def _event_trace(event: ChatStreamEvent | str) -> dict[str, object] | None:
    if isinstance(event, PlanningTraceEvent):
        return event.model_dump(mode="json")
    return None


def _conversation_service(request: Request) -> ConversationService:
    service = request.app.state.conversation_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Conversation storage is not configured.",
        )
    return service


def _travel_plan_service(request: Request) -> TripPlanPersistenceService:
    service = request.app.state.trip_plan_persistence_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Travel plan storage is not configured.",
        )
    return service


async def _finish_safely(
    service: ConversationService,
    message_id: UUID,
    content: str,
    message_status: str,
    debug_trace: list[dict[str, object]] | None = None,
) -> None:
    try:
        await service.finish_turn(
            message_id,
            content,
            message_status,
            debug_trace=debug_trace,
        )
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
async def list_conversations(
    request: Request,
    response: Response,
    user: Annotated[AuthenticatedUser, Depends(require_current_user)],
) -> list[ConversationSummaryResponse]:
    conversations = await _conversation_service(request).list_conversations(user.id)
    response.headers["Cache-Control"] = "no-store"
    return conversations


@router.get("/conversations/{conversation_id}", response_model=ConversationDetailResponse)
async def get_conversation(
    conversation_id: UUID,
    request: Request,
    response: Response,
    user: Annotated[AuthenticatedUser, Depends(require_current_user)],
) -> ConversationDetailResponse:
    try:
        conversation = await _conversation_service(request).get_conversation(
            user.id,
            conversation_id,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        ) from exc
    response.headers["Cache-Control"] = "no-store"
    return conversation


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: UUID,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_current_user)],
    _: Annotated[str, Depends(require_csrf_protection)],
) -> Response:
    try:
        await _conversation_service(request).delete_conversation(user.id, conversation_id)
    except ConversationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/travel-plans/{plan_id}", response_model=TravelPlanDetailResponse)
async def get_travel_plan(
    plan_id: UUID,
    request: Request,
    response: Response,
    user: Annotated[AuthenticatedUser, Depends(require_current_user)],
    version: Annotated[int | None, Query(ge=1)] = None,
) -> TravelPlanDetailResponse:
    try:
        plan = await _travel_plan_service(request).get_plan(
            user.id,
            plan_id,
            version=version,
        )
    except TripPlanNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Travel plan not found.",
        ) from exc
    response.headers["Cache-Control"] = "private, no-store"
    return plan


@router.post("/chat/stream")
async def stream_chat(
    payload: ChatRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_current_user)],
    _: Annotated[str, Depends(require_csrf_protection)],
) -> StreamingResponse:
    conversation_service = _conversation_service(request)
    try:
        turn = await conversation_service.start_turn(
            user.id,
            payload.conversation_id,
            payload.model_id,
            payload.message,
            payload.planning_source,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        ) from exc
    except Exception as exc:
        logger.exception("Could not start persisted conversation turn")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Conversation storage is unavailable.",
        ) from exc

    service = request.app.state.chat_service
    heartbeat_seconds = request.app.state.settings.xhs_sse_heartbeat_seconds
    stream = None
    try:
        execution_context = ToolExecutionContext(
            conversation_id=turn.conversation_id,
            assistant_message_id=turn.assistant_message_id,
        )
        stream = service.stream(
            payload.model_id,
            turn.messages,
            planning_source=payload.planning_source,
            execution_context=execution_context,
        )
        first_event = await anext(stream, None)
    except asyncio.CancelledError:
        if stream is not None:
            await asyncio.shield(stream.aclose())
        await asyncio.shield(
            _finish_safely(
                conversation_service,
                turn.assistant_message_id,
                "",
                "interrupted",
            )
        )
        raise
    except UnknownModelError as exc:
        await _finish_safely(conversation_service, turn.assistant_message_id, "", "failed")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except UnavailableModelError as exc:
        await _finish_safely(conversation_service, turn.assistant_message_id, "", "failed")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except AgentExecutionError as exc:
        await _finish_safely(conversation_service, turn.assistant_message_id, "", "failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.user_message,
        ) from exc
    except Exception as exc:
        await _finish_safely(conversation_service, turn.assistant_message_id, "", "failed")
        logger.exception("Chat request failed before streaming")
        raise _provider_http_exception(exc) from exc

    async def events() -> AsyncIterator[str]:
        chunks: list[str] = []
        debug_trace: list[dict[str, object]] = []
        finalized = False
        next_event_task: asyncio.Task[ChatStreamEvent | str | None] | None = None
        try:
            yield _sse(
                "conversation",
                {
                    "id": str(turn.conversation_id),
                    "title": turn.conversation_title,
                    "planning_source": payload.planning_source,
                },
            )
            yield _sse(
                "message_start",
                {
                    "type": "message_start",
                    "message_id": str(turn.assistant_message_id),
                },
            )
            if first_event is not None:
                if text := _event_text(first_event):
                    chunks.append(text)
                if trace := _event_trace(first_event):
                    debug_trace.append(trace)
                yield _chat_event_sse(first_event)
            next_event_task = asyncio.create_task(anext(stream, None))
            while True:
                done, _ = await asyncio.wait(
                    {next_event_task},
                    timeout=heartbeat_seconds,
                )
                if not done:
                    yield ": heartbeat\n\n"
                    continue
                event = next_event_task.result()
                next_event_task = None
                if event is None:
                    break
                if text := _event_text(event):
                    chunks.append(text)
                if trace := _event_trace(event):
                    debug_trace.append(trace)
                yield _chat_event_sse(event)
                next_event_task = asyncio.create_task(anext(stream, None))
            await conversation_service.finish_turn(
                turn.assistant_message_id,
                "".join(chunks),
                "completed",
                debug_trace=debug_trace,
            )
            finalized = True
            yield _sse(
                "message_end",
                {
                    "type": "message_end",
                    "message_id": str(turn.assistant_message_id),
                    "status": "completed",
                },
            )
            yield _sse("done", {"conversation_id": str(turn.conversation_id)})
        except AgentExecutionError as exc:
            logger.warning("Agent execution stopped code=%s", exc.code)
            await _finish_safely(
                conversation_service,
                turn.assistant_message_id,
                "".join(chunks),
                "failed",
                debug_trace,
            )
            finalized = True
            yield _sse(
                "error",
                {
                    "type": "error",
                    "code": exc.code,
                    "message": exc.user_message,
                },
            )
        except Exception:
            logger.exception("Chat stream failed")
            await _finish_safely(
                conversation_service,
                turn.assistant_message_id,
                "".join(chunks),
                "failed",
                debug_trace,
            )
            finalized = True
            yield _sse(
                "error",
                {
                    "type": "error",
                    "code": "MODEL_STREAM_INTERRUPTED",
                    "message": "The model provider interrupted the response.",
                },
            )
        finally:
            if next_event_task is not None and not next_event_task.done():
                next_event_task.cancel()
                with suppress(asyncio.CancelledError):
                    await next_event_task
            if stream is not None:
                with suppress(Exception):
                    await stream.aclose()
            if not finalized:
                await asyncio.shield(
                    _finish_safely(
                        conversation_service,
                        turn.assistant_message_id,
                        "".join(chunks),
                        "interrupted",
                        debug_trace,
                    )
                )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
