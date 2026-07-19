from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
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
- trip_planner：基于小红书内容创建、安排、修改或重新规划一个城市的多日旅游攻略。
  该链路只需要目标城市和游玩天数，不负责查询机票、火车票或酒店。
- clarify：请求同时包含旅游规划与机票/火车票/酒店等单项查询时，询问用户先做哪一项；
  或仅当普通查询与创建/修改行程之间确实无法判断时使用。

必须结合最近对话理解省略表达，不能只看最新一句：
- 对目标城市或游玩天数的简短补充、对刚生成攻略的调整进入 trip_planner。
- 仅查询旅行数据且没有要求写入行程时进入 general_agent。
- 同时要求规划以及查询机票、火车票或酒店时进入 clarify，clarification_kind 使用
  plan_or_query_first，reason_code 使用 mixed_with_planning。
- “亲子、美食、轻松”等偏好不会改变链路；只要核心诉求是生成多日城市行程，就进入
  trip_planner。

trip_action_hint 仅提示创建或修改：新攻略使用 create，对最近攻略的调整使用 modify。
clarify 时，混合请求使用 plan_or_query_first；查询还是写入行程使用 query_or_plan；
新建还是修改使用 create_or_modify。
reason_code 应与链路一致：普通对话/单项查询、创建/修改、混合规划或无法判断，分别选择
对应枚举。
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
        latest_user_message = next(
            (message.content for message in reversed(messages) if message.role == "user"),
            "",
        )
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
                "Trip request router failed; using deterministic fallback exception_type=%s",
                type(exc).__name__,
            )
            return fallback_route(latest_user_message, recent_messages=messages)


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
        (message.content for message in reversed(effective_messages) if message.role == "user"),
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


def fallback_route(
    latest_user_message: str = "",
    *,
    recent_messages: Sequence[ChatMessage] | None = None,
) -> ResolvedTripRoute:
    if _looks_like_mixed_planning_request(latest_user_message):
        return ResolvedTripRoute(
            route="clarify",
            trip_action_hint="none",
            clarification_kind="plan_or_query_first",
            reason_code="mixed_with_planning",
            source="fallback",
        )
    if _looks_like_plan_revision_follow_up(latest_user_message, recent_messages):
        return ResolvedTripRoute(
            route="trip_planner",
            trip_action_hint="modify",
            clarification_kind="none",
            reason_code="modify_trip",
            source="fallback",
        )
    if _looks_like_planning_clarification_reply(recent_messages):
        return ResolvedTripRoute(
            route="trip_planner",
            trip_action_hint="create",
            clarification_kind="none",
            reason_code="create_trip",
            source="fallback",
        )
    if _looks_like_trip_planning_request(latest_user_message):
        return ResolvedTripRoute(
            route="trip_planner",
            trip_action_hint="create",
            clarification_kind="none",
            reason_code="create_trip",
            source="fallback",
        )
    return ResolvedTripRoute(
        route="general_agent",
        trip_action_hint="none",
        clarification_kind="none",
        reason_code="general_conversation",
        source="fallback",
    )


def _looks_like_trip_planning_request(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    patterns = (
        r"(?:帮我|请帮我).{0,6}(?:规划|制定|安排)",
        (
            r"(?:规划|制定|安排).{0,16}(?:[零〇一二两三四五六七八九十\d]+\s*[天日]|"
            r"行程|旅行|旅游|攻略|旅|游)"
        ),
        r"(?:生成|做|写).{0,8}(?:攻略|行程)",
        r"[零〇一二两三四五六七八九十\d]+\s*[天日].{0,8}(?:游|旅|攻略|行程)",
        r"(?:修改|调整|重做|重新规划).{0,12}(?:攻略|行程|第[零一二三四五六七八九十\d]+天)",
    )
    return any(re.search(pattern, normalized) for pattern in patterns)


def _looks_like_mixed_planning_request(text: str) -> bool:
    if not _looks_like_trip_planning_request(text):
        return False
    query_target = r"(?:机票|航班|火车票|火车|高铁票|高铁|酒店|住宿)"
    query_action = r"(?:查|查询|搜索|价格|票价|余票|库存|预订|推荐)"
    return bool(
        re.search(rf"{query_action}.{{0,12}}{query_target}", text)
        or re.search(rf"{query_target}.{{0,12}}{query_action}", text)
    )


def _looks_like_planning_clarification_reply(
    messages: Sequence[ChatMessage] | None,
) -> bool:
    if not messages:
        return False
    previous_assistant = next(
        (message.content for message in reversed(messages[:-1]) if message.role == "assistant"),
        "",
    )
    markers = ("目标城市和游玩天数", "想去的目标城市", "准备游玩几天")
    return any(marker in previous_assistant for marker in markers)


def _looks_like_plan_revision_follow_up(
    text: str,
    messages: Sequence[ChatMessage] | None,
) -> bool:
    if not messages:
        return False
    has_recent_plan = any(
        message.role == "assistant"
        and ("参考的小红书笔记" in message.content or "## 第 1 天" in message.content)
        for message in messages[-8:-1]
    )
    if not has_recent_plan:
        return False
    return bool(
        re.search(
            r"(?:第[零〇一二两三四五六七八九十\d]+天|修改|调整|换成|删掉|增加|轻松|紧凑|不要)",
            text,
        )
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
