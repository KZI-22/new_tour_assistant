from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import timedelta
from typing import Literal

from app.schemas.chat import ChatMessage
from app.schemas.trip_capabilities import (
    CapabilityAction,
    CapabilityPlan,
    HotelCapabilityPlan,
    HotelIntent,
    JourneyScope,
    MissingRequirement,
    RequirementCheck,
    TransportCapabilityPlan,
    TransportIntent,
    TransportMode,
    TripPlanningRequest,
    ValueDerivation,
)
from app.services.city_trip_request import validate_city_trip_request

CapabilityName = Literal["transport", "hotel"]
InstructionSource = Literal["explicit_user_input", "conversation_context"]

_QUERY_MARKERS = (
    "查",
    "查询",
    "查找",
    "看看",
    "看一下",
    "搜索",
    "搜一下",
    "推荐",
    "比较",
    "对比",
    "加入",
    "列出",
    "提供",
)
_DISABLE_MARKERS = (
    "不用查",
    "不要查",
    "别查",
    "不查",
    "无需查",
    "不用搜",
    "不需要",
    "已经订",
    "已订",
    "订好",
    "已经买",
    "已买",
    "买好",
)
_TRANSPORT_MARKERS = (
    "城际交通",
    "往返交通",
    "交通方案",
    "机票",
    "航班",
    "飞机",
    "火车",
    "高铁",
    "动车",
    "车票",
    "班次",
    "票务",
)
_FLIGHT_MARKERS = ("机票", "航班", "飞机")
_TRAIN_MARKERS = ("火车", "高铁", "动车", "车票")
_MODE_DISABLE_PREFIXES = ("不要", "不用", "别", "不坐", "不乘", "不考虑", "排除")
_HOTEL_MARKERS = ("酒店", "住宿", "民宿")
_ONE_WAY_MARKERS = ("单程", "只查去程", "不用返程", "不要返程")
_ROUND_TRIP_MARKERS = ("往返", "返程")
_CLAUSE_SPLIT = re.compile(
    r"[，,。；;！？!?]+|(?:但是|不过|然而|顺便|然后)|只(?=(?:查|看|搜|推荐|比较|对比))"
)


def resolve_capabilities(
    request: TripPlanningRequest,
    messages: Sequence[ChatMessage],
) -> CapabilityPlan:
    transport_action, transport_text, transport_source = _effective_instruction(
        messages,
        capability="transport",
        intent=request.transport,
    )
    hotel_action, hotel_text, hotel_source = _effective_instruction(
        messages,
        capability="hotel",
        intent=request.hotel,
    )

    transport, transport_derivations = _resolve_transport(
        request,
        action=transport_action,
        instruction_text=transport_text,
        instruction_source=transport_source,
    )
    hotel, hotel_derivations = _resolve_hotel(
        request,
        action=hotel_action,
        instruction_source=hotel_source,
    )
    return CapabilityPlan(
        transport=transport,
        hotel=hotel,
        derivations=[*transport_derivations, *hotel_derivations],
    )


def check_requirements(
    request: TripPlanningRequest,
    plan: CapabilityPlan,
    *,
    maximum_days: int,
) -> RequirementCheck:
    missing_fields, core_errors = validate_city_trip_request(
        request.core,
        maximum_days=maximum_days,
    )
    core_labels = {
        "destination_city": "目标城市",
        "duration_days": "游玩天数",
        "start_date": "出行开始日期",
    }
    missing = [
        MissingRequirement(
            field=f"core.{field}",
            capability="core",
            display_name=core_labels[field],
            reason="地图与天气行程需要该信息",
        )
        for field in missing_fields
    ]
    errors = list(core_errors)

    if plan.transport.enabled:
        if not plan.transport.modes:
            missing.append(
                MissingRequirement(
                    field="transport.modes",
                    capability="transport",
                    display_name="要查询的交通方式",
                    reason="请指定飞机、火车，或明确要求比较两者",
                )
            )
        if not plan.transport.origin:
            missing.append(
                MissingRequirement(
                    field="transport.origin",
                    capability="transport",
                    display_name="交通出发城市",
                    reason="城际交通查询需要出发城市",
                )
            )
        if (
            plan.transport.outbound_date is not None
            and plan.transport.return_date is not None
            and plan.transport.return_date < plan.transport.outbound_date
        ):
            errors.append("返程日期不能早于去程日期。")

    if (
        plan.hotel.enabled
        and plan.hotel.check_in_date is not None
        and plan.hotel.check_out_date is not None
        and plan.hotel.check_in_date >= plan.hotel.check_out_date
    ):
        errors.append("酒店入住日期必须早于退房日期。")

    return RequirementCheck(
        complete=not missing and not errors,
        missing=missing,
        errors=errors,
    )


