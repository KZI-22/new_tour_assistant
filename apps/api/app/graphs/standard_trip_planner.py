from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

from langchain_core.language_models import BaseChatModel

from app.core.settings import Settings
from app.graphs.trip_planner import TripPlannerGraph, TripPlannerNodeSet
from app.graphs.trip_planner_nodes import (
    BuildItinerarySkeletonNode,
    ClarifyRequirementsNode,
    EvidenceJoinNode,
    ExtractRequirementsNode,
    HotelNode,
    MapWeatherNode,
    ResolveCapabilitiesNode,
    TransportNode,
    ValidateRequirementsNode,
)
from app.graphs.trip_planning_state import TripPlanningState
from app.schemas.chat import ChatMessage
from app.schemas.tool_execution import (
    ChatStreamEvent,
    MessageDeltaEvent,
    PlanningStageEvent,
    PlanningTraceEvent,
    TravelPlanReadyEvent,
)
from app.schemas.travel import (
    FlightSearchInput,
    FlyAIErrorCode,
    FlyAIResult,
    HotelSearchInput,
    TrainSearchInput,
)
from app.schemas.trip_evidence import EvidenceStatus, RawCapabilityEvidence
from app.services.agent_executor import AgentExecutionError
from app.services.hotel_search_service import HotelSearchService
from app.services.intercity_transport_service import IntercityTransportService
from app.services.map_trip_collection_service import MapTripCollectionService
from app.services.map_weather_collection_service import MapWeatherCollectionService
from app.services.tool_execution import ToolExecutionContext
from app.services.trip_itinerary_generator import (
    TripItineraryGenerationError,
    TripItineraryGenerator,
)
from app.services.trip_itinerary_renderer import split_trip_itinerary_sections
from app.services.trip_plan_persistence_service import (
    TripPlanVersionArtifact,
    TripPlanVersionWriter,
)
from app.services.trip_presentation_context import build_trip_presentation_context
from app.services.weather_evidence_service import WeatherEvidenceService

logger = logging.getLogger(__name__)


class FlyAITripClient(Protocol):
    async def search_flight(self, query: FlightSearchInput) -> FlyAIResult: ...

    async def search_train(self, query: TrainSearchInput) -> FlyAIResult: ...

    async def search_hotel(self, query: HotelSearchInput) -> FlyAIResult: ...


class StandardTripPlanner:
    def __init__(
        self,
        collection_service: MapTripCollectionService,
        weather_service: WeatherEvidenceService,
        flyai_client: FlyAITripClient | None,
        settings: Settings,
        *,
        version_writer: TripPlanVersionWriter | None = None,
    ) -> None:
        self._map_weather_service = MapWeatherCollectionService(
            collection_service,
            weather_service,
            weather_timeout_seconds=settings.trip_planning_data_timeout_seconds,
        )
        self._flyai_client = flyai_client or _UnavailableFlyAIClient()
        self._settings = settings
        self._version_writer = version_writer

    async def stream(
        self,
        model: BaseChatModel,
        messages: list[ChatMessage],
        *,
        route_source: Literal["llm_router", "fallback", "explicit"] = "explicit",
        execution_context: ToolExecutionContext | None = None,
    ) -> AsyncIterator[ChatStreamEvent]:
        run = _StandardTripPlanningRun(
            model=model,
            messages=messages,
            map_weather_service=self._map_weather_service,
            flyai_client=self._flyai_client,
            settings=self._settings,
            route_source=route_source,
            execution_context=execution_context,
            version_writer=self._version_writer,
        )
        try:
            async for event in run.stream():
                yield event
        except TripItineraryGenerationError as exc:
            raise AgentExecutionError(
                "TRIP_ITINERARY_GENERATION_FAILED",
                "统一旅行方案暂时无法生成，请稍后重试。",
            ) from exc


