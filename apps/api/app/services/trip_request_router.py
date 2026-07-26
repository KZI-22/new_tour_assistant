from __future__ import annotations

import asyncio
import logging
from typing import Literal

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict

from app.core.model_registry import ModelRegistry
from app.schemas.chat import ChatMessage
from app.schemas.routing import ResolvedTripRoute, TripRouteDecision

logger = logging.getLogger(__name__)

_MAX_CONTEXT_MESSAGES = 8
_MAX_MESSAGE_CHARS = 4_000
_MAX_ROUTE_ATTEMPTS = 2

ROUTER_SYSTEM_PROMPT = """你只负责判断用户是否正在创建或修改多日旅游计划，
不回答用户问题，不调用工具。

可选链路只有两条：
- general_agent：普通聊天、旅游知识问答，以及单项航班、火车、酒店、天气、POI、
  路线查询。单项推荐或创作文案也进入此链路。
- trip_planner：创建、生成、安排、修改或重新规划一个城市的多日旅游攻略。
  这里只判断规划意图，不决定使用地图、天气或小红书等哪一种资料来源。

必须结合最近对话理解省略表达，不能只看最新一句：
- 对目标城市或游玩天数的简短补充、对刚生成攻略的调整进入 trip_planner。
- 仅查询单项旅行数据、且核心交付物不是攻略时进入 general_agent。
- 混合请求只要核心交付物是攻略，就进入 trip_planner。
- “亲子、美食、轻松”等偏好不会改变链路。

只输出一个 JSON 对象，不要输出 Markdown、代码围栏、解释、理由或额外字段。
允许的精确格式只有以下两种：
{"route":"general_agent"}
{"route":"trip_planner"}
字段名必须是 route，字段值必须是 general_agent 或 trip_planner。"""

_ROUTER_RETRY_INSTRUCTION = """

上一次输出未通过 JSON 格式或字段校验。请重新判断，并严格按照系统消息规定的精确 JSON 格式输出。"""


class RouteMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str


class RouteContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recent_messages: list[RouteMessage]
    latest_user_message: str


class TripRequestRouter:
    def __init__(self, registry: ModelRegistry) -> None:
        self._registry = registry

    async def route(self, messages: list[ChatMessage]) -> ResolvedTripRoute:
        try:
            context = build_route_context(messages)
            model, timeout_seconds = self._registry.create_router_model()
        except Exception as exc:
            logger.warning(
                "Trip request router setup failed; using general agent fallback "
                "exception_type=%s",
                type(exc).__name__,
            )
            return fallback_route()

        last_error: Exception | None = None
        for attempt_index in range(_MAX_ROUTE_ATTEMPTS):
            user_prompt = context.model_dump_json()
            if attempt_index > 0:
                user_prompt += _ROUTER_RETRY_INSTRUCTION
            try:
                response = await asyncio.wait_for(
                    model.ainvoke(
                        [
                            SystemMessage(content=ROUTER_SYSTEM_PROMPT),
                            HumanMessage(content=user_prompt),
                        ]
                    ),
                    timeout=timeout_seconds,
                )
                decision = parse_route_decision(response)
                return ResolvedTripRoute(
                    **decision.model_dump(),
                    source="llm_router",
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                logger.info(
                    "Trip request router attempt failed attempt=%d max_attempts=%d "
                    "will_retry=%s exception_type=%s",
                    attempt_index + 1,
                    _MAX_ROUTE_ATTEMPTS,
                    str(attempt_index + 1 < _MAX_ROUTE_ATTEMPTS).lower(),
                    type(exc).__name__,
                )

        assert last_error is not None
        logger.warning(
            "Trip request router failed after retry; using general agent fallback "
            "exception_type=%s",
            type(last_error).__name__,
        )
        return fallback_route()


def parse_route_decision(response: BaseMessage) -> TripRouteDecision:
    if not isinstance(response.content, str):
        raise ValueError("router model response content must be text")
    return TripRouteDecision.model_validate_json(response.content)


def build_route_context(messages: list[ChatMessage]) -> RouteContext:
    effective_messages = [
        RouteMessage(
            role=message.role,
            content=message.content.strip()[:_MAX_MESSAGE_CHARS],
        )
        for message in messages
        if message.role in {"user", "assistant"} and message.content.strip()
    ]
    recent_messages = effective_messages[-_MAX_CONTEXT_MESSAGES:]
    latest_user_message = next(
        (message.content for message in reversed(effective_messages) if message.role == "user"),
        "",
    )
    return RouteContext(
        recent_messages=recent_messages,
        latest_user_message=latest_user_message,
    )


def fallback_route() -> ResolvedTripRoute:
    return ResolvedTripRoute(
        route="general_agent",
        source="fallback",
    )


__all__ = [
    "ROUTER_SYSTEM_PROMPT",
    "RouteContext",
    "RouteMessage",
    "TripRequestRouter",
    "build_route_context",
    "fallback_route",
    "parse_route_decision",
]