def render_requirement_clarification(check: RequirementCheck) -> str:
    parts = list(check.errors)
    labels = list(dict.fromkeys(item.display_name for item in check.missing))
    if labels:
        parts.append(f"请一次补充：{'、'.join(labels)}。")
    return " ".join(parts) or "规划信息已完整。"


def _effective_instruction(
    messages: Sequence[ChatMessage],
    *,
    capability: CapabilityName,
    intent: TransportIntent | HotelIntent,
) -> tuple[CapabilityAction, str | None, InstructionSource | None]:
    user_messages = [message.content.strip() for message in messages if message.role == "user"]
    latest = user_messages[-1] if user_messages else ""

    current_action = classify_capability_instruction(latest, capability)
    if current_action is not CapabilityAction.UNSPECIFIED:
        return current_action, latest, "explicit_user_input"

    evidence = (intent.evidence_text or "").strip()
    if (
        intent.action is not CapabilityAction.UNSPECIFIED
        and evidence
        and evidence in latest
        and classify_capability_instruction(evidence, capability) is intent.action
    ):
        return intent.action, evidence, "explicit_user_input"

    for text in reversed(user_messages[:-1]):
        action = classify_capability_instruction(text, capability)
        if action is not CapabilityAction.UNSPECIFIED:
            return action, text, "conversation_context"
    return CapabilityAction.UNSPECIFIED, None, None


def classify_capability_instruction(
    text: str,
    capability: CapabilityName,
) -> CapabilityAction:
    action = CapabilityAction.UNSPECIFIED
    for clause in _capability_clauses(text, capability):
        clause_action = _classify_capability_clause(clause, capability)
        if clause_action is not CapabilityAction.UNSPECIFIED:
            action = clause_action
    return action


def extract_transport_modes(text: str) -> list[TransportMode]:
    enabled: dict[TransportMode, bool] = {}
    for clause in _capability_clauses(text, "transport"):
        modes = _raw_transport_modes(clause)
        if not modes:
            continue
        action = _classify_capability_clause(clause, "transport")
        for mode in modes:
            enabled[mode] = (
                action is not CapabilityAction.DISABLE
                and not _transport_mode_is_disabled(clause, mode)
            )
    return [
        mode for mode in (TransportMode.FLIGHT, TransportMode.TRAIN) if enabled.get(mode, False)
    ]


def extract_journey_scope(text: str) -> JourneyScope:
    scope = JourneyScope.UNSPECIFIED
    for clause in _capability_clauses(text, "transport"):
        if _contains_any(clause, _ONE_WAY_MARKERS):
            scope = JourneyScope.ONE_WAY
        elif _contains_any(clause, _ROUND_TRIP_MARKERS):
            scope = JourneyScope.ROUND_TRIP
    return scope


