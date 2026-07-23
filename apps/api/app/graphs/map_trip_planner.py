from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Literal, TypeVar

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel

from app.core.settings import Settings
from app.schemas.chat import ChatMessage
from app.schemas.map_planning import MapNarrativePlan, MapTripEvidence
from app.schemas.tool_execution import (
    ChatStreamEvent,
    MessageDeltaEvent,
    PlanningStageEvent,
    PlanningTraceEvent,
)
from app.schemas.trip_planning import (
    CityTripRequest,
    CityTripRequestExtraction,
    TripWeatherEvidence,
)
from app.services.agent_executor import AgentExecutionError
from app.services.city_trip_request import (
    apply_explicit_request_overrides,
    clarification_question,
    request_extraction_prompt,
    validate_city_trip_request,
)
from app.services.map_itinerary_renderer import render_map_itinerary
from app.services.map_trip_collection_service import (
    MapTripCollectionService,
)
from app.services.map_weather_collection_service import MapWeatherCollectionService
from app.services.structured_output_service import (
    StructuredOutputError,
    StructuredOutputService,
)
from app.services.trip_plan_validator import validate_map_narrative
from app.services.weather_evidence_service import WeatherEvidenceService

logger = logging.getLogger(__name__)
SchemaT = TypeVar("SchemaT", bound=BaseModel)

_MAP_GENERATION_SYSTEM_PROMPT = """你是地图证据旅行攻略的文案整理器。

你只能为输入中已经确定的高德景点引用编写简短推荐理由、每日主题，并依据给定天气证据编写天气建议。

严格规则：
1. 每日 place 引用必须与 evidence 中的 attractions 顺序完全相同，不能增删、交换或跨天移动。
2. 不得输出新的地点、具体餐厅或路线引用，也不得把模型常识写成高德、官方或小红书事实。
3. 不得声称评分、榜单、营业状态、票价、开放时间、展品、招牌菜、排队或预约信息。
4. 天气事实只能来自 weather_evidence；coverage=unavailable 时 weather_advice 必须为空。
5. recommendation_reason 是模型整理建议，只描述大致体验方向，不得伪造供应商评价。
6. 午餐和晚餐只可作为时间预留提示，不能推荐具体餐厅。
7. 只输出符合指定 JSON Schema 的结构化结果。"""


class MapTripPlanningError(AgentExecutionError):
    pass


class MapTripPlanner:
    def __init__(
        self,
        collection_service: MapTripCollectionService,
        weather_service: WeatherEvidenceService,
        settings: Settings,
    ) -> None:
        self._map_weather_service = MapWeatherCollectionService(
            collection_service,
            weather_service,
            weather_timeout_seconds=settings.trip_planning_data_timeout_seconds,
        )
        self._settings = settings

    async def stream(
        self,
        model: BaseChatModel,
        messages: list[ChatMessage],
        *,
        route_source: Literal["llm_router", "fallback", "explicit"] = "explicit",
    ) -> AsyncIterator[ChatStreamEvent]:
        run = _MapTripPlanningRun(
            model=model,
            messages=messages,
            map_weather_service=self._map_weather_service,
            settings=self._settings,
            route_source=route_source,
        )
        async for event in run.stream():
            yield event


