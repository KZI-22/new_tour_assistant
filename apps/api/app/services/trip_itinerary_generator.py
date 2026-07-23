from __future__ import annotations

import json

from langchain_core.language_models import BaseChatModel

from app.schemas.trip_evidence import EvidenceStatus, JoinedTripEvidence
from app.schemas.trip_itinerary import TripNarrativePlan
from app.schemas.trip_validation import ValidationIssue
from app.services.structured_output_service import (
    StructuredOutputError,
    StructuredOutputService,
)

TRIP_GENERATION_SYSTEM_PROMPT = """你是受外部证据约束的旅行方案整理器。

严格规则：
1. 每日 place 引用必须与 map_evidence 中的 attractions 顺序完全相同，不能增删、交换或跨天移动。
2. 不得输出新的地点、具体餐厅或路线引用；地图、路线、距离和天气事实只能来自对应证据。
3. transport_options 仅在交通能力 enabled 且 transport_evidence.status=usable 时填写。
4. hotel_options 仅在酒店能力 enabled 且 hotel_evidence.status=usable 时填写。
5. FlyAI data 是不透明 JSON；只可忠实整理其中可见内容，不得猜测缺失字段、价格单位、库存或预订状态。
6. failed、empty 或 skipped 的可选 Evidence 不得生成具体班次或酒店，状态说明由后端确定性渲染。
7. 不得声称已完成预订、支付、锁价、占座或库存确认。
8. 只输出符合指定 JSON Schema 的结构化结果。"""


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
        *,
        validation_issues: list[ValidationIssue] | None = None,
    ) -> TripNarrativePlan:
        prompt = build_trip_generation_prompt(
            evidence,
            validation_issues=validation_issues or [],
        )
        try:
            return await self._structured.invoke(
                TripNarrativePlan,
                TRIP_GENERATION_SYSTEM_PROMPT,
                prompt,
                timeout_seconds=self._timeout_seconds,
            )
        except StructuredOutputError as exc:
            raise TripItineraryGenerationError("模型没有生成有效的统一旅行方案。") from exc


def build_trip_generation_prompt(
    evidence: JoinedTripEvidence,
    *,
    validation_issues: list[ValidationIssue],
) -> str:
    map_evidence = evidence.map_weather.map
    weather = evidence.map_weather.weather
    requirements = (
        [
            {
                "day_index": day.day_index,
                "date": day.date.isoformat(),
                "ordered_place_refs": [place.reference_id for place in day.ordered_places()],
            }
            for day in map_evidence.days
        ]
        if map_evidence is not None
        else []
    )
    payload = {
        "request": evidence.request.model_dump(mode="json"),
        "capability_plan": evidence.capabilities.model_dump(mode="json"),
        "map_evidence": (
            map_evidence.model_dump(mode="json") if map_evidence is not None else None
        ),
        "weather_evidence": (weather.model_dump(mode="json") if weather is not None else None),
        "transport_evidence": _optional_prompt_evidence(
            evidence.capabilities.transport.enabled,
            evidence.transport,
        ),
        "hotel_evidence": _optional_prompt_evidence(
            evidence.capabilities.hotel.enabled,
            evidence.hotel,
        ),
        "requirements": requirements,
        "validation_issues": [issue.model_dump(mode="json") for issue in validation_issues],
    }
    return json.dumps(payload, ensure_ascii=False)


def _optional_prompt_evidence(
    enabled: bool,
    evidence: object,
) -> dict[str, object]:
    status = getattr(evidence, "status", EvidenceStatus.FAILED)
    data = getattr(evidence, "data", None)
    return {
        "enabled": enabled,
        "status": status.value if isinstance(status, EvidenceStatus) else str(status),
        "query": getattr(evidence, "query", {}) if enabled else {},
        "data": data if enabled and status is EvidenceStatus.USABLE else None,
        "warnings": getattr(evidence, "warnings", []) if enabled else [],
        "error_code": getattr(evidence, "error_code", None) if enabled else None,
    }


__all__ = [
    "TRIP_GENERATION_SYSTEM_PROMPT",
    "TripItineraryGenerationError",
    "TripItineraryGenerator",
    "build_trip_generation_prompt",
]
