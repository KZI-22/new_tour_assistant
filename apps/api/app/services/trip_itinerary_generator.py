from __future__ import annotations

import json
from datetime import date

from langchain_core.language_models import BaseChatModel

from app.schemas.map_planning import (
    MapDayEvidence,
    MapDayNarrative,
    MapPlaceEvidence,
    MapPlaceNarrative,
)
from app.schemas.trip_evidence import EvidenceStatus, JoinedTripEvidence
from app.schemas.trip_itinerary import (
    TripDayNarrativeDraft,
    TripNarrativeDraft,
    TripNarrativePlan,
)
from app.schemas.trip_planning import DailyWeatherEvidence
from app.services.structured_output_service import (
    StructuredOutputError,
    StructuredOutputService,
)
from app.services.weather_advice_service import build_weather_advice

TRIP_GENERATION_SYSTEM_PROMPT = """你是受外部证据约束的旅行文案编辑器。

严格规则：
1. 后端已经确定行程天数、日期、地点、地点顺序、路线、天气、交通与酒店；你只生成文案。
2. days 必须按输入 days 的顺序返回；recommendation_reasons 必须按对应 places 的顺序返回。
3. 不得新增地点、具体餐厅、路线、距离、天气、班次、酒店、价格、库存或预订事实。
4. 推荐理由只能依据地点名称、类型、用户偏好、匹配偏好和后端筛选理由整理。
5. 文案保持简洁：summary 不超过 300 字，每条推荐理由不超过 120 字，每天 tips 不超过 3 条。
6. 不得声称已完成预订、支付、出票、锁价、占座或库存确认。
7. 只输出符合指定 JSON Schema 的结构化结果。"""


class TripItineraryGenerationError(RuntimeError):
    pass


class TripItineraryGenerator:
    def __init__(
        self,
        model: BaseChatModel,
        *,
        timeout_seconds: float,
    ) -> None:
        self._structured = StructuredOutputService(model)
        self._timeout_seconds = timeout_seconds

    async def generate(
        self,
        evidence: JoinedTripEvidence,
    ) -> TripNarrativePlan:
        prompt = build_trip_generation_prompt(evidence)
        try:
            draft = await self._structured.invoke(
                TripNarrativeDraft,
                TRIP_GENERATION_SYSTEM_PROMPT,
                prompt,
                timeout_seconds=self._timeout_seconds,
            )
            return compose_trip_narrative(evidence, draft)
        except StructuredOutputError as exc:
            raise TripItineraryGenerationError("模型没有生成有效的统一旅行方案。") from exc


def build_trip_generation_prompt(evidence: JoinedTripEvidence) -> str:
    map_evidence = evidence.map_weather.map
    core = evidence.request.core
    days = (
        [
            {
                "day_index": day.day_index,
                "places": [
                    {
                        "name": place.name,
                        "poi_type": place.poi_type,
                        "estimated_visit_minutes": place.estimated_visit_minutes,
                        "matched_preferences": place.matched_preferences,
                        "selection_reasons": place.selection_reasons,
                    }
                    for place in day.ordered_places()
                ],
            }
            for day in map_evidence.days
        ]
        if map_evidence is not None
        else []
    )
    payload = {
        "request": {
            "destination_city": core.destination_city,
            "duration_days": core.duration_days,
            "interests": core.interests,
            "food_preferences": core.food_preferences,
        },
        "days": days,
    }
    return json.dumps(payload, ensure_ascii=False)


def compose_trip_narrative(
    evidence: JoinedTripEvidence,
    draft: TripNarrativeDraft,
) -> TripNarrativePlan:
    map_evidence = evidence.map_weather.map
    weather = evidence.map_weather.weather
    if map_evidence is None or weather is None:
        raise TripItineraryGenerationError("地图与天气核心证据不可用。")

    weather_by_date = {day.date: day for day in weather.days}
    narrative_days: list[MapDayNarrative] = []
    for day_offset, evidence_day in enumerate(map_evidence.days):
        draft_day = draft.days[day_offset] if day_offset < len(draft.days) else None
        narrative_days.append(
            _compose_day_narrative(
                evidence,
                evidence_day,
                weather_by_date,
                draft_day,
            )
        )

    return TripNarrativePlan(
        title=draft.title,
        summary=draft.summary,
        days=narrative_days,
        practical_tips=list(draft.practical_tips),
        warnings=list(draft.warnings),
        transport_options=_evidence_options(
            evidence.capabilities.transport.enabled,
            evidence.transport,
        ),
        hotel_options=_evidence_options(
            evidence.capabilities.hotel.enabled,
            evidence.hotel,
        ),
    )


def _compose_day_narrative(
    evidence: JoinedTripEvidence,
    evidence_day: MapDayEvidence,
    weather_by_date: dict[date, DailyWeatherEvidence],
    draft_day: TripDayNarrativeDraft | None,
) -> MapDayNarrative:
    reasons = draft_day.recommendation_reasons if draft_day is not None else []
    return MapDayNarrative(
        day_index=evidence_day.day_index,
        date=evidence_day.date,
        theme=(
            draft_day.theme
            if draft_day is not None
            else _fallback_day_theme(evidence, evidence_day.day_index)
        ),
        places=[
            MapPlaceNarrative(
                reference_id=place.reference_id,
                recommendation_reason=(
                    reasons[place_offset]
                    if place_offset < len(reasons)
                    else _fallback_recommendation_reason(place)
                ),
            )
            for place_offset, place in enumerate(evidence_day.ordered_places())
        ],
        weather_advice=(
            build_weather_advice(weather_by_date[evidence_day.date])
            if evidence_day.date in weather_by_date
            else []
        ),
        tips=list(draft_day.tips) if draft_day is not None else [],
    )


def _fallback_day_theme(
    evidence: JoinedTripEvidence,
    day_index: int,
) -> str:
    city = evidence.request.core.destination_city or "目的地"
    return f"{city}第 {day_index} 天行程"


def _fallback_recommendation_reason(place: MapPlaceEvidence) -> str:
    if place.selection_reasons:
        detail = "；".join(place.selection_reasons)
        return f"后端筛选依据：{detail}"[:500]
    if place.matched_preferences:
        preferences = "、".join(str(item) for item in place.matched_preferences)
        return f"符合本次行程的{preferences}偏好，并纳入当天既定游览顺序。"
    return f"{place.name}已由后端纳入当天的既定游览顺序。"


def _evidence_options(enabled: bool, evidence: object) -> list[str]:
    status = getattr(evidence, "status", EvidenceStatus.FAILED)
    if not enabled or status is not EvidenceStatus.USABLE:
        return []
    return list(getattr(evidence, "display_options", []))


__all__ = [
    "TRIP_GENERATION_SYSTEM_PROMPT",
    "TripItineraryGenerationError",
    "TripItineraryGenerator",
    "build_trip_generation_prompt",
    "compose_trip_narrative",
]
