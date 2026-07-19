from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

TripRoute = Literal["general_agent", "trip_planner", "clarify"]
TripActionHint = Literal["none", "create", "modify"]
ClarificationKind = Literal[
    "none",
    "query_or_plan",
    "create_or_modify",
    "plan_or_query_first",
]
RouteReasonCode = Literal[
    "general_conversation",
    "single_travel_query",
    "create_trip",
    "modify_trip",
    "resume_draft",
    "mixed_with_planning",
    "ambiguous_persistence",
]

_REASONS_BY_ROUTE: dict[TripRoute, set[RouteReasonCode]] = {
    "general_agent": {"general_conversation", "single_travel_query"},
    "trip_planner": {
        "create_trip",
        "modify_trip",
        "resume_draft",
    },
    "clarify": {"ambiguous_persistence", "mixed_with_planning"},
}


class TripRouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: TripRoute
    trip_action_hint: TripActionHint = "none"
    clarification_kind: ClarificationKind = "none"
    reason_code: RouteReasonCode

    @model_validator(mode="after")
    def validate_field_combinations(self) -> Self:
        if self.route == "general_agent":
            if self.trip_action_hint != "none":
                raise ValueError("general_agent cannot include a trip action hint")
            if self.clarification_kind != "none":
                raise ValueError("general_agent cannot request route clarification")
        elif self.route == "trip_planner":
            if self.clarification_kind != "none":
                raise ValueError("trip_planner cannot request route clarification")
        else:
            if self.trip_action_hint != "none":
                raise ValueError("clarify cannot include a trip action hint")
            if self.clarification_kind == "none":
                raise ValueError("clarify requires a clarification kind")

        if self.reason_code not in _REASONS_BY_ROUTE[self.route]:
            raise ValueError("reason_code is inconsistent with route")
        return self


class ResolvedTripRoute(TripRouteDecision):
    source: Literal["llm_router", "fallback"]


_CLARIFICATION_MESSAGES: dict[ClarificationKind, str] = {
    "query_or_plan": "你是只想查询相关信息，还是希望把结果加入行程？",
    "create_or_modify": "你是想新建一份行程，还是修改当前已有的行程？",
    "plan_or_query_first": (
        "这个请求同时包含行程规划和单项查询。你想先生成城市行程，还是先查询机票、火车票或酒店信息？"
    ),
    "none": "",
}


def clarification_message(kind: ClarificationKind) -> str:
    message = _CLARIFICATION_MESSAGES[kind]
    if not message:
        raise ValueError("clarification kind 'none' has no user-facing message")
    return message


__all__ = [
    "ClarificationKind",
    "ResolvedTripRoute",
    "RouteReasonCode",
    "TripActionHint",
    "TripRoute",
    "TripRouteDecision",
    "clarification_message",
]