class _StandardTripPlanningRun:
    def __init__(
        self,
        *,
        model: BaseChatModel,
        messages: list[ChatMessage],
        map_weather_service: MapWeatherCollectionService,
        flyai_client: FlyAITripClient,
        settings: Settings,
        route_source: Literal["llm_router", "fallback", "explicit"],
        execution_context: ToolExecutionContext | None,
        version_writer: TripPlanVersionWriter | None,
    ) -> None:
        self._model = model
        self._messages = messages
        self._map_weather_service = map_weather_service
        self._flyai_client = flyai_client
        self._settings = settings
        self._route_source = route_source
        self._execution_context = execution_context
        self._version_writer = version_writer
        self._planning_run_id = str(uuid4())
        self._trace_sequence = 0
        self._itinerary_generator = TripItineraryGenerator(
            self._model,
            timeout_seconds=self._settings.trip_planner_model_timeout_seconds,
        )

    async def stream(self) -> AsyncIterator[ChatStreamEvent]:
        yield self._trace(
            "request_received",
            "收到统一旅行规划请求",
            data={"conversation_message_count": len(self._messages)},
        )
        yield self._trace(
            "route_selected",
            "请求已路由到统一旅行规划图",
            data={
                "route": "trip_planner_graph",
                "route_source": self._route_source,
            },
        )
        yield _stage(
            "understanding_request",
            "正在提取行程与可选能力需求",
            "running",
        )

        graph = self._build_graph()
        state: TripPlanningState = {
            "messages": self._messages,
            "planning_run_id": self._planning_run_id,
        }
        if self._execution_context is not None:
            state["conversation_id"] = str(self._execution_context.conversation_id)
            state["assistant_message_id"] = str(self._execution_context.assistant_message_id)

        async for update_object in graph.astream(state):
            if not isinstance(update_object, dict):
                continue
            for node, raw_result in update_object.items():
                if not isinstance(raw_result, dict):
                    continue
                result = cast(dict[str, Any], raw_result)
                state.update(cast(TripPlanningState, result))
                for event in self._events_for_update(node, state, result):
                    yield event

        if (
            state.get("joined_evidence") is not None
            and state.get("plan_snapshot") is not None
            and state.get("skeleton_answer")
            and not state.get("skeleton_validation_issues")
            and not state.get("final_answer")
        ):
            async for event in self._stream_itinerary_markdown(state):
                yield event

    def _build_graph(self) -> TripPlannerGraph:
        return TripPlannerGraph(
            TripPlannerNodeSet(
                extract_requirements=ExtractRequirementsNode(
                    self._model,
                    timeout_seconds=(
                        self._settings.trip_planner_request_extraction_timeout_seconds
                    ),
                ),
                resolve_capabilities=ResolveCapabilitiesNode(),
                validate_requirements=ValidateRequirementsNode(self._settings),
                clarify_requirements=ClarifyRequirementsNode(),
                collect_map_weather=MapWeatherNode(self._map_weather_service),
                collect_transport=TransportNode(IntercityTransportService(self._flyai_client)),
                collect_hotels=HotelNode(HotelSearchService(self._flyai_client)),
                join_evidence=EvidenceJoinNode(),
                build_itinerary_skeleton=BuildItinerarySkeletonNode(),
            )
        )

    async def _stream_itinerary_markdown(
        self,
        state: TripPlanningState,
    ) -> AsyncIterator[ChatStreamEvent]:
        output_chars = 0
        output_chunks = 0
        output_parts: list[str] = []
        yield _stage(
            "finalizing",
            "正在流式生成最终旅行方案",
            "running",
            detail="模型生成的文本会实时展示。",
        )
        try:
            async for text in self._itinerary_generator.stream_markdown(state["plan_snapshot"]):
                output_chars += len(text)
                output_chunks += 1
                output_parts.append(text)
                yield MessageDeltaEvent(delta=text)
        except TripItineraryGenerationError as exc:
            logger.warning(
                "event=trip_markdown_stream_fallback planning_run_id=%s "
                "error_code=%s streamed_chars=%d streamed_chunks=%d",
                self._planning_run_id,
                exc.code,
                output_chars,
                output_chunks,
            )
            fallback = cast(str, state["skeleton_answer"])
            if output_chars:
                fallback = (
                    "\n\n---\n\n"
                    "> 文案生成中断，下面补充根据已校验信息生成的完整行程。\n\n"
                    f"{fallback}"
                )
            fallback_chunks = split_trip_itinerary_sections(fallback)
            for chunk in fallback_chunks:
                output_chars += len(chunk)
                output_chunks += 1
                output_parts.append(chunk)
                yield MessageDeltaEvent(delta=chunk)
            async for event in self._persist_completed_plan(
                state,
                rendered_markdown="".join(output_parts),
            ):
                yield event
            yield _stage(
                "generating_itinerary",
                "旅行文案生成未完整结束",
                "partial",
                detail="已返回确定性行程作为兜底。",
            )
            yield self._trace(
                "itinerary_generated",
                "模型文案未完整生成，已使用确定性行程兜底",
                status="partial",
                data={"fallback": "deterministic_skeleton", "error_code": exc.code},
            )
            yield self._trace(
                "response_completed",
                "最终统一旅行方案已返回",
                status="partial",
                data={
                    "output_chars": output_chars,
                    "output_chunks": output_chunks,
                    "streamed": True,
                    "fallback": "deterministic_skeleton",
                },
            )
            yield _stage(
                "finalizing",
                "正在流式生成最终旅行方案",
                "partial",
                detail="模型文案生成中断，已补充确定性行程。",
            )
            return

        async for event in self._persist_completed_plan(
            state,
            rendered_markdown="".join(output_parts),
        ):
            yield event
        yield _stage(
            "generating_itinerary",
            "旅行文案流式生成完成",
            "success",
        )
        yield self._trace(
            "itinerary_generated",
            "统一旅行方案文案流式生成完成",
            data={"output_chars": output_chars, "output_chunks": output_chunks},
        )
        yield self._trace(
            "response_completed",
            "最终统一旅行方案已流式返回",
            data={
                "output_chars": output_chars,
                "output_chunks": output_chunks,
                "streamed": True,
            },
        )
        yield _stage(
            "finalizing",
            "正在流式生成最终旅行方案",
            "success",
        )

    async def _persist_completed_plan(
        self,
        state: TripPlanningState,
        *,
        rendered_markdown: str,
    ) -> AsyncIterator[ChatStreamEvent]:
        if self._version_writer is None or self._execution_context is None:
            return

        yield _stage(
            "saving_itinerary",
            "正在保存旅行规划版本",
            "running",
        )
        latest_user_message = next(
            (message.content for message in reversed(self._messages) if message.role == "user"),
            "",
        )
        snapshot = state["plan_snapshot"]
        try:
            saved = await self._version_writer.save_completed_version(
                TripPlanVersionArtifact(
                    conversation_id=self._execution_context.conversation_id,
                    assistant_message_id=self._execution_context.assistant_message_id,
                    snapshot=snapshot,
                    presentation_context=build_trip_presentation_context(snapshot),
                    narrative=state["narrative_skeleton"],
                    rendered_markdown=rendered_markdown,
                    user_instruction=latest_user_message,
                )
            )
        except Exception:
            logger.exception(
                "event=trip_plan_version_save_failed planning_run_id=%s",
                self._planning_run_id,
            )
            yield _stage(
                "saving_itinerary",
                "旅行规划版本保存失败",
                "failed",
                detail="本次规划已生成，但暂时无法用于后续版本编辑。",
            )
            return

        yield _stage(
            "saving_itinerary",
            "旅行规划版本已保存",
            "success",
            detail=f"版本 {saved.version}",
        )
        yield TravelPlanReadyEvent(
            plan_id=saved.plan_id,
            version_id=saved.version_id,
            version=saved.version,
        )

    def _events_for_update(
        self,
        node: str,
        state: TripPlanningState,
        result: dict[str, Any],
    ) -> list[ChatStreamEvent]:
        events: list[ChatStreamEvent] = []
        if node == "extract_requirements":
            request = state["request"]
            events.extend(
                [
                    _stage(
                        "understanding_request",
                        "正在提取行程与可选能力需求",
                        "success",
                    ),
                    self._trace(
                        "requirements_extracted",
                        "已提取统一旅行规划需求",
                        data={
                            "destination_city": request.core.destination_city,
                            "duration_days": request.core.duration_days,
                            "start_date": (
                                request.core.start_date.isoformat()
                                if request.core.start_date
                                else None
                            ),
                            "transport_action": request.transport.action.value,
                            "transport_modes": [mode.value for mode in request.transport.modes],
                            "transport_journey_scope": (request.transport.journey_scope.value),
                            "transport_origin_city": request.transport.origin_city,
                            "hotel_action": request.hotel.action.value,
                            "hotel_nearby_poi": request.hotel.nearby_poi,
                            "extraction_method": state.get(
                                "extraction_method",
                                "unknown",
                            ),
                            **state.get("extraction_overrides", {}),
                            **state.get("extraction_details", {}),
                        },
                    ),
                    _stage(
                        "checking_requirements",
                        "正在解析能力并检查统一缺失项",
                        "running",
                    ),
                ]
            )
        elif node == "resolve_capabilities":
            plan = state["capability_plan"]
            events.append(
                self._trace(
                    "requirements_extracted",
                    "已解析本轮能力执行计划",
                    data={
                        "map_weather_enabled": True,
                        "transport_enabled": plan.transport.enabled,
                        "transport_modes": [mode.value for mode in plan.transport.modes],
                        "journey_scope": plan.transport.journey_scope.value,
                        "transport_origin": plan.transport.origin,
                        "transport_destination": plan.transport.destination,
                        "transport_outbound_date": (
                            plan.transport.outbound_date.isoformat()
                            if plan.transport.outbound_date
                            else None
                        ),
                        "transport_return_date": (
                            plan.transport.return_date.isoformat()
                            if plan.transport.return_date
                            else None
                        ),
                        "hotel_enabled": plan.hotel.enabled,
                        "hotel_destination": plan.hotel.destination,
                        "hotel_check_in_date": (
                            plan.hotel.check_in_date.isoformat()
                            if plan.hotel.check_in_date
                            else None
                        ),
                        "hotel_check_out_date": (
                            plan.hotel.check_out_date.isoformat()
                            if plan.hotel.check_out_date
                            else None
                        ),
                        "hotel_nearby_poi": plan.hotel.nearby_poi,
                        "derivation_fields": [item.field for item in plan.derivations],
                    },
                )
            )
        elif node == "validate_requirements":
            check = state["requirement_check"]
            events.extend(
                [
                    _stage(
                        "checking_requirements",
                        "正在解析能力并检查统一缺失项",
                        "success" if check.complete else "partial",
                        detail=(
                            None if check.complete else f"需要补充 {len(check.missing)} 项信息。"
                        ),
                    ),
                    self._trace(
                        "requirements_validated",
                        "统一规划参数检查完成",
                        status="success" if check.complete else "partial",
                        data={
                            "complete": check.complete,
                            "missing_fields": [item.field for item in check.missing],
                            "validation_error_count": len(check.errors),
                        },
                    ),
                ]
            )
        elif node == "clarify_requirements":
            events.append(MessageDeltaEvent(delta=cast(str, result["final_answer"])))
        elif node == "dispatch_collection":
            plan = state["capability_plan"]
            events.extend(
                [
                    _stage(
                        "collecting_pois",
                        "正在召回、筛选并编排高德景点",
                        "running",
                    ),
                    _stage(
                        "collecting_weather",
                        "正在查询行程日期对应的天气",
                        "running",
                    ),
                    _stage(
                        "collecting_transport",
                        "正在查询城际交通",
                        "running" if plan.transport.enabled else "skipped",
                    ),
                    _stage(
                        "collecting_hotels",
                        "正在查询酒店",
                        "running" if plan.hotel.enabled else "skipped",
                    ),
                ]
            )
        elif node == "collect_map_weather":
            bundle = state["map_weather_evidence"]
            if bundle.status == "failed":
                events.extend(
                    [
                        _stage(
                            "collecting_pois",
                            "正在召回、筛选并编排高德景点",
                            "failed",
                            detail="地图核心证据不可用。",
                        ),
                        _stage(
                            "collecting_weather",
                            "正在查询行程日期对应的天气",
                            "failed",
                            detail="地图核心证据不可用。",
                        ),
                    ]
                )
            else:
                assert bundle.map is not None
                assert bundle.weather is not None
                weather_coverage = sum(day.coverage == "available" for day in bundle.weather.days)
                events.extend(
                    [
                        _stage(
                            "collecting_pois",
                            "正在召回、筛选并编排高德景点",
                            ("partial" if bundle.map.warnings else "success"),
                            detail=f"已形成 {len(bundle.map.days)} 个分日地图证据包。",
                        ),
                        _stage(
                            "collecting_weather",
                            "正在查询行程日期对应的天气",
                            (
                                "success"
                                if weather_coverage == len(bundle.weather.days)
                                else "partial"
                            ),
                            detail=(
                                f"天气预报覆盖 {weather_coverage}/"
                                f"{len(bundle.weather.days)} 个行程日。"
                            ),
                        ),
                    ]
                )
        elif node == "collect_transport":
            events.append(
                _optional_stage(
                    "collecting_transport",
                    "正在查询城际交通",
                    state["transport_evidence"],
                )
            )
        elif node == "collect_hotels":
            events.append(
                _optional_stage(
                    "collecting_hotels",
                    "正在查询酒店",
                    state["hotel_evidence"],
                )
            )
        elif node == "join_evidence":
            evidence = state["joined_evidence"]
            events.extend(
                [
                    self._trace(
                        "evidence_selected",
                        "地图、天气与可选证据已汇合",
                        status=(
                            "success"
                            if evidence.overall_status == "usable"
                            else ("partial" if evidence.overall_status == "partial" else "failed")
                        ),
                        data={
                            "map_weather_status": evidence.map_weather.status,
                            "transport_status": evidence.transport.status.value,
                            "hotel_status": evidence.hotel.status.value,
                            "overall_status": evidence.overall_status,
                        },
                    ),
                ]
            )
            if evidence.overall_status != "failed":
                events.append(
                    _stage(
                        "generating_itinerary",
                        "正在生成确定性旅行骨架",
                        "running",
                    )
                )
        elif node == "build_itinerary_skeleton":
            issues = state.get("skeleton_validation_issues", [])
            if issues:
                events.append(
                    _stage(
                        "generating_itinerary",
                        "正在生成确定性旅行骨架",
                        "failed",
                        detail=f"骨架校验发现 {len(issues)} 个问题。",
                    )
                )
            else:
                events.extend(
                    [
                        self._trace(
                            "itinerary_skeleton_ready",
                            "确定性旅行骨架已生成并通过校验",
                            data={
                                "output_chars": len(state["skeleton_answer"]),
                            },
                        ),
                        self._trace(
                            "validation_completed",
                            "确定性旅行骨架校验通过",
                            data={"scope": "deterministic_skeleton"},
                        ),
                        _stage(
                            "generating_itinerary",
                            "确定性旅行骨架已就绪，正在生成文案",
                            "running",
                            detail="模型输出将从首个文本片段开始实时展示。",
                        ),
                    ]
                )
        elif node == "controlled_failure":
            answer = cast(str, result["final_answer"])
            issues = state.get("skeleton_validation_issues", [])
            events.extend(
                [
                    MessageDeltaEvent(delta=answer),
                    self._trace(
                        "validation_completed",
                        "统一旅行方案未通过确定性校验",
                        status="failed",
                        data={
                            "issue_codes": [issue.code for issue in issues],
                        },
                    ),
                ]
            )
        return events

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
            step=step,  # type: ignore[arg-type]
            title=title,
            status=status,  # type: ignore[arg-type]
            data=data or {},
        )


