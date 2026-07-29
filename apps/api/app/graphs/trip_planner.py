from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from app.graphs.trip_planning_state import TripPlanningState
from app.services.trip_planner_logging import logged_trip_planner_node

TripPlannerNode = Callable[[TripPlanningState], Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class TripPlannerNodeSet:
    extract_requirements: TripPlannerNode
    resolve_capabilities: TripPlannerNode
    validate_requirements: TripPlannerNode
    clarify_requirements: TripPlannerNode
    collect_map_weather: TripPlannerNode
    collect_transport: TripPlannerNode
    collect_hotels: TripPlannerNode
    join_evidence: TripPlannerNode
    build_itinerary_skeleton: TripPlannerNode
    generate_itinerary: TripPlannerNode
    validate_itinerary: TripPlannerNode
    render_response: TripPlannerNode


class TripPlannerGraph:
    def __init__(
        self,
        nodes: TripPlannerNodeSet,
        *,
        finish_after_skeleton: bool = False,
    ) -> None:
        self._nodes = nodes
        self._finish_after_skeleton = finish_after_skeleton
        self._graph = self._build_graph()

    def _build_graph(self) -> Any:
        workflow = StateGraph(TripPlanningState)
        workflow.add_node(
            "extract_requirements",
            logged_trip_planner_node(
                "extract_requirements",
                self._nodes.extract_requirements,
            ),
        )
        workflow.add_node(
            "resolve_capabilities",
            logged_trip_planner_node(
                "resolve_capabilities",
                self._nodes.resolve_capabilities,
            ),
        )
        workflow.add_node(
            "validate_requirements",
            logged_trip_planner_node(
                "validate_requirements",
                self._nodes.validate_requirements,
            ),
        )
        workflow.add_node(
            "clarify_requirements",
            logged_trip_planner_node(
                "clarify_requirements",
                self._nodes.clarify_requirements,
            ),
        )
        workflow.add_node(
            "dispatch_collection",
            logged_trip_planner_node(
                "dispatch_collection",
                _dispatch_collection,
            ),
        )
        workflow.add_node(
            "collect_map_weather",
            logged_trip_planner_node(
                "collect_map_weather",
                self._nodes.collect_map_weather,
            ),
        )
        workflow.add_node(
            "collect_transport",
            logged_trip_planner_node(
                "collect_transport",
                self._nodes.collect_transport,
            ),
        )
        workflow.add_node(
            "collect_hotels",
            logged_trip_planner_node(
                "collect_hotels",
                self._nodes.collect_hotels,
            ),
        )
        workflow.add_node(
            "join_evidence",
            logged_trip_planner_node(
                "join_evidence",
                self._nodes.join_evidence,
            ),
        )
        workflow.add_node(
            "build_itinerary_skeleton",
            logged_trip_planner_node(
                "build_itinerary_skeleton",
                self._nodes.build_itinerary_skeleton,
            ),
        )
        workflow.add_node(
            "generate_itinerary",
            logged_trip_planner_node(
                "generate_itinerary",
                self._nodes.generate_itinerary,
            ),
        )
        workflow.add_node(
            "validate_itinerary",
            logged_trip_planner_node(
                "validate_itinerary",
                self._nodes.validate_itinerary,
            ),
        )
        workflow.add_node(
            "render_response",
            logged_trip_planner_node(
                "render_response",
                self._nodes.render_response,
            ),
        )
        workflow.add_node(
            "controlled_failure",
            logged_trip_planner_node(
                "controlled_failure",
                _controlled_failure,
            ),
        )

        workflow.add_edge(START, "extract_requirements")
        workflow.add_edge("extract_requirements", "resolve_capabilities")
        workflow.add_edge("resolve_capabilities", "validate_requirements")
        workflow.add_conditional_edges(
            "validate_requirements",
            _route_after_requirement_check,
            {
                "clarify": "clarify_requirements",
                "collect": "dispatch_collection",
            },
        )
        workflow.add_edge("clarify_requirements", END)

        workflow.add_edge("dispatch_collection", "collect_map_weather")
        workflow.add_edge("dispatch_collection", "collect_transport")
        workflow.add_edge("dispatch_collection", "collect_hotels")
        workflow.add_edge(
            ["collect_map_weather", "collect_transport", "collect_hotels"],
            "join_evidence",
        )
        workflow.add_conditional_edges(
            "join_evidence",
            _route_after_evidence_join,
            {
                "generate": "build_itinerary_skeleton",
                "fail": "controlled_failure",
            },
        )
        if self._finish_after_skeleton:
            workflow.add_conditional_edges(
                "build_itinerary_skeleton",
                _route_after_skeleton_validation,
                {
                    "generate": END,
                    "fail": "controlled_failure",
                },
            )
        else:
            workflow.add_conditional_edges(
                "build_itinerary_skeleton",
                _route_after_skeleton_validation,
                {
                    "generate": "generate_itinerary",
                    "fail": "controlled_failure",
                },
            )
        workflow.add_edge("generate_itinerary", "validate_itinerary")
        workflow.add_conditional_edges(
            "validate_itinerary",
            _route_after_validation,
            {
                "render": "render_response",
                "fail": "controlled_failure",
            },
        )
        workflow.add_edge("render_response", END)
        workflow.add_edge("controlled_failure", END)
        return workflow.compile()

    async def ainvoke(
        self,
        initial_state: TripPlanningState,
    ) -> TripPlanningState:
        state: TripPlanningState = {
            "planning_run_id": str(uuid4()),
            **initial_state,
        }
        result = await self._graph.ainvoke(state)
        return cast(TripPlanningState, result)

    async def astream(
        self,
        initial_state: TripPlanningState,
        *,
        stream_mode: str = "updates",
    ) -> AsyncIterator[object]:
        state: TripPlanningState = {
            "planning_run_id": str(uuid4()),
            **initial_state,
        }
        async for event in self._graph.astream(state, stream_mode=stream_mode):
            yield event


async def _dispatch_collection(_: TripPlanningState) -> dict[str, Any]:
    return {"current_stage": "collecting_evidence"}


async def _controlled_failure(_: TripPlanningState) -> dict[str, Any]:
    return {
        "controlled_error": True,
        "final_answer": "旅行计划未通过确定性校验，请稍后重试。",
        "current_stage": "failed",
    }


def _route_after_requirement_check(
    state: TripPlanningState,
) -> Literal["clarify", "collect"]:
    check = state.get("requirement_check")
    return "collect" if check is not None and check.complete else "clarify"


def _route_after_evidence_join(
    state: TripPlanningState,
) -> Literal["generate", "fail"]:
    evidence = state.get("joined_evidence")
    return "fail" if evidence is None or evidence.overall_status == "failed" else "generate"


def _route_after_skeleton_validation(
    state: TripPlanningState,
) -> Literal["generate", "fail"]:
    return "fail" if state.get("skeleton_validation_issues") else "generate"


def _route_after_validation(
    state: TripPlanningState,
) -> Literal["render", "fail"]:
    return "fail" if state.get("validation_issues") else "render"


__all__ = [
    "TripPlannerGraph",
    "TripPlannerNode",
    "TripPlannerNodeSet",
]
