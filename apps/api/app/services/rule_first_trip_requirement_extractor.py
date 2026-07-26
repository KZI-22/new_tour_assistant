from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from datetime import date
from time import perf_counter
from typing import Literal

from langchain_core.language_models import BaseChatModel
from pydantic import Field

from app.core.request_context import get_request_context
from app.data.china_city_names import CITY_NAME_PATTERN
from app.schemas.chat import ChatMessage
from app.schemas.trip_capabilities import (
    CapabilityAction,
    JourneyScope,
    TripPlanningRequest,
)
from app.schemas.trip_planning import CityTripRequest, TripPlanningModel, TripPreference
from app.services.capability_resolver import (
    classify_capability_instruction,
    extract_journey_scope,
    extract_transport_modes,
)
from app.services.city_trip_request import (
    explicit_dates,
    explicit_duration_candidates,
)
from app.services.structured_output_service import (
    StructuredOutputError,
    StructuredOutputService,
)

logger = logging.getLogger(__name__)

FieldSource = Literal["explicit_rule", "conversation_context", "llm_resolution", "derived"]
AmbiguousField = Literal[
    "core.destination_city",
    "core.duration_days",
    "core.start_date",
    "transport.origin_city",
    "hotel.nearby_poi",
]

_CITY = rf"(?:{CITY_NAME_PATTERN})"
_CITY_MENTION = re.compile(rf"(?P<city>{_CITY})(?:市)?")
_CITY_ROUTE = re.compile(
    rf"(?:从\s*)?(?P<origin>{_CITY})(?:市)?\s*(?:出发\s*)?"
    rf"(?:到|去|前往)\s*(?P<destination>{_CITY})(?:市)?"
)
_ALTERNATIVE_CITY_PAIR = re.compile(
    rf"(?P<first>{_CITY})(?:市)?\s*(?:或者|或是|还是|或|/)\s*"
    rf"(?P<second>{_CITY})(?:市)?"
)
_ALTERNATIVE_ROUTE_ORIGIN = re.compile(
    rf"(?:从\s*)?(?P<first>{_CITY})(?:市)?\s*(?:或者|或是|还是|或|/)\s*"
    rf"(?P<second>{_CITY})(?:市)?\s*(?:出发\s*)?(?:到|去|前往)\s*"
    rf"(?P<destination>{_CITY})(?:市)?"
)
_CITY_ORIGIN = re.compile(
    rf"(?:从|由)\s*(?P<origin>{_CITY})(?:市)?\s*(?:出发|启程|坐|乘)"
    rf"|(?P<bare_origin>{_CITY})(?:市)?\s*(?:出发|启程)"
)
_DIRECT_DESTINATION = re.compile(
    rf"(?:想去|要去|准备去|计划去|前往|目的地(?:是|定在)?|到|去|规划|安排|"
    rf"改成|改为|换成|换去)"
    rf"\s*(?P<city>{_CITY})(?:市)?"
)
_CITY_TRIP_SUFFIX = re.compile(
    rf"(?P<city>{_CITY})(?:市)?\s*"
    r"(?:[零〇一二两三四五六七八九十\d]{1,3}\s*[天日]|旅游|旅行|游|攻略|行程|度假|玩)"
)
_ALTERNATIVE_MARKER = re.compile(r"(?:或者|或是|还是|或|/)")
_CORRECTION_MARKER = re.compile(r"(?:不对|改成|改为|更正为|调整为|应该是|就去|还是去)")
_DESTINATION_PRONOUN = re.compile(r"(?:那里|那边|那个(?:城市|地方)|这里|这边|这个地方)")
_POI_PRONOUN = re.compile(r"(?:那里|那边|附近|那个(?:车站|地方|地标)|这里|这边)")
_CLAUSE_SPLIT = re.compile(r"[，,。；;！？!?]+")
_DATE_RANGE_MARKER = re.compile(r"(?:到|至|—|–|~|～)")
_HOTEL_NEARBY = re.compile(
    r"(?P<poi>[\u4e00-\u9fffA-Za-z0-9·]{2,24}?)(?:附近|周边)(?:的)?"
    r"(?:酒店|住宿|民宿)"
)
_HOTEL_STAR = re.compile(r"([一二三四五1-5])\s*星")
_PRICE = re.compile(
    r"(?:预算|不超过|不要超过|最多|上限|控制在)\s*(?:人民币|元|¥|￥)?\s*"
    r"(\d+(?:\.\d+)?)"
)
_TRIP_MARKERS = ("旅游", "旅行", "游", "攻略", "行程", "度假", "玩", "规划", "安排")
_HOTEL_MARKERS = ("酒店", "住宿", "民宿", "入住", "退房", "离店")
_TRANSPORT_MARKERS = (
    "交通",
    "机票",
    "航班",
    "飞机",
    "火车",
    "高铁",
    "动车",
    "车票",
    "去程",
    "返程",
)
_PREFERENCE_MARKERS: tuple[tuple[tuple[str, ...], TripPreference], ...] = (
    (("历史", "文化", "人文", "古迹", "园林"), TripPreference.HISTORY_CULTURE),
    (("博物馆", "展览", "美术馆"), TripPreference.MUSEUM_EXHIBITION),
    (("自然", "山水", "风光"), TripPreference.NATURAL_SCENERY),
    (("地标",), TripPreference.CITY_LANDMARK),
    (("街区", "步行街"), TripPreference.CHARACTERISTIC_DISTRICT),
    (("摄影", "打卡"), TripPreference.PHOTOGRAPHY),
    (("亲子", "儿童"), TripPreference.FAMILY),
    (("休闲", "慢游", "轻松"), TripPreference.LEISURE),
    (("夜景", "夜游"), TripPreference.NIGHT_VIEW),
)
_CHINESE_STAR = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}