def _classify_capability_clause(
    text: str,
    capability: CapabilityName,
) -> CapabilityAction:
    markers = _TRANSPORT_MARKERS if capability == "transport" else _HOTEL_MARKERS
    if not _contains_any(text, markers):
        return CapabilityAction.UNSPECIFIED
    has_disable = _contains_any(text, _DISABLE_MARKERS)
    if capability == "transport":
        has_disable = has_disable or any(
            _transport_mode_is_disabled(text, mode)
            for mode in (TransportMode.FLIGHT, TransportMode.TRAIN)
        )
    positive_text = text
    for marker in _DISABLE_MARKERS:
        positive_text = positive_text.replace(marker, "")
    if _contains_any(positive_text, _QUERY_MARKERS):
        return CapabilityAction.ENABLE
    if has_disable:
        return CapabilityAction.DISABLE
    return CapabilityAction.UNSPECIFIED


def _capability_clauses(text: str, capability: CapabilityName) -> list[str]:
    markers = _TRANSPORT_MARKERS if capability == "transport" else _HOTEL_MARKERS
    clauses: list[str] = []
    query_context = False
    for raw_clause in _CLAUSE_SPLIT.split(text):
        clause = raw_clause.strip()
        if not clause:
            continue
        positive_text = clause
        for marker in _DISABLE_MARKERS:
            positive_text = positive_text.replace(marker, "")
        if _contains_any(positive_text, _QUERY_MARKERS):
            query_context = True
        if not _contains_any(clause, markers):
            continue
        if (
            query_context
            and clause.startswith(("以及", "和", "并且", "还有"))
            and not _contains_any(clause, (*_QUERY_MARKERS, *_DISABLE_MARKERS))
        ):
            clause = f"查{clause}"
        clauses.append(clause)
    return clauses


def _resolve_transport(
    request: TripPlanningRequest,
    *,
    action: CapabilityAction,
    instruction_text: str | None,
    instruction_source: InstructionSource | None,
) -> tuple[TransportCapabilityPlan, list[ValueDerivation]]:
    if action is not CapabilityAction.ENABLE:
        reason = (
            "用户明确关闭交通查询"
            if action is CapabilityAction.DISABLE
            else "用户未明确要求交通查询"
        )
        return TransportCapabilityPlan(reason=reason), []

    intent = request.transport
    detected_modes = extract_transport_modes(instruction_text or "")
    modes = detected_modes or list(dict.fromkeys(intent.modes))
    if not modes and _contains_any(instruction_text or "", ("比较", "对比")):
        modes = [TransportMode.FLIGHT, TransportMode.TRAIN]

    journey_scope = intent.journey_scope
    derivations: list[ValueDerivation] = []
    if journey_scope is JourneyScope.UNSPECIFIED:
        explicit_scope = extract_journey_scope(instruction_text or "")
        if explicit_scope is JourneyScope.ONE_WAY:
            journey_scope = explicit_scope
            scope_source: Literal["explicit_user_input", "default_policy"] = "explicit_user_input"
            explanation = "用户明确要求单程"
        elif explicit_scope is JourneyScope.ROUND_TRIP:
            journey_scope = explicit_scope
            scope_source = "explicit_user_input"
            explanation = "用户明确要求往返"
        else:
            journey_scope = JourneyScope.ROUND_TRIP
            scope_source = "default_policy"
            explanation = "未明确单双程时默认查询往返"
        derivations.append(
            ValueDerivation(
                field="transport.journey_scope",
                value=journey_scope.value,
                source=scope_source,
                explanation=explanation,
            )
        )

    outbound_date = intent.outbound_date or request.core.start_date
    if intent.outbound_date is None and outbound_date is not None:
        derivations.append(
            ValueDerivation(
                field="transport.outbound_date",
                value=outbound_date.isoformat(),
                source="derived_from_trip_dates",
                explanation="默认使用行程开始日期作为去程日期",
            )
        )

    return_date = None
    if journey_scope is JourneyScope.ROUND_TRIP:
        return_date = intent.return_date or _last_trip_date(request)
        if intent.return_date is None and return_date is not None:
            derivations.append(
                ValueDerivation(
                    field="transport.return_date",
                    value=return_date.isoformat(),
                    source="derived_from_trip_dates",
                    explanation="默认使用行程最后一天作为返程日期",
                )
            )

    if instruction_source is not None:
        derivations.append(
            ValueDerivation(
                field="transport.enabled",
                value="true",
                source=instruction_source,
                explanation="检测到用户明确交通查询指令",
            )
        )
    return (
        TransportCapabilityPlan(
            enabled=True,
            modes=modes,
            journey_scope=journey_scope,
            origin=intent.origin_city,
            destination=request.core.destination_city,
            outbound_date=outbound_date,
            return_date=return_date,
            max_price=intent.max_price,
            reason="用户明确要求交通查询",
        ),
        derivations,
    )


