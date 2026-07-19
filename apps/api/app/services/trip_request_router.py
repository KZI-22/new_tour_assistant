from __future__ import annotations

import asyncio
import logging
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict

from app.core.model_registry import ModelRegistry
from app.schemas.chat import ChatMessage
from app.schemas.routing import ResolvedTripRoute, TripRouteDecision

logger = logging.getLogger(__name__)

_MAX_CONTEXT_MESSAGES = 8
_MAX_MESSAGE_CHARS = 4_000

ROUTER_SYSTEM_PROMPT = """你只负责选择下一条执行链路，不回答用户问题，不调用工具。

可选链路只有两条：
- general_agent：普通聊天、旅游知识问答，以及单项航班、火车、酒店、天气、POI、
  路线查询。单项推荐或创作文案也进入此链路。
- xhs_trip_planner：创建、生成、安排、修改或重新规划一个城市的多日旅游攻略。
  该链路只需要目标城市和游玩天数，不查询或编排实时机票、火车票、酒店库存和价格。

必须结合最近对话理解省略表达，不能只看最新一句：
- 对目标城市或游玩天数的简短补充、对刚生成攻略的调整进入 xhs_trip_planner。
- 仅查询单项旅行数据、且核心交付物不是攻略时进入 general_agent。
- 混合请求只要核心交付物是攻略，就进入 xhs_trip_planner；附带的实时机酒查询本轮不执行。
- “亲子、美食、轻松”等偏好不会改变链路。

只输出符合 TripRouteDecision 的严格结构化结果。"""


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
            structured_model = model.with_structured_output(TripRouteDecision)
            raw_decision = await asyncio.wait_for(
                structured_model.ainvoke(
                    [
                        SystemMessage(content=ROUTER_SYSTEM_PROMPT),
                        HumanMessage(content=context.model_dump_json()),
                    ]
                ),
                timeout=timeout_seconds,
            )
            decision = (
                raw_decision
                if isinstance(raw_decision, TripRouteDecision)
                else TripRouteDecision.model_validate(raw_decision)
            )
            return ResolvedTripRoute(
                **decision.model_dump(),
                source="llm_router",
            )
        except Exception as exc:
            logger.warning(
                "Trip request router failed; using general agent fallback exception_type=%s",
                type(exc).__name__,
            )
            return fallback_route()


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
]
