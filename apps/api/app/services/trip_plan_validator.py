from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import timedelta

from app.schemas.map_planning import MapNarrativePlan, MapTripEvidence
from app.schemas.trip_evidence import EvidenceStatus, JoinedTripEvidence
from app.schemas.trip_planning import (
    CityTripRequest,
    TripWeatherEvidence,
)
from app.schemas.trip_validation import ValidationIssue
from app.services.weather_advice_service import build_weather_advice

_BOOKING_CLAIM_PATTERNS = (
    "预订成功",
    "已预订",
    "已经预订",
    "完成预订",
    "出票成功",
    "已出票",
    "支付成功",
    "锁价成功",
    "已占座",
)


class TripPlanValidator:
    def validate(
        self,
        evidence: JoinedTripEvidence,
        plan: MapNarrativePlan,
    ) -> list[ValidationIssue]:
        map_evidence = evidence.map_weather.map
        weather = evidence.map_weather.weather
        if map_evidence is None or weather is None:
            return [
                _issue(
                    "DAY_INDEX_MISMATCH",
                    "map_weather",
                    "地图与天气核心证据缺失。",
                    expected="usable map and weather evidence",
                    actual="core evidence unavailable",
                )
            ]

        issues = validate_map_narrative(
            evidence.request.core,
            map_evidence,
            weather,
            plan,
        )
        issues.extend(
            _validate_optional_output(
                name="transport",
                enabled=evidence.capabilities.transport.enabled,
                evidence_status=evidence.transport.status,
                evidence_options=evidence.transport.display_options,
                options=getattr(plan, "transport_options", []),
            )
        )
        issues.extend(
            _validate_optional_output(
                name="hotel",
                enabled=evidence.capabilities.hotel.enabled,
                evidence_status=evidence.hotel.status,
                evidence_options=evidence.hotel.display_options,
                options=getattr(plan, "hotel_options", []),
            )
        )
        if any(
            pattern in text
            for text in _iter_text(plan.model_dump(mode="python"))
            for pattern in _BOOKING_CLAIM_PATTERNS
        ):
            issues.append(
                _issue(
                    "BOOKING_CLAIM_FORBIDDEN",
                    "narrative",
                    "旅行方案不得声称已完成预订、支付、出票、锁价或占座。",
                    expected="recommendations without booking completion claims",
                    actual="booking completion claim detected",
                )
            )
        return issues