_AMBIGUITY_SYSTEM_PROMPT = """你只负责消解代码已经检测到的旅行需求歧义。
不得补全缺失信息，不得改写不在歧义列表中的字段，不得猜测。
候选值存在时只能从候选值中选择；无法唯一确定时返回 null。
只输出符合指定 JSON Schema 的结构化结果。"""


class RequirementAmbiguity(TripPlanningModel):
    field: AmbiguousField
    kind: Literal["multiple_candidates", "pronoun", "conflict", "scope"]
    excerpt: str = Field(max_length=1_000)
    candidates: list[str] = Field(default_factory=list, max_length=20)


class CapabilityEvidence(TripPlanningModel):
    capability: Literal["transport", "hotel"]
    action: CapabilityAction
    evidence_text: str = Field(max_length=500)
    source: Literal["explicit_user_input", "conversation_context"]


class TripAmbiguityResolution(TripPlanningModel):
    destination_city: str | None = Field(default=None, max_length=80)
    duration_days: int | None = Field(default=None, ge=1)
    start_date: date | None = None
    transport_origin_city: str | None = Field(default=None, max_length=80)
    hotel_nearby_poi: str | None = Field(default=None, max_length=200)


class ExtractionMetrics(TripPlanningModel):
    path: Literal["rules", "hybrid"]
    rule_duration_ms: float
    llm_duration_ms: float
    llm_call_count: int
    llm_retry_count: int
    ambiguity_fields: list[str]


class RuleFirstExtractionResult(TripPlanningModel):
    request: TripPlanningRequest
    field_sources: dict[str, FieldSource] = Field(default_factory=dict)
    ambiguities: list[RequirementAmbiguity] = Field(default_factory=list)
    explicit_missing: list[str] = Field(default_factory=list)
    capability_evidence: list[CapabilityEvidence] = Field(default_factory=list)
    metrics: ExtractionMetrics


class _TurnExtraction:
    def __init__(self) -> None:
        self.destination_city: str | None = None
        self.duration_days: int | None = None
        self.start_date: date | None = None
        self.transport_origin_city: str | None = None
        self.transport_outbound_date: date | None = None
        self.transport_return_date: date | None = None
        self.hotel_check_in_date: date | None = None
        self.hotel_check_out_date: date | None = None
        self.hotel_nearby_poi: str | None = None
        self.ambiguities: list[RequirementAmbiguity] = []


