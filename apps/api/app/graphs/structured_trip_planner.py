from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from langchain_core.language_models import BaseChatModel

from app.core.settings import Settings
from app.schemas.platform_planning import StructuredTripRequest
from app.schemas.tool_execution import (
    ChatStreamEvent,
    MessageDeltaEvent,
    PlanningStageEvent,
    PlanningTraceEvent,
    TravelPlanReadyEvent,
)
from app.schemas.trip_plan_snapshot import TripPlanSnapshotV2
from app.schemas.trip_planning import CityTripRequest
from app.services.map_weather_collection_service import MapWeatherCollectionService
from app.services.restaurant_recommendation_service import RestaurantRecommendationService
from app.services.structured_itinerary_generator import (
    StructuredItineraryGenerationError,
    StructuredItineraryGenerator,
    build_structured_narrative,
    render_structured_itinerary,
)
from app.services.trip_plan_persistence_service import (
    TripPlanVersionArtifact,
    TripPlanVersionWriter,
)
from app.services.trip_plan_snapshot_builder import build_structured_trip_plan_snapshot

logger = logging.getLogger(__name__)


class StructuredTripPlannerError(RuntimeError):
    def __init__(self, code: str, user_message: str) -> None:
        super().__init__(user_message)
        self.code = code
        self.user_message = user_message


class StructuredTripPlanner:
    def __init__(
        self,
        map_weather_service: MapWeatherCollectionService,
        restaurant_service: RestaurantRecommendationService,
        settings: Settings,
        *,
        version_writer: TripPlanVersionWriter | None,
    ) -> None:
        self._map_weather_service = map_weather_service
        self._restaurant_service = restaurant_service
        self._settings = settings
        self._version_writer = version_writer

    async def stream(
        self,
        model: BaseChatModel,
        request: StructuredTripRequest,
        *,
        user_id: UUID,
        plan_id: UUID | None = None,
    ) -> AsyncIterator[ChatStreamEvent]:
        planning_run_id = str(uuid4())
        yield _trace(
            1,
            "request_received",
            "已收到结构化旅游规划表单",
            data={
                "destination_city": request.destination_city,
                "duration_days": request.duration_days,
            },
        )
        yield _stage("validating_request", "正在校验规划表单", "success")

        core_request = CityTripRequest(
            destination_city=request.destination_city,
            start_date=request.start_date,
            duration_days=request.duration_days,
            interests=list(request.interests),
        )
        yield _stage("collecting_pois", "正在召回并编排城市景点", "running")
        yield _stage("collecting_weather", "正在查询行程天气", "running")
        map_weather = await self._map_weather_service.collect(core_request)
        if map_weather.status == "failed" or map_weather.map is None:
            yield _stage(
                "collecting_pois",
                "城市景点规划失败",
                "failed",
                detail="地图核心数据暂不可用。",
            )
            yield _stage(
                "collecting_weather",
                "行程天气查询未完成",
                "failed",
                detail="核心行程未生成。",
            )
            raise StructuredTripPlannerError(
                "MAP_PLANNING_FAILED",
                "暂时无法获取目标城市的地图规划数据，请稍后重试。",
            )

        poi_count = sum(len(day.attractions) for day in map_weather.map.days)
        yield _stage(
            "collecting_pois",
            "城市景点规划完成",
            "partial" if map_weather.map.warnings else "success",
            detail=f"已编排 {len(map_weather.map.days)} 天、{poi_count} 个景点。",
        )
        yield _stage(
            "collecting_weather",
            "行程天气查询完成",
            "partial" if map_weather.weather is None else "success",
        )

        yield _stage("collecting_restaurants", "正在整理城市餐饮推荐", "running")
        restaurants = await self._restaurant_service.collect(request.destination_city)
        yield _stage(
            "collecting_restaurants",
            "城市餐饮推荐已整理",
            (
                "success"
                if restaurants.status == "usable"
                else "partial"
                if restaurants.status == "failed"
                else "skipped"
            ),
            detail=(
                f"已选出 {len(restaurants.recommendations)} 家餐厅。"
                if restaurants.recommendations
                else "没有补充未经验证的餐厅。"
            ),
        )

        snapshot = build_structured_trip_plan_snapshot(
            request,
            map_weather,
            restaurants,
            planning_run_id=planning_run_id,
        )
        _validate_snapshot(snapshot)
        narrative = build_structured_narrative(snapshot)
        fallback = render_structured_itinerary(snapshot, narrative)

        yield _trace(
            2,
            "itinerary_skeleton_ready",
            "结构化行程已通过校验",
            data={
                "day_count": len(snapshot.days),
                "restaurant_count": len(snapshot.restaurant_recommendations),
            },
        )
        yield _stage(
            "generating_itinerary",
            "正在整理旅行攻略文案",
            "running",
        )
        generator = StructuredItineraryGenerator(
            model,
            timeout_seconds=self._settings.trip_planner_model_timeout_seconds,
        )
        output: list[str] = []
        try:
            async for text in generator.stream_markdown(snapshot):
                output.append(text)
                yield MessageDeltaEvent(delta=text)
            generation_status = "success"
        except StructuredItineraryGenerationError:
            logger.exception(
                "Structured itinerary generation failed planning_run_id=%s",
                planning_run_id,
            )
            output = [fallback]
            yield MessageDeltaEvent(delta=fallback)
            generation_status = "partial"

        if self._version_writer is None:
            raise StructuredTripPlannerError(
                "PLAN_STORAGE_UNAVAILABLE",
                "旅行规划已生成，但当前无法保存。",
            )
        yield _stage("saving_itinerary", "正在保存旅行规划", "running")
        saved = await self._version_writer.save_completed_version(
            TripPlanVersionArtifact(
                user_id=user_id,
                plan_id=plan_id,
                snapshot=snapshot,
                presentation_context=snapshot.model_dump(mode="json"),
                narrative=narrative,
                rendered_markdown="".join(output),
                user_instruction=_request_summary(request),
            )
        )
        yield _stage(
            "saving_itinerary",
            "旅行规划已保存",
            "success",
            detail=f"版本 {saved.version}",
        )
        yield TravelPlanReadyEvent(
            plan_id=saved.plan_id,
            version_id=saved.version_id,
            version=saved.version,
        )
        yield _stage(
            "generating_itinerary",
            "旅行攻略生成完成",
            generation_status,
        )
        yield _trace(
            3,
            "response_completed",
            "结构化旅游规划已完成",
            status=generation_status,
            data={"plan_id": str(saved.plan_id), "version": saved.version},
        )


