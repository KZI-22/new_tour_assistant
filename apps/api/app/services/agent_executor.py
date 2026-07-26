from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Protocol

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage

from app.schemas.tool_execution import ChatStreamEvent, MessageDeltaEvent
from app.services.tool_execution import ToolExecutionContext, ToolExecutor

MAX_TOOL_ROUNDS = 5
logger = logging.getLogger(__name__)


class ToolEnabledModel(Protocol):
    def astream(self, input: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]: ...


class AgentExecutionError(RuntimeError):
    def __init__(self, code: str, user_message: str) -> None:
        super().__init__(user_message)
        self.code = code
        self.user_message = user_message


class ToolLoopLimitError(AgentExecutionError):
    def __init__(self) -> None:
        super().__init__(
            "TOOL_LOOP_LIMIT",
            "工具调用次数已达到上限，本次查询已停止。请缩小问题范围后重试。",
        )


class AgentExecutor:
    """Run one tool-enabled model until it returns a final natural-language answer."""

    def __init__(
        self,
        model: ToolEnabledModel,
        tool_executor: ToolExecutor,
        *,
        max_tool_rounds: int = MAX_TOOL_ROUNDS,
    ) -> None:
        if max_tool_rounds < 1:
            raise ValueError("max_tool_rounds must be at least 1")
        self._model = model
        self._tool_executor = tool_executor
        self._max_tool_rounds = max_tool_rounds

    async def stream(
        self,
        messages: list[BaseMessage],
        *,
        execution_context: ToolExecutionContext | None = None,
    ) -> AsyncIterator[ChatStreamEvent]:
        context_messages = list(messages)
        tool_rounds = 0
        retried_empty_response = False

        while True:
            response: AIMessageChunk | None = None
            streamed_text = False
            async for chunk in self._model.astream(context_messages):
                if not isinstance(chunk, AIMessageChunk):
                    raise AgentExecutionError(
                        "MODEL_RESPONSE_INVALID",
                        "模型返回了无法处理的响应，请更换模型或稍后重试。",
                    )
                response = chunk if response is None else response + chunk
                if text := _message_text(chunk):
                    streamed_text = True
                    yield MessageDeltaEvent(delta=text)

            if response is None or not response.tool_calls:
                if not streamed_text:
                    logger.warning(
                        "Model returned empty final response additional_fields=%s "
                        "metadata_fields=%s",
                        sorted(response.additional_kwargs) if response else [],
                        sorted(response.response_metadata) if response else [],
                    )
                    if not retried_empty_response:
                        retried_empty_response = True
                        continue
                    raise AgentExecutionError(
                        "MODEL_EMPTY_RESPONSE",
                        "模型没有返回有效内容，请稍后重试。",
                    )
                return

            if tool_rounds >= self._max_tool_rounds:
                raise ToolLoopLimitError()

            retried_empty_response = False
            context_messages.append(_to_ai_message(response))
            prepared = self._tool_executor.prepare_calls(
                [dict(tool_call) for tool_call in response.tool_calls],
                round_index=tool_rounds,
            )
            for call in prepared:
                yield call.event

            outcomes = await self._tool_executor.execute_many(
                prepared,
                context=execution_context,
            )
            for outcome in outcomes:
                context_messages.append(outcome.message)
                yield outcome.event
            tool_rounds += 1


def _to_ai_message(chunk: AIMessageChunk) -> AIMessage:
    """Convert an accumulated chunk so replayed history keeps the plain ``ai`` type."""
    return AIMessage(
        content=chunk.content,
        additional_kwargs=chunk.additional_kwargs,
        response_metadata=chunk.response_metadata,
        tool_calls=chunk.tool_calls,
        invalid_tool_calls=chunk.invalid_tool_calls,
        id=chunk.id,
    )


def _message_text(message: AIMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


__all__ = [
    "AgentExecutionError",
    "AgentExecutor",
    "MAX_TOOL_ROUNDS",
    "ToolEnabledModel",
    "ToolLoopLimitError",
]