class RuleFirstTripRequirementExtractor:
    def __init__(self, model: BaseChatModel, *, timeout_seconds: float) -> None:
        self._structured = StructuredOutputService(model)
        self._timeout_seconds = timeout_seconds

    async def extract(
        self,
        messages: Sequence[ChatMessage],
    ) -> RuleFirstExtractionResult:
        started = perf_counter()
        result = extract_trip_request_by_rules(messages)
        rule_duration_ms = (perf_counter() - started) * 1_000

        llm_duration_ms = 0.0
        attempts: list[Literal["native", "fallback"]] = []
        ambiguity_fields = list(dict.fromkeys(item.field for item in result.ambiguities))
        if result.ambiguities:
            llm_started = perf_counter()
            try:
                resolution = await self._structured.invoke(
                    TripAmbiguityResolution,
                    _AMBIGUITY_SYSTEM_PROMPT,
                    _ambiguity_prompt(messages, result.ambiguities),
                    timeout_seconds=self._timeout_seconds,
                    attempt_observer=attempts.append,
                )
            except StructuredOutputError:
                resolution = TripAmbiguityResolution()
            llm_duration_ms = (perf_counter() - llm_started) * 1_000
            _apply_ambiguity_resolution(result, resolution)

        result.explicit_missing = _explicit_missing(result.request)
        result.metrics = ExtractionMetrics(
            path="hybrid" if result.ambiguities else "rules",
            rule_duration_ms=round(rule_duration_ms, 3),
            llm_duration_ms=round(llm_duration_ms, 3),
            llm_call_count=len(attempts),
            llm_retry_count=max(0, len(attempts) - 1),
            ambiguity_fields=ambiguity_fields,
        )
        logger.info(
            "Trip requirement extraction completed path=%s rule_duration_ms=%.3f "
            "llm_duration_ms=%.3f llm_call_count=%d llm_retry_count=%d "
            "ambiguity_fields=%s field_sources=%s",
            result.metrics.path,
            result.metrics.rule_duration_ms,
            result.metrics.llm_duration_ms,
            result.metrics.llm_call_count,
            result.metrics.llm_retry_count,
            ",".join(ambiguity_fields) or "none",
            json.dumps(result.field_sources, ensure_ascii=False, sort_keys=True),
        )
        return result


def extract_trip_request_by_rules(
    messages: Sequence[ChatMessage],
) -> RuleFirstExtractionResult:
    user_messages = [
        message for message in messages if message.role == "user" and message.content.strip()
    ]
    request = TripPlanningRequest(core=CityTripRequest())
    field_sources: dict[str, FieldSource] = {}
    ambiguities: list[RequirementAmbiguity] = []
    capability_evidence: list[CapabilityEvidence] = []
    today = _current_date()

    for index, message in enumerate(user_messages):
        text = message.content.strip()
        source: FieldSource = (
            "explicit_rule" if index == len(user_messages) - 1 else "conversation_context"
        )
        turn = _extract_turn(text, today=today)
        turn.ambiguities = [
            item
            for item in turn.ambiguities
            if not (
                item.kind == "pronoun"
                and (
                    (
                        item.field == "core.destination_city"
                        and request.core.destination_city is not None
                    )
                    or (item.field == "hotel.nearby_poi" and request.hotel.nearby_poi is not None)
                )
            )
        ]
        _merge_turn(request, field_sources, ambiguities, turn, source=source)
        _merge_preferences(request.core, text, field_sources, source)

        for capability in ("transport", "hotel"):
            action = classify_capability_instruction(text, capability)
            if action is CapabilityAction.UNSPECIFIED:
                continue
            evidence_text = text[:500]
            if capability == "transport":
                request.transport.action = action
                request.transport.evidence_text = evidence_text
            else:
                request.hotel.action = action
                request.hotel.evidence_text = evidence_text
            field_sources[f"{capability}.action"] = source
            field_sources[f"{capability}.evidence_text"] = source
            capability_evidence = [
                item for item in capability_evidence if item.capability != capability
            ]
            capability_evidence.append(
                CapabilityEvidence(
                    capability=capability,
                    action=action,
                    evidence_text=evidence_text,
                    source=(
                        "explicit_user_input"
                        if source == "explicit_rule"
                        else "conversation_context"
                    ),
                )
            )

        modes = extract_transport_modes(text)
        if modes:
            request.transport.modes = modes
            field_sources["transport.modes"] = source
        scope = extract_journey_scope(text)
        if scope is not JourneyScope.UNSPECIFIED:
            request.transport.journey_scope = scope
            field_sources["transport.journey_scope"] = source
        _merge_prices_and_stars(request, text, field_sources, source)

    if (
        request.transport.action is CapabilityAction.ENABLE
        and request.transport.journey_scope is JourneyScope.UNSPECIFIED
    ):
        request.transport.journey_scope = JourneyScope.ROUND_TRIP
        field_sources["transport.journey_scope"] = "derived"

    return RuleFirstExtractionResult(
        request=request,
        field_sources=field_sources,
        ambiguities=ambiguities,
        explicit_missing=_explicit_missing(request),
        capability_evidence=capability_evidence,
        metrics=ExtractionMetrics(
            path="rules",
            rule_duration_ms=0,
            llm_duration_ms=0,
            llm_call_count=0,
            llm_retry_count=0,
            ambiguity_fields=[],
        ),
    )