class _MapTripPlanningRun:
    def __init__(
        self,
        *,
        model: BaseChatModel,
        messages: list[ChatMessage],
        map_weather_service: MapWeatherCollectionService,
        settings: Settings,
        route_source: Literal["llm_router", "fallback", "explicit"],
    ) -> None:
        self._model = model
        self._messages = messages
        self._map_weather_service = map_weather_service
        self._settings = settings
        self._route_source = route_source
        self._trace_sequence = 0

    async def stream(self) -> AsyncIterator[ChatStreamEvent]:
        latest = next(
            (message.content for message in reversed(self._messages) if message.role == "user"),
            "",
        )
        yield self._trace(
            "request_received",
            "收到地图规划请求",
            data={
                "latest_user_message": latest[:2_000],
                "conversation_message_count": len(self._messages),
            },
        )
        yield self._trace(
            "route_selected",
            "请求已路由到地图与天气规划器",
            data={"route": "map_weather", "route_source": self._route_source},
        )
        yield _stage("understanding_request", "正在提取城市、天数和出行日期", "running")
        request, extraction_method, overrides = await self._extract_request()
        yield self._trace(
            "requirements_extracted",
            "已提取城市、天数和出行日期",
            data={
                "destination_city": request.destination_city,
                "duration_days": request.duration_days,
                "start_date": request.start_date.isoformat() if request.start_date else None,
                "extraction_method": extraction_method,
                **overrides,
            },
        )
        yield _stage("understanding_request", "正在提取城市、天数和出行日期", "success")

        yield _stage("checking_requirements", "正在检查规划所需信息", "running")
        missing, errors = validate_city_trip_request(
            request,
            maximum_days=self._settings.trip_planner_max_days,
        )
        yield self._trace(
            "requirements_validated",
            "规划参数检查完成",
            status="success" if not missing and not errors else "partial",
            data={"missing_fields": missing, "validation_errors": errors},
        )
        yield _stage("checking_requirements", "正在检查规划所需信息", "success")
        if missing or errors:
            yield MessageDeltaEvent(delta=clarification_question(missing, errors))
            return

        assert request.destination_city is not None
        assert request.duration_days is not None
        assert request.start_date is not None
        yield _stage("collecting_pois", "正在召回、筛选并编排高德景点", "running")
        yield _stage("collecting_weather", "正在查询行程日期对应的天气", "running")
        bundle = await self._map_weather_service.collect(request)
        if bundle.status == "failed":
            user_message = (
                bundle.warnings[0] if bundle.warnings else "地图规划暂时不可用，请稍后重试。"
            )
            yield _stage(
                "collecting_pois",
                "正在召回、筛选并编排高德景点",
                "failed",
                detail=user_message,
            )
            raise MapTripPlanningError(
                bundle.error_code or "MAP_PLANNING_UNAVAILABLE",
                user_message,
            )

        assert bundle.map is not None
        assert bundle.weather is not None
        evidence = bundle.map
        weather = bundle.weather
        yield _stage(
            "collecting_pois",
            "正在召回、筛选并编排高德景点",
            "success" if not evidence.warnings else "partial",
            detail=f"已形成 {len(evidence.days)} 个分日地图证据包。",
        )
        coverage = sum(day.coverage == "available" for day in weather.days)
        yield _stage(
            "collecting_weather",
            "正在查询行程日期对应的天气",
            "success" if coverage == len(weather.days) else "partial",
            detail=f"天气预报覆盖 {coverage}/{len(weather.days)} 个行程日。",
        )
        yield self._trace(
            "evidence_selected",
            "地图、路线和天气证据已汇合",
            status="success" if not evidence.warnings and not weather.warnings else "partial",
            data={
                "day_count": len(evidence.days),
                "place_count": sum(len(day.ordered_places()) for day in evidence.days),
                "route_leg_count": sum(len(day.route_legs) for day in evidence.days),
                "weather_coverage_count": coverage,
                "planning_run_id": evidence.planning_run_id,
            },
        )

        yield _stage("generating_itinerary", "正在整理地图与天气攻略", "running")
        narrative = await self._generate_validated_narrative(request, evidence, weather)
        yield self._trace(
            "itinerary_generated",
            "地图攻略文案生成完成",
            data={"title": narrative.title, "day_count": len(narrative.days)},
        )
        yield _stage("generating_itinerary", "正在整理地图与天气攻略", "success")
        yield _stage("validating_itinerary", "正在校验地点、日期和路线引用", "success")
        yield self._trace(
            "validation_completed",
            "地图攻略证据校验通过",
            data={"revision_limit": 1},
        )

        yield _stage("finalizing", "正在渲染最终地图攻略", "running")
        answer = render_map_itinerary(evidence, weather, narrative)
        yield MessageDeltaEvent(delta=answer)
        yield self._trace(
            "response_completed",
            "最终地图攻略已渲染",
            data={"output_chars": len(answer)},
        )
        yield _stage("finalizing", "正在渲染最终地图攻略", "success")

    async def _extract_request(
        self,
    ) -> tuple[CityTripRequest, str, dict[str, bool]]:
        try:
            extraction = await self._structured(
                CityTripRequestExtraction,
                (
                    "你只负责提取城市多日攻略所需的 destination_city、duration_days、"
                    "start_date 和 interests；不得猜测。interests 只能映射为 Schema 中"
                    "列出的标准偏好标签，未明确提供时返回空数组；food_preferences 返回空数组。"
                ),
                request_extraction_prompt(self._messages),
                timeout_seconds=self._settings.trip_planner_request_extraction_timeout_seconds,
            )
            request = extraction.request
            method = "model"
        except MapTripPlanningError as exc:
            logger.info(
                "Map request extraction fell back to deterministic fields code=%s",
                exc.code,
            )
            request = CityTripRequest()
            method = "fallback"
        request, overrides = apply_explicit_request_overrides(request, self._messages)
        return request, method, overrides

    async def _generate_validated_narrative(
        self,
        request: CityTripRequest,
        evidence: MapTripEvidence,
        weather: TripWeatherEvidence,
    ) -> MapNarrativePlan:
        prompt = _generation_prompt(request, evidence, weather)
        plan = await self._structured(
            MapNarrativePlan,
            _MAP_GENERATION_SYSTEM_PROMPT,
            prompt,
            timeout_seconds=self._settings.trip_planner_model_timeout_seconds,
        )
        issues = validate_map_narrative(request, evidence, weather, plan)
        if not issues:
            return plan
        revision_prompt = (
            f"{prompt}\n\n上一次输出未通过以下校验："
            f"{json.dumps([issue.message for issue in issues], ensure_ascii=False)}。"
            "请仅修正结构、日期和引用顺序，不得添加新地点。"
        )
        revised = await self._structured(
            MapNarrativePlan,
            _MAP_GENERATION_SYSTEM_PROMPT,
            revision_prompt,
            timeout_seconds=self._settings.trip_planner_model_timeout_seconds,
        )
        remaining = validate_map_narrative(request, evidence, weather, revised)
        if remaining:
            raise MapTripPlanningError(
                "MAP_PLAN_VALIDATION_FAILED",
                "模型生成的地图攻略未通过证据校验，请稍后重试。",
            )
        return revised

    async def _structured(
        self,
        schema: type[SchemaT],
        system_prompt: str,
        user_prompt: str,
        *,
        timeout_seconds: float,
    ) -> SchemaT:
        try:
            return await StructuredOutputService(self._model).invoke(
                schema,
                system_prompt,
                user_prompt,
                timeout_seconds=timeout_seconds,
            )
        except StructuredOutputError as exc:
            raise MapTripPlanningError(
                "MAP_STRUCTURED_OUTPUT_FAILED",
                "模型没有生成有效的地图攻略，请稍后重试。",
            ) from exc

    def _trace(
        self,
        step: str,
        title: str,
        *,
        status: str = "success",
        data: dict[str, Any] | None = None,
    ) -> PlanningTraceEvent:
        self._trace_sequence += 1
        return PlanningTraceEvent(
            sequence=self._trace_sequence,
            step=step,
            title=title,
            status=status,
            data=data or {},
        )


def _generation_prompt(
    request: CityTripRequest,
    evidence: MapTripEvidence,
    weather: TripWeatherEvidence,
) -> str:
    requirements = [
        {
            "day_index": day.day_index,
            "date": day.date.isoformat(),
            "ordered_place_refs": [place.reference_id for place in day.ordered_places()],
        }
        for day in evidence.days
    ]
    return json.dumps(
        {
            "request": request.model_dump(mode="json"),
            "map_evidence": evidence.model_dump(mode="json"),
            "weather_evidence": weather.model_dump(mode="json"),
            "requirements": requirements,
        },
        ensure_ascii=False,
    )


def _stage(
    stage: str,
    display_name: str,
    status: str,
    *,
    detail: str | None = None,
) -> PlanningStageEvent:
    return PlanningStageEvent(
        stage=stage,
        display_name=display_name,
        status=status,
        detail=detail,
    )


__all__ = [
    "MapTripPlanner",
    "MapTripPlanningError",
]