class _UnavailableFlyAIClient:
    async def search_flight(
        self,
        _: FlightSearchInput,
    ) -> FlyAIResult:
        return _unavailable_result()

    async def search_train(
        self,
        _: TrainSearchInput,
    ) -> FlyAIResult:
        return _unavailable_result()

    async def search_hotel(
        self,
        _: HotelSearchInput,
    ) -> FlyAIResult:
        return _unavailable_result()


def _unavailable_result() -> FlyAIResult:
    return FlyAIResult(
        success=False,
        command=[],
        error_code=FlyAIErrorCode.CLI_NOT_FOUND,
        error_message="FlyAI client is unavailable.",
        duration_ms=0,
    )


def _optional_stage(
    stage: Literal["collecting_transport", "collecting_hotels"],
    display_name: str,
    evidence: RawCapabilityEvidence,
) -> PlanningStageEvent:
    statuses = {
        EvidenceStatus.SKIPPED: "skipped",
        EvidenceStatus.USABLE: "success",
        EvidenceStatus.EMPTY: "partial",
        EvidenceStatus.FAILED: "failed",
    }
    details = {
        EvidenceStatus.SKIPPED: "用户未明确要求，本次未执行。",
        EvidenceStatus.USABLE: "已获得可用查询结果。",
        EvidenceStatus.EMPTY: "当前条件下未查询到结果。",
        EvidenceStatus.FAILED: "可选查询暂时不可用，地图与天气行程继续生成。",
    }
    return _stage(
        stage,
        display_name,
        statuses[evidence.status],  # type: ignore[arg-type]
        detail=details[evidence.status],
    )


def _stage(
    stage: str,
    display_name: str,
    status: str,
    *,
    detail: str | None = None,
) -> PlanningStageEvent:
    return PlanningStageEvent(
        stage=stage,  # type: ignore[arg-type]
        display_name=display_name,
        status=status,  # type: ignore[arg-type]
        detail=detail,
    )


__all__ = ["StandardTripPlanner"]