def _resolve_hotel(
    request: TripPlanningRequest,
    *,
    action: CapabilityAction,
    instruction_source: InstructionSource | None,
) -> tuple[HotelCapabilityPlan, list[ValueDerivation]]:
    if action is not CapabilityAction.ENABLE:
        reason = (
            "用户明确关闭酒店查询"
            if action is CapabilityAction.DISABLE
            else "用户未明确要求酒店查询"
        )
        return HotelCapabilityPlan(reason=reason), []

    intent = request.hotel
    check_in_date = intent.check_in_date or request.core.start_date
    check_out_date = intent.check_out_date or _hotel_check_out_date(request)
    derivations: list[ValueDerivation] = []
    if intent.check_in_date is None and check_in_date is not None:
        derivations.append(
            ValueDerivation(
                field="hotel.check_in_date",
                value=check_in_date.isoformat(),
                source="derived_from_trip_dates",
                explanation="默认使用行程开始日期作为入住日期",
            )
        )
    if intent.check_out_date is None and check_out_date is not None:
        derivations.append(
            ValueDerivation(
                field="hotel.check_out_date",
                value=check_out_date.isoformat(),
                source="derived_from_trip_dates",
                explanation="默认使用行程结束后的次日作为退房日期",
            )
        )
    if instruction_source is not None:
        derivations.append(
            ValueDerivation(
                field="hotel.enabled",
                value="true",
                source=instruction_source,
                explanation="检测到用户明确酒店查询指令",
            )
        )
    return (
        HotelCapabilityPlan(
            enabled=True,
            destination=request.core.destination_city,
            check_in_date=check_in_date,
            check_out_date=check_out_date,
            nearby_poi=intent.nearby_poi,
            keywords=intent.keywords,
            hotel_stars=intent.hotel_stars,
            max_nightly_price=intent.max_nightly_price,
            reason="用户明确要求酒店查询",
        ),
        derivations,
    )


def _raw_transport_modes(text: str) -> list[TransportMode]:
    modes: list[TransportMode] = []
    if _contains_any(text, _FLIGHT_MARKERS):
        modes.append(TransportMode.FLIGHT)
    if _contains_any(text, _TRAIN_MARKERS):
        modes.append(TransportMode.TRAIN)
    return modes


def _transport_mode_is_disabled(text: str, mode: TransportMode) -> bool:
    markers = _FLIGHT_MARKERS if mode is TransportMode.FLIGHT else _TRAIN_MARKERS
    return any(
        re.search(rf"{re.escape(prefix)}.{{0,3}}{re.escape(marker)}", text)
        for prefix in _MODE_DISABLE_PREFIXES
        for marker in markers
    )


def _last_trip_date(request: TripPlanningRequest):
    if request.core.start_date is None or request.core.duration_days is None:
        return None
    return request.core.start_date + timedelta(days=request.core.duration_days - 1)


def _hotel_check_out_date(request: TripPlanningRequest):
    if request.core.start_date is None or request.core.duration_days is None:
        return None
    return request.core.start_date + timedelta(days=request.core.duration_days)


def _contains_any(text: str, markers: Sequence[str]) -> bool:
    return any(marker in text for marker in markers)


__all__ = [
    "classify_capability_instruction",
    "check_requirements",
    "extract_journey_scope",
    "extract_transport_modes",
    "render_requirement_clarification",
    "resolve_capabilities",
]
