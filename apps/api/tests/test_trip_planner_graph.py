from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

import pytest
from app.graphs.trip_planner import TripPlannerGraph, TripPlannerNodeSet
from app.graphs.trip_planning_state import TripPlanningState
from app.schemas.chat import ChatMessage
from app.schemas.map_planning import MapTripEvidence
from app.schemas.trip_capabilities import (
    CapabilityPlan,
    HotelCapabilityPlan,
    RequirementCheck,
    TransportCapabilityPlan,
    TripPlanningRequest,
)
from app.schemas.trip_evidence import (
    EvidenceStatus,
    JoinedTripEvidence,
    MapWeatherEvidenceBundle,
    RawCapabilityEvidence,
)
from app.schemas.trip_validation import ValidationIssue

Node = Callable[[TripPlanningState], Awaitable[dict[str, Any]]]
NOW = datetime(2026, 7, 23, tzinfo=UTC)


def _request() -> TripPlanningRequest:
    return TripPlanningRequest(
        core={
            "destination_city": "成都",
            "duration_days": 3,
            "start_date": date(2026, 8, 1),
        }
    )


def _raw(capability: str, status: EvidenceStatus) -> RawCapabilityEvidence:
    return RawCapabilityEvidence(
        capability=capability,
        status=status,
        query={},
        queried_at=NOW,
        duration_ms=0,
        data={"ok": True} if status is EvidenceStatus.USABLE else None,
    )


def _node(**updates: object) -> Node:
    async def run(_: TripPlanningState) -> dict[str, Any]:
        return dict(updates)

    return run


def _node_set(
    *,
    plan: CapabilityPlan | None = None,
    requirement_check: RequirementCheck | None = None,
    map_node: Node | None = None,
    transport_node: Node | None = None,
    hotel_node: Node | None = None,
    join_node: Node | None = None,
    skeleton_node: Node | None = None,
    generate_node: Node | None = None,
    validate_node: Node | None = None,
    render_node: Node | None = None,
    clarify_node: Node | None = None,
) -> TripPlannerNodeSet:
    request = _request()
    capability_plan = plan or CapabilityPlan()

    async def default_join(state: TripPlanningState) -> dict[str, Any]:
        map_weather = state["map_weather_evidence"]
        transport = state["transport_evidence"]
        hotel = state["hotel_evidence"]
        optional_partial = any(
            evidence.status in {EvidenceStatus.EMPTY, EvidenceStatus.FAILED}
            for evidence in (transport, hotel)
        )
        joined = JoinedTripEvidence(
            request=state["request"],
            capabilities=state["capability_plan"],
            map_weather=map_weather,
            transport=transport,
            hotel=hotel,
            overall_status=(
                "failed"
                if map_weather.status == "failed"
                else "partial"
                if optional_partial
                else "usable"
            ),
        )
        return {"joined_evidence": joined}

    return TripPlannerNodeSet(
        extract_requirements=_node(request=request),
        resolve_capabilities=_node(capability_plan=capability_plan),
        validate_requirements=_node(
            requirement_check=requirement_check or RequirementCheck(complete=True)
        ),
        clarify_requirements=clarify_node or _node(final_answer="请补充规划信息。"),
        collect_map_weather=map_node
        or _node(
            map_weather_evidence=MapWeatherEvidenceBundle(
                status="usable",
                map=MapTripEvidence(
                    city="成都",
                    planning_run_id="map-run",
                    days=[],
                ),
            )
        ),
        collect_transport=transport_node
        or _node(
            transport_evidence=_raw(
                "transport",
                (
                    EvidenceStatus.USABLE
                    if capability_plan.transport.enabled
                    else EvidenceStatus.SKIPPED
                ),
            )
        ),
        collect_hotels=hotel_node
        or _node(
            hotel_evidence=_raw(
                "hotel",
                (
                    EvidenceStatus.USABLE
                    if capability_plan.hotel.enabled
                    else EvidenceStatus.SKIPPED
                ),
            )
        ),
        join_evidence=join_node or default_join,
        build_itinerary_skeleton=skeleton_node
        or _node(
            narrative_skeleton={"draft": True},
            skeleton_validation_issues=[],
            skeleton_answer="预览",
        ),
        generate_itinerary=generate_node or _node(narrative={"draft": True}),
        validate_itinerary=validate_node or _node(validation_issues=[]),
        render_response=render_node or _node(final_answer="完成"),
    )


