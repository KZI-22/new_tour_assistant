from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Protocol

from langchain_core.messages import AIMessage, BaseMessage

from app.schemas.tool_execution import ChatStreamEvent, MessageDeltaEvent
from app.services.tool_execution import ToolExecutionContext, ToolExecutor

MAX_TOOL_ROUNDS = 5
_TEXT_CHUNK_SIZE = 80
logger = logging.getLogger(__name__)


class ToolEnabledModel(Protocol):
    async def ainvoke(self, input: list[BaseMessage]) -> BaseMessage: ...


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
            response = await self._model.ainvoke(context_messages)
            if not isinstance(response, AIMessage):
                raise AgentExecutionError(
                    "MODEL_RESPONSE_INVALID",
                    "模型返回了无法处理的响应，请更换模型或稍后重试。",
                )

            if not response.tool_calls:
                text = _message_text(response)
                if not text:
                    logger.warning(
                        "Model returned empty final response additional_fields=%s "
                        "metadata_fields=%s",
                        sorted(response.additional_kwargs),
                        sorted(response.response_metadata),
                    )
                    if not retried_empty_response:
                        retried_empty_response = True
                        continue
                    raise AgentExecutionError(
                        "MODEL_EMPTY_RESPONSE",
                        "模型没有返回有效内容，请稍后重试。",
                    )
                for chunk in _split_text(text):
                    yield MessageDeltaEvent(delta=chunk)
                return

            if tool_rounds >= self._max_tool_rounds:
                raise ToolLoopLimitError()

            retried_empty_response = False
            context_messages.append(response)
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


def _split_text(value: str) -> list[str]:
    return [
        value[index : index + _TEXT_CHUNK_SIZE] for index in range(0, len(value), _TEXT_CHUNK_SIZE)
    ]


__all__ = [
    "AgentExecutionError",
    "AgentExecutor",
    "MAX_TOOL_ROUNDS",
    "ToolEnabledModel",
    "ToolLoopLimitError",
]
