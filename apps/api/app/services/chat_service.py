from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from typing import cast

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool

from app.core.model_registry import ModelRegistry, UnavailableModelError
from app.core.request_context import get_request_context
from app.schemas.chat import ChatMessage
from app.schemas.tool_execution import ChatStreamEvent
from app.services.agent_executor import MAX_TOOL_ROUNDS, AgentExecutor, ToolEnabledModel
from app.services.tool_call_log_service import ToolCallLogWriter
from app.services.tool_execution import ToolExecutionContext, ToolExecutor

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = """你是旅游规划助手。请清晰、诚实地回答用户。

当问题涉及实时或外部数据时，必须优先调用已提供的工具，而不是依赖模型记忆，包括：
- 航班、火车和酒店
- 景点和 POI
- 天气、路线、距离与预计耗时
- 当前城市

当工具缺少不可可靠推测的必填参数时，先向用户追问，不要编造参数：
- 航班和火车至少需要出发地、目的地与日期
- 酒店至少需要城市、入住日期与退房日期
- 路线至少需要起点与终点
相对日期可以结合系统提供的当前日期和时区解释。
只问“今天天气”且没有城市时，可以先使用 IP 城市推测工具，
但必须明确说明定位结果只是城市级网络位置推测，不是精确位置。

工具执行失败时，如实说明失败；可以使用同一轮中其他成功结果继续回答，
但不得伪造查询结果或声称已经查到实际数据。
清楚区分实时查询结果与一般建议，并提醒价格、余票、天气和耗时可能变化。
不要向用户展示内部错误、API Key、环境变量、供应商原始响应、内部 URL、
命令、技术堆栈或本机路径。

普通问候、概念解释和创作文案等不需要外部数据的问题应直接回答，不要调用工具。
禁止声称已完成预订或任何未实际执行的外部操作。"""


def _to_langchain_messages(messages: list[ChatMessage]) -> list[BaseMessage]:
    prompt = DEFAULT_SYSTEM_PROMPT
    if request_context := get_request_context():
        current = request_context.time
        prompt = (
            f"{prompt}\n\n当前日期时间：{current.current_datetime.isoformat()}；"
            f"时区：{current.timezone}；星期：{current.weekday}。"
        )

    converted: list[BaseMessage] = [SystemMessage(content=prompt)]
    message_types = {
        "system": SystemMessage,
        "user": HumanMessage,
        "assistant": AIMessage,
    }
    converted.extend(message_types[item.role](content=item.content) for item in messages)
    return converted


class ChatService:
    def __init__(
        self,
        registry: ModelRegistry,
        tools: Sequence[BaseTool],
        *,
        max_tool_rounds: int = MAX_TOOL_ROUNDS,
        tool_timeout_seconds: float = 130,
        tool_call_log_writer: ToolCallLogWriter | None = None,
    ) -> None:
        self._registry = registry
        self._tools = tuple(tools)
        self._max_tool_rounds = max_tool_rounds
        self._tool_executor = ToolExecutor(
            self._tools,
            timeout_seconds=tool_timeout_seconds,
            log_writer=tool_call_log_writer,
        )

    async def stream(
        self,
        model_id: str,
        messages: list[ChatMessage],
        *,
        execution_context: ToolExecutionContext | None = None,
    ) -> AsyncIterator[ChatStreamEvent]:
        model = self._registry.create_model(model_id)
        try:
            bound_model = model.bind_tools(list(self._tools))
        except Exception as exc:
            logger.warning(
                "Model does not support tool calling model_id=%s exception_type=%s",
                model_id,
                type(exc).__name__,
            )
            raise UnavailableModelError(
                f"The selected model '{model_id}' does not support tool calling."
            ) from None

        executor = AgentExecutor(
            cast(ToolEnabledModel, bound_model),
            self._tool_executor,
            max_tool_rounds=self._max_tool_rounds,
        )
        async for event in executor.stream(
            _to_langchain_messages(messages),
            execution_context=execution_context,
        ):
            yield event


__all__ = ["ChatService", "DEFAULT_SYSTEM_PROMPT"]
