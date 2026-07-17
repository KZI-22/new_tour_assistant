from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict

from app.core.model_registry import ModelRegistry
from app.schemas.chat import ChatMessage
from app.schemas.itinerary import PlanningIntent
from app.schemas.routing import ResolvedTripRoute, TripRouteDecision
from app.services.trip_plan_service import StoredTripPlan

logger = logging.getLogger(__name__)

_MAX_CONTEXT_MESSAGES = 8
_MAX_MESSAGE_CHARS = 4_000

ROUTER_SYSTEM_PROMPT = """你只负责选择下一条执行链路，不回答用户问题，不调用工具。

可选链路：
- general_agent：普通聊天、旅游知识问答、单项航班/火车/酒店/天气/POI/路线查询、
  不写入行程的推荐或创作请求。
- trip_planner：创建、安排、修改或重新规划结构化行程；查询数据并明确加入行程；
  同时包含规划和单项查询的多意图请求。
- clarify：仅当普通查询与创建/修改持久化行程之间确实无法判断时使用。

必须结合最近对话和当前行程状态理解省略表达，不能只看最新一句：
- 草稿等待补充时，日期、天数、人数、预算、出发地或偏好等简短回复进入 trip_planner，
  trip_action_hint 使用 create，reason_code 使用 resume_draft。
- 仅查询旅行数据且没有要求写入行程时进入 general_agent。
- 查询后加入行程，或规划同时要求查询/创作时进入 trip_planner。
- 只有确实需要确认是否写入行程时才进入 clarify。

trip_action_hint 仅提示创建或修改：没有正式方案时不要坚持 modify。
clarify 时，查询还是写入行程使用 query_or_plan；新建还是修改使用 create_or_modify。
reason_code 应与链路一致：普通对话/单项查询、创建/修改/续接草稿/混合规划，
或无法判断是否持久化，分别选择对应枚举。
只输出符合 TripRouteDecision 的严格结构化结果。"""


class RouteMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str


class CurrentPlanRouteSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destination: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    has_formal_plan: bool


class RouteContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recent_messages: list[RouteMessage]
    latest_user_message: str
    has_current_plan: bool
    has_draft: bool
    stored_plan_status: str | None
    current_plan_summary: CurrentPlanRouteSummary | None


class TripRequestRouter:
    def __init__(self, registry: ModelRegistry) -> None:
        self._registry = registry

    async def route(
        self,
        messages: list[ChatMessage],
        *,
        stored: StoredTripPlan | None,
    ) -> ResolvedTripRoute:
        try:
            context = build_route_context(messages, stored=stored)
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
                "Trip request router failed; using general agent fallback "
                "exception_type=%s",
                type(exc).__name__,
            )
            return fallback_route()


def build_route_context(
    messages: list[ChatMessage],
    *,
    stored: StoredTripPlan | None,
) -> RouteContext:
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
        (
            message.content
            for message in reversed(effective_messages)
            if message.role == "user"
        ),
        "",
    )

    has_current_plan = bool(stored and stored.plan is not None)
    has_draft = bool(stored and stored.plan is None)
    summary = _plan_summary(stored)
    return RouteContext(
        recent_messages=recent_messages,
        latest_user_message=latest_user_message,
        has_current_plan=has_current_plan,
        has_draft=has_draft,
        stored_plan_status=stored.status if stored else None,
        current_plan_summary=summary,
    )


def resolve_planning_intent(
    route: TripRouteDecision,
    *,
    has_current_plan: bool,
    has_draft: bool,
) -> PlanningIntent | None:
    if route.route != "trip_planner":
        return None
    if has_draft or route.trip_action_hint == "create":
        return "new_trip_plan"
    if route.trip_action_hint == "modify" and has_current_plan:
        return "modify_trip_plan"
    if has_current_plan:
        return "modify_trip_plan"
    return "new_trip_plan"


def fallback_route() -> ResolvedTripRoute:
    return ResolvedTripRoute(
        route="general_agent",
        trip_action_hint="none",
        clarification_kind="none",
        reason_code="general_conversation",
        source="fallback",
    )


def _plan_summary(stored: StoredTripPlan | None) -> CurrentPlanRouteSummary | None:
    if stored is None:
        return None
    if stored.plan is not None:
        return CurrentPlanRouteSummary(
            destination=stored.plan.destination,
            start_date=stored.plan.start_date,
            end_date=stored.plan.end_date,
            has_formal_plan=True,
        )
    request = stored.request
    return CurrentPlanRouteSummary(
        destination=request.destinations[0] if request.destinations else None,
        start_date=request.start_date,
        end_date=request.end_date,
        has_formal_plan=False,
    )


__all__ = [
    "CurrentPlanRouteSummary",
    "ROUTER_SYSTEM_PROMPT",
    "RouteContext",
    "RouteMessage",
    "TripRequestRouter",
    "build_route_context",
    "fallback_route",
    "resolve_planning_intent",
]