def _validate_snapshot(snapshot: TripPlanSnapshotV2) -> None:
    days = snapshot.days
    request = snapshot.request
    if len(days) != request.duration_days:
        raise StructuredTripPlannerError(
            "INVALID_PLAN_DAY_COUNT",
            "景点数据不足以形成完整的旅行规划，请调整条件后重试。",
        )
    expected_dates = [
        request.start_date + timedelta(days=offset) for offset in range(request.duration_days)
    ]
    if [day.date for day in days] != expected_dates:
        raise StructuredTripPlannerError(
            "INVALID_PLAN_DATES",
            "旅行规划日期不连续，请重新生成。",
        )
    if any(not day.places for day in days):
        raise StructuredTripPlannerError(
            "EMPTY_PLAN_DAY",
            "部分日期没有可用景点，请调整城市或天数后重试。",
        )


def _request_summary(request: StructuredTripRequest) -> str:
    interests = "、".join(item.value for item in request.interests) or "无特定偏好"
    return (
        f"{request.destination_city}，{request.start_date.isoformat()} 出发，"
        f"游玩 {request.duration_days} 天，偏好：{interests}"
    )


def _stage(
    stage: Literal[
        "validating_request",
        "collecting_pois",
        "collecting_weather",
        "collecting_restaurants",
        "generating_itinerary",
        "saving_itinerary",
    ],
    display_name: str,
    status: Literal["running", "success", "partial", "failed", "skipped"],
    *,
    detail: str | None = None,
) -> PlanningStageEvent:
    return PlanningStageEvent(
        stage=stage,
        display_name=display_name,
        status=status,
        detail=detail,
    )


def _trace(
    sequence: int,
    step: Literal[
        "request_received",
        "itinerary_skeleton_ready",
        "response_completed",
    ],
    title: str,
    *,
    status: Literal["running", "success", "partial", "failed", "skipped"] = "success",
    data: dict[str, object] | None = None,
) -> PlanningTraceEvent:
    return PlanningTraceEvent(
        sequence=sequence,
        step=step,
        title=title,
        status=status,
        data=data or {},
        occurred_at=datetime.now(UTC),
    )


__all__ = ["StructuredTripPlanner", "StructuredTripPlannerError"]