def _extract_turn(text: str, *, today: date) -> _TurnExtraction:
    turn = _TurnExtraction()
    route_matches = list(_CITY_ROUTE.finditer(text))
    destination_candidates = _destination_candidates(text)

    if route_matches:
        route = route_matches[-1]
        turn.transport_origin_city = route.group("origin")
        turn.destination_city = route.group("destination")

    if len(destination_candidates) == 1:
        turn.destination_city = destination_candidates[0]
    elif len(destination_candidates) > 1:
        if _CORRECTION_MARKER.search(text):
            turn.destination_city = destination_candidates[-1]
        else:
            turn.destination_city = None
            turn.ambiguities.append(
                _ambiguity(
                    "core.destination_city",
                    "multiple_candidates",
                    text,
                    destination_candidates,
                )
            )
    elif _DESTINATION_PRONOUN.search(text):
        turn.ambiguities.append(_ambiguity("core.destination_city", "pronoun", text, []))

    origin_candidates = _origin_candidates(text)
    if len(origin_candidates) == 1:
        turn.transport_origin_city = origin_candidates[0]
    elif len(origin_candidates) > 1:
        turn.transport_origin_city = None
        turn.ambiguities.append(
            _ambiguity(
                "transport.origin_city",
                "multiple_candidates",
                text,
                origin_candidates,
            )
        )

    duration_candidates = explicit_duration_candidates(text)
    if len(set(duration_candidates)) == 1:
        turn.duration_days = duration_candidates[-1]
    elif len(set(duration_candidates)) > 1:
        if _CORRECTION_MARKER.search(text):
            turn.duration_days = duration_candidates[-1]
        else:
            turn.ambiguities.append(
                _ambiguity(
                    "core.duration_days",
                    "conflict",
                    text,
                    [str(value) for value in duration_candidates],
                )
            )

    _extract_turn_dates(turn, text, today=today)
    _extract_hotel_nearby(turn, text)
    return turn


