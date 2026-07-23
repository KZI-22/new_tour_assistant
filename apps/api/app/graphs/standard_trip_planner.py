from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

from langchain_core.language_models import BaseChatModel

from app.core.settings import Settings
from app.graphs.trip_planner import TripPlannerGraph, TripPlannerNodeSet
from app.graphs.trip_planner_nodes import (
    ClarifyRequirementsNode,
    EvidenceJoinNode,
    ExtractRequirementsNode,
    GenerateItineraryNode,
    HotelNode,
    MapWeatherNode,
    RenderResponseNode,
    ResolveCapabilitiesNode,
    TransportNode,
    ValidateItineraryNode,
    ValidateRequirementsNode,
)
from app.graphs.trip_planning_state import TripPlanningState
from app.schemas.chat import ChatMessage
from app.schemas.tool_execution import (
    ChatStreamEvent,
    MessageDeltaEvent,
    PlanningStageEvent,
    PlanningTraceEvent,
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
from app.services.weather_evidence_service import WeatherEvidenceService


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
    ) -> None:
        self._map_weather_service = MapWeatherCollectionService(
            collection_service,
            weather_service,
            weather_timeout_seconds=settings.trip_planning_data_timeout_seconds,
        )
        self._flyai_client = flyai_client or _UnavailableFlyAIClient()
        self._settings = settings

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
    ) -> None:
        self._model = model
        self._messages = messages
        self._map_weather_service = map_weather_service
        self._flyai_client = flyai_client
        self._settings = settings
        self._route_source = route_source
        self._execution_context = execution_context
        self._planning_run_id = str(uuid4())
        self._trace_sequence = 0

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
                generate_itinerary=GenerateItineraryNode(
                    TripItineraryGenerator(
                        self._model,
                        timeout_seconds=(self._settings.trip_planner_model_timeout_seconds),
                    )
                ),
                validate_itinerary=ValidateItineraryNode(),
                render_response=RenderResponseNode(),
            )
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
                            "extraction_method": state.get(
                                "extraction_method",
                                "unknown",
                            ),
                            **state.get("extraction_overrides", {}),
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
                        "hotel_enabled": plan.hotel.enabled,
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
                        "正在整理统一旅行方案",
                        "running",
                    )
                )
        elif node == "generate_itinerary":
            if state.get("revision_count", 0):
                events.append(
                    _stage(
                        "revising_itinerary",
                        "正在修订未通过校验的旅行方案",
                        "success",
                    )
                )
            events.extend(
                [
                    _stage(
                        "generating_itinerary",
                        "正在整理统一旅行方案",
                        "success",
                    ),
                    self._trace(
                        "itinerary_generated",
                        "统一旅行方案文案生成完成",
                        data={
                            "revision_count": state.get(
                                "revision_count",
                                0,
                            )
                        },
                    ),
                    _stage(
                        "validating_itinerary",
                        "正在校验日期、引用与能力输出",
                        "running",
                    ),
                ]
            )
        elif node == "validate_itinerary":
            issues = state.get("validation_issues", [])
            if issues:
                events.append(
                    _stage(
                        "validating_itinerary",
                        "正在校验日期、引用与能力输出",
                        ("partial" if state.get("revision_count", 0) < 1 else "failed"),
                        detail=f"检测到 {len(issues)} 个确定性校验问题。",
                    )
                )
            else:
                events.extend(
                    [
                        _stage(
                            "validating_itinerary",
                            "正在校验日期、引用与能力输出",
                            "success",
                        ),
                        self._trace(
                            "validation_completed",
                            "统一旅行方案确定性校验通过",
                            data={
                                "revision_count": state.get(
                                    "revision_count",
                                    0,
                                )
                            },
                        ),
                        _stage(
                            "finalizing",
                            "正在渲染最终旅行方案",
                            "running",
                        ),
                    ]
                )
        elif node == "prepare_revision":
            events.append(
                _stage(
                    "revising_itinerary",
                    "正在修订未通过校验的旅行方案",
                    "running",
                )
            )
        elif node == "render_response":
            answer = cast(str, result["final_answer"])
            events.extend(
                [
                    MessageDeltaEvent(delta=answer),
                    self._trace(
                        "response_completed",
                        "最终统一旅行方案已渲染",
                        data={"output_chars": len(answer)},
                    ),
                    _stage(
                        "finalizing",
                        "正在渲染最终旅行方案",
                        "success",
                    ),
                ]
            )
        elif node == "controlled_failure":
            answer = cast(str, result["final_answer"])
            events.extend(
                [
                    MessageDeltaEvent(delta=answer),
                    self._trace(
                        "validation_completed",
                        "统一旅行方案未通过确定性校验",
                        status="failed",
                        data={
                            "revision_count": state.get(
                                "revision_count",
                                0,
                            ),
                            "issue_codes": [
                                issue.code
                                for issue in state.get(
                                    "validation_issues",
                                    [],
                                )
                            ],
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
