from __future__ import annotations

import json
import logging
from collections.abc import Callable

from langchain_core.language_models import BaseChatModel

from app.schemas.agent_runtime import (
    AgentRunSnapshot,
    SupervisorDecision,
    SupervisorTask,
)
from app.schemas.chat import ChatMessage
from app.services.structured_output_service import (
    StructuredOutputError,
    StructuredOutputService,
)

logger = logging.getLogger(__name__)

_MAX_CONTEXT_MESSAGES = 10
_MAX_MESSAGE_CHARS = 4_000

SUPERVISOR_SYSTEM_PROMPT = """你是旅游助手的主 Agent，只负责任务判断和协调，不调用任何业务工具。

判断当前请求应如何处理：
- direct：问候、闲聊、简单知识问答，且不需要实时旅行数据。
- delegate：拆给一个或多个专业 Agent。

专业 Agent 的唯一职责边界：
- itinerary：多日攻略、景点详情、门票/讲解/演出/旅游商品、天气。
- transport：航班、火车、高铁及交通方式比较。
- hotel：酒店与住宿查询。

每个专业 Agent 最多一个自然语言任务。组合请求必须拆成多个可并行任务。把最近对话中已确认且与任务
有关的信息自然地写进 instruction，但不要替专业 Agent 判断工具必填字段。专业 Agent 之间不能通信。
不要输出回答正文；只返回结构化决策。"""


class SupervisorAgent:
    def __init__(
        self,
        model_factory: Callable[[str], BaseChatModel],
        *,
        timeout_seconds: float,
    ) -> None:
        self._model_factory = model_factory
        self._timeout_seconds = timeout_seconds

    async def decide(
        self,
        model_id: str,
        messages: list[ChatMessage],
    ) -> SupervisorDecision:
        model = self._model_factory(model_id)
        service = StructuredOutputService(model)
        try:
            return await service.invoke(
                SupervisorDecision,
                SUPERVISOR_SYSTEM_PROMPT,
                _conversation_prompt(messages),
                timeout_seconds=self._timeout_seconds,
            )
        except StructuredOutputError as exc:
            logger.warning(
                "Supervisor structured output failed; using deterministic fallback "
                "exception_type=%s",
                type(exc).__name__,
            )
            return fallback_decision(messages)


def resume_decision(
    pending: AgentRunSnapshot,
    latest_user_message: str,
) -> SupervisorDecision:
    tasks = [
        SupervisorTask(
            agent=task.agent,
            task_id=task.id,
            instruction=(
                f"{task.instruction}\n\n用户针对缺失信息的最新补充：{latest_user_message.strip()}"
            ),
        )
        for task in pending.tasks
        if task.status in {"needs_input", "partial", "waiting"}
    ]
    return SupervisorDecision(mode="resume", tasks=tasks)


def fallback_decision(messages: list[ChatMessage]) -> SupervisorDecision:
    latest = next(
        (message.content.strip() for message in reversed(messages) if message.role == "user"),
        "",
    )
    normalized = latest.casefold()
    tasks: list[SupervisorTask] = []

    itinerary_markers = (
        "攻略",
        "行程",
        "规划",
        "景点",
        "门票",
        "天气",
        "怎么玩",
        "travel plan",
        "itinerary",
        "weather",
        "attraction",
        "ticket",
    )
    transport_markers = (
        "航班",
        "飞机",
        "火车",
        "高铁",
        "动车",
        "交通",
        "flight",
        "train",
        "transport",
    )
    hotel_markers = ("酒店", "住宿", "民宿", "hotel", "accommodation")

    if any(marker in normalized for marker in itinerary_markers):
        tasks.append(SupervisorTask(agent="itinerary", instruction=latest))
    if any(marker in normalized for marker in transport_markers):
        tasks.append(SupervisorTask(agent="transport", instruction=latest))
    if any(marker in normalized for marker in hotel_markers):
        tasks.append(SupervisorTask(agent="hotel", instruction=latest))
    if not tasks:
        return SupervisorDecision(mode="direct")
    return SupervisorDecision(mode="delegate", tasks=tasks)


def _conversation_prompt(messages: list[ChatMessage]) -> str:
    recent = [
        {
            "role": message.role,
            "content": message.content.strip()[:_MAX_MESSAGE_CHARS],
        }
        for message in messages[-_MAX_CONTEXT_MESSAGES:]
        if message.role in {"user", "assistant"} and message.content.strip()
    ]
    return json.dumps({"recent_messages": recent}, ensure_ascii=False)


__all__ = [
    "SUPERVISOR_SYSTEM_PROMPT",
    "SupervisorAgent",
    "fallback_decision",
    "resume_decision",
]