def _extract_turn_dates(turn: _TurnExtraction, text: str, *, today: date) -> None:
    dates = explicit_dates(text, today=today)
    if not dates:
        return
    if len(dates) > 1 and _ALTERNATIVE_MARKER.search(text):
        turn.ambiguities.append(
            _ambiguity(
                "core.start_date",
                "multiple_candidates",
                text,
                [value.isoformat() for value in dates],
            )
        )
        return

    for clause in _CLAUSE_SPLIT.split(text):
        clause_dates = explicit_dates(clause, today=today)
        if not clause_dates:
            continue
        if any(marker in clause for marker in ("入住", "住店")):
            turn.hotel_check_in_date = clause_dates[0]
        if any(marker in clause for marker in ("退房", "离店")):
            turn.hotel_check_out_date = clause_dates[-1]
        if any(marker in clause for marker in ("去程", "出发", "启程")):
            turn.transport_outbound_date = clause_dates[0]
        if any(marker in clause for marker in ("返程", "回程", "回来")):
            turn.transport_return_date = clause_dates[-1]

    is_range = len(dates) >= 2 and _DATE_RANGE_MARKER.search(text)
    if is_range:
        start_date, end_date = dates[0], dates[1]
        if end_date < start_date:
            turn.ambiguities.append(
                _ambiguity(
                    "core.start_date",
                    "conflict",
                    text,
                    [value.isoformat() for value in dates[:2]],
                )
            )
            return
        turn.start_date = start_date
        range_days = (end_date - start_date).days + 1
        if turn.duration_days is None:
            turn.duration_days = range_days
        elif turn.duration_days != range_days:
            turn.ambiguities.append(
                _ambiguity(
                    "core.duration_days",
                    "conflict",
                    text,
                    [str(turn.duration_days), str(range_days)],
                )
            )
            turn.duration_days = None
        if any(marker in text for marker in ("入住", "退房", "离店", "住店")):
            turn.hotel_check_in_date = turn.hotel_check_in_date or start_date
            turn.hotel_check_out_date = turn.hotel_check_out_date or end_date
        if any(marker in text for marker in _TRANSPORT_MARKERS):
            turn.transport_outbound_date = turn.transport_outbound_date or start_date
            turn.transport_return_date = turn.transport_return_date or end_date
        return

    if len(dates) == 1:
        turn.start_date = dates[0]
    elif turn.transport_outbound_date is not None and turn.transport_return_date is not None:
        turn.start_date = turn.transport_outbound_date
    elif turn.hotel_check_in_date is not None and turn.hotel_check_out_date is not None:
        turn.start_date = turn.hotel_check_in_date
    else:
        turn.ambiguities.append(
            _ambiguity(
                "core.start_date",
                "conflict",
                text,
                [value.isoformat() for value in dates],
            )
        )


def _extract_hotel_nearby(turn: _TurnExtraction, text: str) -> None:
    matches = list(_HOTEL_NEARBY.finditer(text))
    if matches:
        poi = _clean_poi(matches[-1].group("poi"))
        if _POI_PRONOUN.fullmatch(poi) or any(
            marker in poi for marker in ("那里", "那边", "这里", "这边", "那个")
        ):
            turn.ambiguities.append(
                _ambiguity("hotel.nearby_poi", "pronoun", matches[-1].group(0), [])
            )
        else:
            turn.hotel_nearby_poi = poi
    elif any(marker in text for marker in _HOTEL_MARKERS) and _POI_PRONOUN.search(text):
        turn.ambiguities.append(_ambiguity("hotel.nearby_poi", "pronoun", text, []))


def _merge_turn(
    request: TripPlanningRequest,
    field_sources: dict[str, FieldSource],
    ambiguities: list[RequirementAmbiguity],
    turn: _TurnExtraction,
    *,
    source: FieldSource,
) -> None:
    values: tuple[tuple[AmbiguousField, object | None], ...] = (
        ("core.destination_city", turn.destination_city),
        ("core.duration_days", turn.duration_days),
        ("core.start_date", turn.start_date),
        ("transport.origin_city", turn.transport_origin_city),
        ("hotel.nearby_poi", turn.hotel_nearby_poi),
    )
    for field, value in values:
        if value is None:
            continue
        _set_request_field(request, field, value)
        field_sources[field] = source
        ambiguities[:] = [item for item in ambiguities if item.field != field]

    dated_values = (
        ("transport.outbound_date", turn.transport_outbound_date),
        ("transport.return_date", turn.transport_return_date),
        ("hotel.check_in_date", turn.hotel_check_in_date),
        ("hotel.check_out_date", turn.hotel_check_out_date),
    )
    for field, value in dated_values:
        if value is None:
            continue
        owner, attribute = field.split(".", 1)
        setattr(getattr(request, owner), attribute, value)
        field_sources[field] = source

    for ambiguity in turn.ambiguities:
        _set_request_field(request, ambiguity.field, None)
        field_sources.pop(ambiguity.field, None)
        ambiguities[:] = [item for item in ambiguities if item.field != ambiguity.field]
        ambiguities.append(ambiguity)