def validate_map_narrative(
    request: CityTripRequest,
    evidence: MapTripEvidence,
    weather: TripWeatherEvidence,
    plan: MapNarrativePlan,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    duration_days = request.duration_days or 0
    expected_indexes = list(range(1, duration_days + 1))
    expected_dates = [
        request.start_date + timedelta(days=offset)
        for offset in range(duration_days)
        if request.start_date is not None
    ]

    if [day.day_index for day in evidence.days] != expected_indexes:
        issues.append(
            _issue(
                "DAY_INDEX_MISMATCH",
                "map_evidence.days",
                "地图证据的行程日索引与请求不一致。",
                expected="consecutive day indexes starting at 1",
                actual="map evidence day indexes mismatch",
            )
        )
    if [day.date for day in evidence.days] != expected_dates:
        issues.append(
            _issue(
                "DAY_DATE_MISMATCH",
                "map_evidence.days",
                "地图证据日期未从请求开始日期连续排列。",
                expected="consecutive dates from requested start date",
                actual="map evidence dates mismatch",
            )
        )
    if [day.day_index for day in plan.days] != expected_indexes:
        issues.append(
            _issue(
                "DAY_INDEX_MISMATCH",
                "days",
                "方案的行程日索引与请求不一致。",
                expected="consecutive day indexes starting at 1",
                actual="narrative day indexes mismatch",
            )
        )
    if [day.date for day in weather.days] != expected_dates:
        issues.append(
            _issue(
                "WEATHER_DATE_MISMATCH",
                "weather_evidence.days",
                "天气证据日期与请求的行程日期不一致。",
                expected="weather dates matching requested itinerary",
                actual="weather dates mismatch",
            )
        )

    all_poi_ids = [
        place.poi_id for evidence_day in evidence.days for place in evidence_day.ordered_places()
    ]
    duplicate_poi_ids = sorted(
        poi_id for poi_id in set(all_poi_ids) if all_poi_ids.count(poi_id) > 1
    )
    for poi_id in duplicate_poi_ids:
        issues.append(
            _issue(
                "DUPLICATE_POI",
                "map_evidence.days",
                "地图证据包含重复 POI。",
                expected="each POI appears once",
                actual="duplicate POI detected",
                reference_id=poi_id,
            )
        )

    known_references = {
        place.reference_id
        for evidence_day in evidence.days
        for place in evidence_day.ordered_places()
    }
    narrative_days = {day.day_index: day for day in plan.days}
    weather_by_date = {day.date: day for day in weather.days}
    for narrative_day in plan.days:
        for place_index, place in enumerate(narrative_day.places):
            if place.reference_id not in known_references:
                issues.append(
                    _issue(
                        "MAP_REFERENCE_UNKNOWN",
                        f"days.{narrative_day.day_index}.places.{place_index}.reference_id",
                        "方案引用了地图证据中不存在的地点。",
                        expected="reference must exist in map evidence",
                        actual="unknown reference",
                        reference_id=place.reference_id,
                    )
                )

    for evidence_day in evidence.days:
        narrative_day = narrative_days.get(evidence_day.day_index)
        if narrative_day is None:
            continue
        if narrative_day.date != evidence_day.date:
            issues.append(
                _issue(
                    "DAY_DATE_MISMATCH",
                    f"days.{evidence_day.day_index}.date",
                    "方案日期与对应地图证据日期不一致。",
                    expected="date matching map evidence",
                    actual="narrative date mismatch",
                )
            )
        expected_refs = [place.reference_id for place in evidence_day.ordered_places()]
        actual_refs = [place.reference_id for place in narrative_day.places]
        if actual_refs != expected_refs:
            issues.append(
                _issue(
                    "MAP_REFERENCE_ORDER_MISMATCH",
                    f"days.{evidence_day.day_index}.places",
                    "方案地点引用顺序与地图证据不一致。",
                    expected="exact map evidence reference order",
                    actual="narrative reference order mismatch",
                )
            )
        expected_legs = list(zip(expected_refs, expected_refs[1:], strict=False))
        actual_legs = [(leg.origin_ref, leg.destination_ref) for leg in evidence_day.route_legs]
        if actual_legs != expected_legs:
            issues.append(
                _issue(
                    "ROUTE_ENDPOINT_MISMATCH",
                    f"map_evidence.days.{evidence_day.day_index}.route_legs",
                    "路线段起终点与地图地点顺序不一致。",
                    expected="route legs connecting consecutive map references",
                    actual="route endpoints mismatch",
                )
            )
        weather_day = weather_by_date.get(evidence_day.date)
        if weather_day is None:
            continue
        expected_advice = build_weather_advice(weather_day)
        if narrative_day.weather_advice != expected_advice:
            issues.append(
                _issue(
                    "WEATHER_ADVICE_MISMATCH",
                    f"days.{evidence_day.day_index}.weather_advice",
                    "天气建议必须与后端根据供应商证据生成的标准结果完全一致。",
                    expected="deterministic weather advice derived from provider evidence",
                    actual="weather advice differs from deterministic output",
                )
            )
    return issues


def _validate_optional_output(
    *,
    name: str,
    enabled: bool,
    evidence_status: EvidenceStatus,
    evidence_options: Sequence[str],
    options: Sequence[str],
) -> list[ValidationIssue]:
    if name == "transport":
        disabled_code = "TRANSPORT_OUTPUT_WHILE_DISABLED"
        unavailable_code = "TRANSPORT_FACT_WITHOUT_USABLE_EVIDENCE"
        missing_code = "TRANSPORT_USABLE_EVIDENCE_WITHOUT_OPTIONS"
        mismatch_code = "TRANSPORT_OPTION_MISMATCH"
        path = "transport_options"
        label = "交通"
    else:
        disabled_code = "HOTEL_OUTPUT_WHILE_DISABLED"
        unavailable_code = "HOTEL_FACT_WITHOUT_USABLE_EVIDENCE"
        missing_code = "HOTEL_USABLE_EVIDENCE_WITHOUT_OPTIONS"
        mismatch_code = "HOTEL_OPTION_MISMATCH"
        path = "hotel_options"
        label = "酒店"
    if not options and evidence_status is not EvidenceStatus.USABLE:
        return []
    if not enabled:
        if not options:
            return []
        return [
            _issue(
                disabled_code,
                path,
                f"未启用{label}能力时不得输出实时{label}结果。",
                expected=f"empty {name} output while disabled",
                actual=f"{name} output present",
            )
        ]
    if evidence_status is not EvidenceStatus.USABLE:
        if not options:
            return []
        return [
            _issue(
                unavailable_code,
                path,
                f"{label}证据不可用时不得输出具体{label}结果。",
                expected=f"empty {name} output without usable evidence",
                actual=f"{name} output present",
            )
        ]
    if not evidence_options:
        return [
            _issue(
                missing_code,
                path,
                f"{label}证据标记为可用，但没有可展示的规范化结果。",
                expected=f"normalized {name} options for usable evidence",
                actual=f"usable {name} evidence without normalized options",
            )
        ]
    if list(options) != list(evidence_options):
        return [
            _issue(
                mismatch_code,
                path,
                f"{label}展示结果必须与供应商规范化证据完全一致。",
                expected=f"{name} output matching normalized evidence",
                actual=f"{name} output differs from normalized evidence",
            )
        ]
    return []


def _iter_text(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _iter_text(item)
    elif isinstance(value, Sequence):
        for item in value:
            yield from _iter_text(item)


def _issue(
    code: str,
    path: str,
    message: str,
    *,
    expected: str,
    actual: str,
    reference_id: str | None = None,
) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        path=path,
        message=message,
        expected_summary=expected,
        actual_summary=actual,
        reference_id=reference_id,
    )


__all__ = ["TripPlanValidator", "validate_map_narrative"]
