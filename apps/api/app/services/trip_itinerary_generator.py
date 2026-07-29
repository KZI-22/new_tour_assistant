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
from app.services.trip_presentation_context import build_trip_presentation_context
from app.services.weather_advice_service import build_weather_advice

TRIP_GENERATION_SYSTEM_PROMPT = """你是受外部证据约束的旅行文案编辑器。

严格规则：
1. 输入是后端精简并核实过的展示上下文；
   日期、地点、顺序、路线、天气、交通与酒店均已确定，你只组织和润色文案。
2. title 应在信息充足时体现出发地、目的地、天数或日期；
   summary 应自然串联天气、交通、住宿和每日行程，但不得替用户选择某个班次或酒店。
3. days 必须按输入 days 的顺序返回；recommendation_reasons 的数量和顺序必须与对应 places 完全一致。
4. 不得新增、删除或改写地点、日期、路线、距离、天气、班次、酒店、价格、库存或预订事实。
5. 推荐理由只能依据地点名称、地址、类型、用户偏好、匹配偏好和后端筛选理由整理。
6. tips 只能依据输入中的天气建议、路段耗时、降级标记和 warnings 整理。
7. 输入没有提供景点简介、开放时间、门票、预约规则、历史背景、具体餐厅、美食、
   地铁线路或营业状态时，禁止补写这些事实；可保守提醒用户出发前确认。
8. 交通和酒店 options 仅供概括本次查询范围；
   不得声称已经替用户选择、预订、支付、出票、锁价、占座或确认库存。
9. 文案保持简洁：summary 不超过 300 字，每条推荐理由不超过 120 字，每天 tips 不超过 3 条。
10. 只输出符合指定 JSON Schema 的结构化结果。"""


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
            draft = await self._structured.invoke_prompt_json(
                TripNarrativeDraft,
                TRIP_GENERATION_SYSTEM_PROMPT,
                prompt,
                timeout_seconds=self._timeout_seconds,
            )
            return compose_trip_narrative(evidence, draft)
        except StructuredOutputError as exc:
            raise TripItineraryGenerationError("模型没有生成有效的统一旅行方案。") from exc


def build_trip_narrative_skeleton(
    evidence: JoinedTripEvidence,
) -> TripNarrativePlan:
    core = evidence.request.core
    city = core.destination_city or "目的地"
    duration_days = core.duration_days or len(
        evidence.map_weather.map.days if evidence.map_weather.map else []
    )
    draft = TripNarrativeDraft(
        title=f"{city}{duration_days}日旅行方案",
        summary=(
            "日期、地点、顺序、路线、天气、交通与酒店信息已由后端根据查询证据确定，"
            "旅行文案正在生成。"
        ),
        days=[],
    )
    return compose_trip_narrative(evidence, draft)


def build_trip_generation_prompt(evidence: JoinedTripEvidence) -> str:
    context = build_trip_presentation_context(evidence)
    return json.dumps(
        context.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )


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
    "build_trip_narrative_skeleton",
    "build_trip_generation_prompt",
    "compose_trip_narrative",
]