def _merge_preferences(
    request: CityTripRequest,
    text: str,
    field_sources: dict[str, FieldSource],
    source: FieldSource,
) -> None:
    interests = list(request.interests)
    for markers, preference in _PREFERENCE_MARKERS:
        if any(marker in text for marker in markers) and preference not in interests:
            interests.append(preference)
    if interests != request.interests:
        request.interests = interests
        field_sources["core.interests"] = source


def _merge_prices_and_stars(
    request: TripPlanningRequest,
    text: str,
    field_sources: dict[str, FieldSource],
    source: FieldSource,
) -> None:
    for clause in _CLAUSE_SPLIT.split(text):
        if match := _PRICE.search(clause):
            value = float(match.group(1))
            if any(marker in clause for marker in _HOTEL_MARKERS):
                request.hotel.max_nightly_price = value
                field_sources["hotel.max_nightly_price"] = source
            elif any(marker in clause for marker in _TRANSPORT_MARKERS):
                request.transport.max_price = value
                field_sources["transport.max_price"] = source
        if any(marker in clause for marker in _HOTEL_MARKERS):
            stars = [
                int(value) if value.isdigit() else _CHINESE_STAR[value]
                for value in _HOTEL_STAR.findall(clause)
            ]
            if stars:
                request.hotel.hotel_stars = list(dict.fromkeys(stars))
                field_sources["hotel.hotel_stars"] = source


def _destination_candidates(text: str) -> list[str]:
    alternative_routes = list(_ALTERNATIVE_ROUTE_ORIGIN.finditer(text))
    alternative_destinations: list[str] = []
    for match in _ALTERNATIVE_CITY_PAIR.finditer(text):
        if any(
            match.start() >= route.start() and match.end() <= route.end("second")
            for route in alternative_routes
        ):
            continue
        alternative_destinations.extend((match.group("first"), match.group("second")))
    if alternative_destinations:
        return list(dict.fromkeys(alternative_destinations))

    candidates = [match.group("city") for match in _DIRECT_DESTINATION.finditer(text)]
    candidates.extend(match.group("city") for match in _CITY_TRIP_SUFFIX.finditer(text))
    candidates.extend(match.group("destination") for match in _CITY_ROUTE.finditer(text))
    candidates.extend(match.group("destination") for match in alternative_routes)
    candidates = list(dict.fromkeys(candidates))
    if candidates:
        return candidates

    mentions = _city_mentions(text)
    normalized = text.strip(" ，,。；;！？!?")
    if len(mentions) == 1 and normalized in {mentions[0], f"{mentions[0]}市"}:
        return mentions
    if len(mentions) == 1 and any(marker in text for marker in _TRIP_MARKERS):
        return mentions
    if (
        len(mentions) > 1
        and any(marker in text for marker in _TRIP_MARKERS)
        and _ALTERNATIVE_MARKER.search(text)
    ):
        return mentions
    return []


def _origin_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for match in _ALTERNATIVE_ROUTE_ORIGIN.finditer(text):
        candidates.extend((match.group("first"), match.group("second")))
    candidates.extend(
        match.group("origin") or match.group("bare_origin") for match in _CITY_ORIGIN.finditer(text)
    )
    candidates.extend(match.group("origin") for match in _CITY_ROUTE.finditer(text))
    if "出发" in text and _ALTERNATIVE_MARKER.search(text):
        prefix = text[: text.find("出发")]
        alternatives = _city_mentions(prefix)
        if len(alternatives) > 1:
            candidates.extend(alternatives)
    return list(dict.fromkeys(item for item in candidates if item))