@pytest.mark.asyncio
async def test_incomplete_requirements_clarify_without_collecting() -> None:
    collection_called = False

    async def forbidden_collection(_: TripPlanningState) -> dict[str, Any]:
        nonlocal collection_called
        collection_called = True
        raise AssertionError("collection must not run before requirements are complete")

    graph = TripPlannerGraph(
        _node_set(
            requirement_check=RequirementCheck(
                complete=False,
                missing=[
                    {
                        "field": "core.start_date",
                        "capability": "core",
                        "display_name": "出行开始日期",
                        "reason": "地图规划需要日期",
                    }
                ],
            ),
            map_node=forbidden_collection,
            transport_node=forbidden_collection,
            hotel_node=forbidden_collection,
        )
    )

    result = await graph.ainvoke({"messages": [ChatMessage(role="user", content="成都三日游")]})

    assert result["final_answer"] == "请补充规划信息。"
    assert collection_called is False
    assert "joined_evidence" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transport_enabled", "hotel_enabled"),
    [(False, False), (True, False), (False, True), (True, True)],
)
async def test_capability_combinations_join_once(
    transport_enabled: bool,
    hotel_enabled: bool,
) -> None:
    join_count = 0
    plan = CapabilityPlan(
        transport=TransportCapabilityPlan(enabled=transport_enabled),
        hotel=HotelCapabilityPlan(enabled=hotel_enabled),
    )
    base_nodes = _node_set(plan=plan)
    original_join = base_nodes.join_evidence

    async def counted_join(state: TripPlanningState) -> dict[str, Any]:
        nonlocal join_count
        join_count += 1
        return await original_join(state)

    graph = TripPlannerGraph(replace(base_nodes, join_evidence=counted_join))

    result = await graph.ainvoke({"messages": []})

    assert join_count == 1
    assert result["transport_evidence"].status is (
        EvidenceStatus.USABLE if transport_enabled else EvidenceStatus.SKIPPED
    )
    assert result["hotel_evidence"].status is (
        EvidenceStatus.USABLE if hotel_enabled else EvidenceStatus.SKIPPED
    )
    assert result["final_answer"] == "完成"


@pytest.mark.asyncio
async def test_collection_branches_run_in_parallel_before_join() -> None:
    started = {name: asyncio.Event() for name in ("map", "transport", "hotel")}
    release = asyncio.Event()
    join_count = 0

    def branch(name: str, key: str, value: object) -> Node:
        async def run(_: TripPlanningState) -> dict[str, Any]:
            started[name].set()
            await release.wait()
            return {key: value}

        return run

    base_nodes = _node_set(
        map_node=branch(
            "map",
            "map_weather_evidence",
            MapWeatherEvidenceBundle(
                status="usable",
                map=MapTripEvidence(
                    city="成都",
                    planning_run_id="parallel-run",
                    days=[],
                ),
            ),
        ),
        transport_node=branch(
            "transport",
            "transport_evidence",
            _raw("transport", EvidenceStatus.SKIPPED),
        ),
        hotel_node=branch(
            "hotel",
            "hotel_evidence",
            _raw("hotel", EvidenceStatus.SKIPPED),
        ),
    )
    original_join = base_nodes.join_evidence

    async def counted_join(state: TripPlanningState) -> dict[str, Any]:
        nonlocal join_count
        join_count += 1
        return await original_join(state)

    graph = TripPlannerGraph(replace(base_nodes, join_evidence=counted_join))
    task = asyncio.create_task(graph.ainvoke({"messages": []}))
    await asyncio.wait_for(
        asyncio.gather(*(event.wait() for event in started.values())),
        timeout=1,
    )

    assert join_count == 0
    release.set()
    result = await task

    assert join_count == 1
    assert result["final_answer"] == "完成"


@pytest.mark.asyncio
async def test_cancellation_propagates_to_every_parallel_branch() -> None:
    started = {name: asyncio.Event() for name in ("map", "transport", "hotel")}
    cancelled: set[str] = set()

    def branch(name: str) -> Node:
        async def run(_: TripPlanningState) -> dict[str, Any]:
            started[name].set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.add(name)
                raise
            return {}

        return run

    graph = TripPlannerGraph(
        _node_set(
            map_node=branch("map"),
            transport_node=branch("transport"),
            hotel_node=branch("hotel"),
        )
    )
    task = asyncio.create_task(graph.ainvoke({"messages": []}))
    await asyncio.wait_for(
        asyncio.gather(*(event.wait() for event in started.values())),
        timeout=1,
    )

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancelled == {"map", "transport", "hotel"}


@pytest.mark.asyncio
async def test_validation_failure_does_not_regenerate_model_output() -> None:
    generation_count = 0
    validation_count = 0

    async def generate(_: TripPlanningState) -> dict[str, Any]:
        nonlocal generation_count
        generation_count += 1
        return {"narrative": {"revision": generation_count}}

    async def validate(_: TripPlanningState) -> dict[str, Any]:
        nonlocal validation_count
        validation_count += 1
        return {
            "validation_issues": [
                ValidationIssue(
                    code="DAY_DATE_MISMATCH",
                    path="days.0.date",
                    message="日期不匹配",
                )
            ]
        }

    graph = TripPlannerGraph(
        _node_set(
            generate_node=generate,
            validate_node=validate,
        )
    )

    result = await graph.ainvoke({"messages": []})

    assert generation_count == 1
    assert validation_count == 1
    assert result["controlled_error"] is True
    assert result["current_stage"] == "failed"


@pytest.mark.asyncio
async def test_skeleton_validation_failure_stops_before_model_generation() -> None:
    generation_count = 0

    async def generate(_: TripPlanningState) -> dict[str, Any]:
        nonlocal generation_count
        generation_count += 1
        return {"narrative": {"unexpected": True}}

    graph = TripPlannerGraph(
        _node_set(
            skeleton_node=_node(
                skeleton_validation_issues=[
                    ValidationIssue(
                        code="MAP_REFERENCE_ORDER_MISMATCH",
                        path="days.0.places",
                        message="顺序不匹配",
                    )
                ],
                skeleton_answer="",
            ),
            generate_node=generate,
        )
    )

    result = await graph.ainvoke({"messages": []})

    assert generation_count == 0
    assert result["controlled_error"] is True