def _city_mentions(text: str) -> list[str]:
    matches = sorted(
        _CITY_MENTION.finditer(text),
        key=lambda match: (match.start(), -(match.end() - match.start())),
    )
    selected: list[tuple[int, int, str]] = []
    for match in matches:
        if any(match.start() < end and match.end() > start for start, end, _ in selected):
            continue
        selected.append((match.start(), match.end(), match.group("city")))
    return list(dict.fromkeys(item[2] for item in selected))


def _clean_poi(value: str) -> str:
    normalized = value.strip(" 的，,。；;")
    for marker in ("以及", "还有", "并且", "顺便", "推荐", "查找", "查询", "找", "查"):
        if marker in normalized:
            normalized = normalized.rsplit(marker, 1)[-1]
    return normalized.strip(" 的，,。；;")


def _ambiguity(
    field: AmbiguousField,
    kind: Literal["multiple_candidates", "pronoun", "conflict", "scope"],
    excerpt: str,
    candidates: list[str],
) -> RequirementAmbiguity:
    return RequirementAmbiguity(
        field=field,
        kind=kind,
        excerpt=excerpt[:1_000],
        candidates=list(dict.fromkeys(candidates)),
    )


def _ambiguity_prompt(
    messages: Sequence[ChatMessage],
    ambiguities: Sequence[RequirementAmbiguity],
) -> str:
    recent_context = [
        {
            "role": message.role,
            "content": message.content.strip()[:1_000],
        }
        for message in messages
        if message.role in {"user", "assistant"} and message.content.strip()
    ][-4:]
    payload = {
        "current_date": _current_date().isoformat(),
        "ambiguities": [item.model_dump(mode="json") for item in ambiguities],
        "recent_context": recent_context,
        "instructions": ("只填写歧义列表对应的输出字段；缺失信息或仍有多种合理理解时返回 null。"),
    }
    return json.dumps(payload, ensure_ascii=False)


def _apply_ambiguity_resolution(
    result: RuleFirstExtractionResult,
    resolution: TripAmbiguityResolution,
) -> None:
    resolution_values: dict[AmbiguousField, object | None] = {
        "core.destination_city": resolution.destination_city,
        "core.duration_days": resolution.duration_days,
        "core.start_date": resolution.start_date,
        "transport.origin_city": resolution.transport_origin_city,
        "hotel.nearby_poi": resolution.hotel_nearby_poi,
    }
    for ambiguity in result.ambiguities:
        value = resolution_values[ambiguity.field]
        if value is None or not _resolution_is_allowed(value, ambiguity.candidates):
            continue
        _set_request_field(result.request, ambiguity.field, value)
        result.field_sources[ambiguity.field] = "llm_resolution"


def _resolution_is_allowed(value: object, candidates: Sequence[str]) -> bool:
    if not candidates:
        return True
    normalized = value.isoformat() if isinstance(value, date) else str(value)
    return normalized in candidates


def _set_request_field(
    request: TripPlanningRequest,
    field: AmbiguousField,
    value: object | None,
) -> None:
    owner, attribute = field.split(".", 1)
    if owner == "core":
        setattr(request.core, attribute, value)
    elif owner == "transport":
        setattr(request.transport, attribute, value)
    else:
        setattr(request.hotel, attribute, value)


def _explicit_missing(request: TripPlanningRequest) -> list[str]:
    missing = [
        field
        for field, value in (
            ("core.destination_city", request.core.destination_city),
            ("core.duration_days", request.core.duration_days),
            ("core.start_date", request.core.start_date),
        )
        if value is None
    ]
    if request.transport.action is CapabilityAction.ENABLE:
        if not request.transport.modes:
            missing.append("transport.modes")
        if not request.transport.origin_city:
            missing.append("transport.origin_city")
    return missing


def _current_date() -> date:
    context = get_request_context()
    return context.time.current_date if context is not None else date.today()


__all__ = [
    "CapabilityEvidence",
    "ExtractionMetrics",
    "RequirementAmbiguity",
    "RuleFirstExtractionResult",
    "RuleFirstTripRequirementExtractor",
    "TripAmbiguityResolution",
    "extract_trip_request_by_rules",
]
