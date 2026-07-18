from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from app.core.settings import Settings
from app.graphs.trip_planner import TripPlanner, _deterministic_itinerary
from app.schemas.chat import ChatMessage
from app.schemas.itinerary import (
    Activity,
    DayPlan,
    ExperienceValidation,
    ExtractedLocation,
    HotelOption,
    ItineraryPlan,
    TransportOption,
    TripRequest,
    TripRequestExtraction,
)
from app.schemas.tool_execution import MessageDeltaEvent, PlanningStageEvent
from app.schemas.travel import HotelSearchInput, PoiSearchInput, TrainSearchInput
from app.services.tool_execution import ToolExecutionContext, ToolExecutor
from app.services.trip_plan_service import StoredTripPlan
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool


class _StructuredRunnable:
    def __init__(self, result: Any) -> None:
        self.result = result

    async def ainvoke(self, _: Any) -> Any:
        return self.result


class FakeStructuredModel:
    def __init__(self, responses: dict[str, list[Any]]) -> None:
        self.responses = defaultdict(list, responses)

    def with_structured_output(self, schema: type[Any]) -> _StructuredRunnable:
        values = self.responses[schema.__name__]
        if not values:
            raise AssertionError(f"No fake response for {schema.__name__}")
        return _StructuredRunnable(values.pop(0))


class FakeJsonFallbackModel:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses

    def with_structured_output(self, _: type[Any]) -> _StructuredRunnable:
        raise NotImplementedError("native structured output is unavailable")

    async def ainvoke(self, _: Any) -> AIMessage:
        return AIMessage(content=self.responses.pop(0))


class FakePlanService:
    def __init__(self) -> None:
        self.drafts: list[TripRequest] = []
        self.partials: list[StoredTripPlan] = []
        self.saved: list[ItineraryPlan] = []

    async def get_current(self, _: uuid.UUID) -> None:
        return None

    async def save_draft(self, _: uuid.UUID, request: TripRequest, *, title: str) -> uuid.UUID:
        assert title
        self.drafts.append(request)
        return uuid.uuid4()

    async def save_partial_plan(
        self,
        _: uuid.UUID,
        request: TripRequest,
        plan: ItineraryPlan,
    ) -> StoredTripPlan:
        stored = StoredTripPlan(
            id=uuid.uuid4(),
            request=request,
            plan=plan,
            status="draft",
            version=0,
        )
        self.partials.append(stored)
        return stored

    async def save_plan(
        self,
        _: uuid.UUID,
        request: TripRequest,
        plan: ItineraryPlan,
        *,
        change_summary: str | None = None,
    ) -> StoredTripPlan:
        del change_summary
        self.saved.append(plan)
        return StoredTripPlan(
            id=uuid.uuid4(),
            request=request,
            plan=plan,
            status="active",
            version=len(self.saved),
        )


def _settings() -> Settings:
    return Settings(
        app_name="test",
        model_config_path=Path("models.yaml"),
        cors_origins=(),
        log_level="WARNING",
    )


def _request() -> TripRequest:
    return TripRequest(
        origin="南京",
        destinations=["杭州"],
        start_date=date(2026, 7, 20),
        end_date=date(2026, 7, 21),
        pace="relaxed",
    )


def _outbound_transport() -> TransportOption:
    return TransportOption(
        transport_type="train",
        departure_city="南京",
        arrival_city="杭州",
        departure_time=datetime(2026, 7, 20, 8),
        arrival_time=datetime(2026, 7, 20, 10),
        train_number="G1",
        price=200,
        source_tool="search_train",
        source_reference="search_train:G1",
    )


def _return_transport() -> TransportOption:
    return TransportOption(
        transport_type="train",
        departure_city="杭州",
        arrival_city="南京",
        departure_time=datetime(2026, 7, 21, 18),
        arrival_time=datetime(2026, 7, 21, 20),
        train_number="G2",
        price=200,
        source_tool="search_train",
        source_reference="search_train:G2",
    )


def _hotel() -> HotelOption:
    return HotelOption(
        name="西湖酒店",
        poi_id="h1",
        nightly_price=500,
        check_in_date=date(2026, 7, 20),
        check_out_date=date(2026, 7, 21),
        source_tool="search_hotel",
        source_reference="search_hotel:h1",
    )


def _verified_tool_executor() -> ToolExecutor:
    async def train(**values: Any) -> dict[str, Any]:
        outbound = values["origin"] == "南京"
        return {
            "success": True,
            "provider": "fake",
            "data": {
                "items": [
                    {
                        "train_number": "G1" if outbound else "G2",
                        "departure_time": "08:00" if outbound else "18:00",
                        "arrival_time": "10:00" if outbound else "20:00",
                        "price": 200,
                    }
                ]
            },
        }

    async def hotel(**_: Any) -> dict[str, Any]:
        return {
            "success": True,
            "provider": "fake",
            "data": {"items": [{"id": "h1", "name": "西湖酒店", "price": 500}]},
        }

    return ToolExecutor(
        [
            StructuredTool.from_function(
                coroutine=train,
                name="search_train",
                description="train",
                args_schema=TrainSearchInput,
            ),
            StructuredTool.from_function(
                coroutine=hotel,
                name="search_hotel",
                description="hotel",
                args_schema=HotelSearchInput,
            ),
        ]
    )


def _plan(*, duplicate: bool = False) -> ItineraryPlan:
    return ItineraryPlan(
        title="杭州两日游",
        origin="南京",
        destination="杭州",
        start_date=date(2026, 7, 20),
        end_date=date(2026, 7, 21),
        outbound_transport=_outbound_transport(),
        return_transport=_return_transport(),
        hotel=_hotel(),
        days=[
            DayPlan(
                date=date(2026, 7, 20),
                day_index=1,
                activities=[Activity(place_name="西湖", activity_type="游览")] if duplicate else [],
            ),
            DayPlan(
                date=date(2026, 7, 21),
                day_index=2,
                activities=[Activity(place_name="西湖", activity_type="游览")] if duplicate else [],
            ),
        ],
    )


@pytest.mark.asyncio
async def test_missing_fields_are_saved_as_draft_without_tool_calls() -> None:
    service = FakePlanService()
    model = FakeStructuredModel(
        {
            "TripRequestExtraction": [
                TripRequestExtraction(request=TripRequest(destinations=["杭州"]))
            ]
        }
    )
    planner = TripPlanner(ToolExecutor([]), service, _settings())

    events = [
        event
        async for event in planner.stream(
            model,  # type: ignore[arg-type]
            [ChatMessage(role="user", content="帮我规划杭州行程")],
            "new_trip_plan",
            execution_context=ToolExecutionContext(uuid.uuid4(), uuid.uuid4()),
            stored=None,
        )
    ]

    text = "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
    assert "哪天出发" in text
    assert service.drafts[0].destinations == ["杭州"]
    assert not service.saved


@pytest.mark.asyncio
async def test_destination_only_fallback_never_invents_origin_or_calls_transport() -> None:
    transport_calls = 0

    async def forbidden_train_query(**_: Any) -> dict[str, Any]:
        nonlocal transport_calls
        transport_calls += 1
        raise AssertionError("transport must not run before origin is confirmed")

    train_tool = StructuredTool.from_function(
        coroutine=forbidden_train_query,
        name="search_train",
        description="must not be called",
        args_schema=TrainSearchInput,
    )
    service = FakePlanService()
    planner = TripPlanner(ToolExecutor([train_tool]), service, _settings())

    events = [
        event
        async for event in planner.stream(
            FakeStructuredModel({}),  # type: ignore[arg-type]
            [
                ChatMessage(
                    role="user",
                    content="规划一份去杭州的旅游攻略，2026-07-20 到 2026-07-22。",
                )
            ],
            "new_trip_plan",
            execution_context=ToolExecutionContext(uuid.uuid4(), uuid.uuid4()),
            stored=None,
        )
    ]

    text = "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
    assert "从哪个城市出发" in text
    assert transport_calls == 0
    assert service.drafts[0].origin is None
    assert service.drafts[0].destinations == ["杭州"]
    assert not service.saved


@pytest.mark.asyncio
async def test_unanchored_llm_origin_is_discarded_before_tool_collection() -> None:
    service = FakePlanService()
    extraction = TripRequestExtraction(
        request=TripRequest(
            origin="规划一份",
            destinations=["杭州"],
            start_date=date(2026, 7, 20),
            end_date=date(2026, 7, 22),
        )
    )
    planner = TripPlanner(
        ToolExecutor([]),
        service,
        _settings(),
    )

    events = [
        event
        async for event in planner.stream(
            FakeStructuredModel({"TripRequestExtraction": [extraction]}),  # type: ignore[arg-type]
            [ChatMessage(role="user", content="规划一份去杭州的旅游攻略")],
            "new_trip_plan",
            execution_context=ToolExecutionContext(uuid.uuid4(), uuid.uuid4()),
            stored=None,
        )
    ]

    text = "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
    assert "从哪个城市出发" in text
    assert service.drafts[0].origin is None


@pytest.mark.asyncio
async def test_invalid_origin_in_saved_draft_is_removed_on_follow_up() -> None:
    service = FakePlanService()
    stored = StoredTripPlan(
        id=uuid.uuid4(),
        request=TripRequest(origin="规划一份", destinations=["杭州"]),
        plan=None,
        status="draft",
        version=0,
    )
    extraction = TripRequestExtraction(
        request=TripRequest(
            start_date=date(2026, 7, 20),
            end_date=date(2026, 7, 22),
        )
    )
    planner = TripPlanner(ToolExecutor([]), service, _settings())

    events = [
        event
        async for event in planner.stream(
            FakeStructuredModel({"TripRequestExtraction": [extraction]}),  # type: ignore[arg-type]
            [ChatMessage(role="user", content="2026-07-20 出发，玩三天")],
            "new_trip_plan",
            execution_context=ToolExecutionContext(uuid.uuid4(), uuid.uuid4()),
            stored=stored,
        )
    ]

    text = "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
    assert "从哪个城市出发" in text
    assert service.drafts[0].origin is None
    assert service.drafts[0].destinations == ["杭州"]


@pytest.mark.asyncio
async def test_bare_city_follow_up_fills_the_missing_origin() -> None:
    service = FakePlanService()
    stored = StoredTripPlan(
        id=uuid.uuid4(),
        request=TripRequest(destinations=["杭州"]),
        plan=None,
        status="draft",
        version=0,
    )
    planner = TripPlanner(ToolExecutor([]), service, _settings())

    events = [
        event
        async for event in planner.stream(
            FakeStructuredModel({}),  # type: ignore[arg-type]
            [ChatMessage(role="user", content="南京")],
            "new_trip_plan",
            execution_context=ToolExecutionContext(uuid.uuid4(), uuid.uuid4()),
            stored=stored,
        )
    ]

    text = "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
    assert "哪天出发" in text
    assert service.drafts[0].origin == "南京"
    assert service.drafts[0].destinations == ["杭州"]


@pytest.mark.asyncio
async def test_bare_city_follow_up_fills_the_missing_destination() -> None:
    service = FakePlanService()
    stored = StoredTripPlan(
        id=uuid.uuid4(),
        request=TripRequest(origin="南京"),
        plan=None,
        status="draft",
        version=0,
    )
    planner = TripPlanner(ToolExecutor([]), service, _settings())

    events = [
        event
        async for event in planner.stream(
            FakeStructuredModel({}),  # type: ignore[arg-type]
            [ChatMessage(role="user", content="杭州")],
            "new_trip_plan",
            execution_context=ToolExecutionContext(uuid.uuid4(), uuid.uuid4()),
            stored=stored,
        )
    ]

    text = "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
    assert "哪天出发" in text
    assert service.drafts[0].origin == "南京"
    assert service.drafts[0].destinations == ["杭州"]


@pytest.mark.asyncio
async def test_request_json_fallback_preserves_llm_location_evidence() -> None:
    service = FakePlanService()
    extraction = TripRequestExtraction(
        request=_request(),
        origin_location=ExtractedLocation(
            value="南京",
            evidence="我现在人在南京",
            explicit=True,
        ),
        destination_locations=[
            ExtractedLocation(value="杭州", evidence="去杭州", explicit=True)
        ],
    )
    model = FakeJsonFallbackModel([f"```json\n{extraction.model_dump_json()}\n```"])
    planner = TripPlanner(ToolExecutor([]), service, _settings())

    events = [
        event
        async for event in planner.stream(
            model,  # type: ignore[arg-type]
            [
                ChatMessage(
                    role="user",
                    content=(
                        "我现在人在南京，准备去杭州，2026-07-20 到 2026-07-21，"
                        "请安排两日行程。"
                    ),
                )
            ],
            "new_trip_plan",
            execution_context=ToolExecutionContext(uuid.uuid4(), uuid.uuid4()),
            stored=None,
        )
    ]

    text = "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
    assert "# 杭州2日行程" in text
    assert service.partials
    assert not service.saved
    assert service.partials[0].request.origin == "南京"
    assert service.partials[0].plan is not None
    assert "未完成草案" in text


@pytest.mark.asyncio
async def test_graph_generates_validates_persists_and_streams_stage_events() -> None:
    service = FakePlanService()
    model = FakeStructuredModel(
        {
            "TripRequestExtraction": [TripRequestExtraction(request=_request())],
            "ItineraryPlan": [_plan()],
            "ExperienceValidation": [ExperienceValidation()],
        }
    )
    planner = TripPlanner(_verified_tool_executor(), service, _settings())

    events = [
        event
        async for event in planner.stream(
            model,  # type: ignore[arg-type]
            [ChatMessage(role="user", content="帮我规划南京到杭州两日游")],
            "new_trip_plan",
            execution_context=ToolExecutionContext(uuid.uuid4(), uuid.uuid4()),
            stored=None,
        )
    ]

    stages = [event.stage for event in events if isinstance(event, PlanningStageEvent)]
    text = "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
    assert "understanding_request" in stages
    assert "generating_itinerary" in stages
    assert "saving_itinerary" in stages
    assert "# 杭州两日游" in text
    assert len(service.saved) == 1
    transport_diagnostic = next(
        item
        for item in service.saved[0].diagnostics
        if item.stage == "collecting_transport"
    )
    assert transport_diagnostic.details["usable_items"] == 2
    assert transport_diagnostic.details["covered_items"] == 2


@pytest.mark.asyncio
async def test_graph_uses_deterministic_fallback_when_structured_model_is_incompatible() -> None:
    service = FakePlanService()
    planner = TripPlanner(ToolExecutor([]), service, _settings())

    events = [
        event
        async for event in planner.stream(
            FakeStructuredModel({}),  # type: ignore[arg-type]
            [
                ChatMessage(
                    role="user",
                    content=(
                        "帮我规划 2026-07-20 到 2026-07-22，从南京去杭州，"
                        "两个人预算 4000 元，喜欢自然风景和人文景点，行程轻松一点。"
                    ),
                )
            ],
            "new_trip_plan",
            execution_context=ToolExecutionContext(uuid.uuid4(), uuid.uuid4()),
            stored=None,
        )
    ]

    text = "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
    assert "# 杭州3日行程" in text
    assert service.partials
    assert not service.saved
    assert service.partials[0].request.origin == "南京"
    assert service.partials[0].request.destinations == ["杭州"]
    assert service.partials[0].plan is not None
    assert "结构化体验编排不可用" in text


@pytest.mark.asyncio
async def test_graph_revises_invalid_plan_once_then_stops_loop() -> None:
    service = FakePlanService()
    model = FakeStructuredModel(
        {
            "TripRequestExtraction": [TripRequestExtraction(request=_request())],
            "ItineraryPlan": [_plan(duplicate=True), _plan()],
            "ExperienceValidation": [ExperienceValidation(), ExperienceValidation()],
        }
    )
    planner = TripPlanner(_verified_tool_executor(), service, _settings())

    events = [
        event
        async for event in planner.stream(
            model,  # type: ignore[arg-type]
            [ChatMessage(role="user", content="帮我规划南京到杭州两日游")],
            "new_trip_plan",
            execution_context=ToolExecutionContext(uuid.uuid4(), uuid.uuid4()),
            stored=None,
        )
    ]

    revision_success = [
        event
        for event in events
        if isinstance(event, PlanningStageEvent)
        and event.stage == "revising_itinerary"
        and event.status == "success"
    ]
    assert len(revision_success) == 1
    assert len(service.saved) == 1


@pytest.mark.asyncio
async def test_graph_keeps_invalid_plan_as_draft_after_revision_limit() -> None:
    service = FakePlanService()
    model = FakeStructuredModel(
        {
            "TripRequestExtraction": [TripRequestExtraction(request=_request())],
            "ItineraryPlan": [_plan(duplicate=True) for _ in range(3)],
            "ExperienceValidation": [ExperienceValidation() for _ in range(3)],
        }
    )
    planner = TripPlanner(_verified_tool_executor(), service, _settings())

    events = [
        event
        async for event in planner.stream(
            model,  # type: ignore[arg-type]
            [ChatMessage(role="user", content="帮我规划南京到杭州两日游")],
            "new_trip_plan",
            execution_context=ToolExecutionContext(uuid.uuid4(), uuid.uuid4()),
            stored=None,
        )
    ]

    revision_success = [
        event
        for event in events
        if isinstance(event, PlanningStageEvent)
        and event.stage == "revising_itinerary"
        and event.status == "success"
    ]
    assert len(revision_success) == 2
    assert service.partials
    assert not service.saved
    assert service.partials[0].plan is not None
    text = "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
    assert "自动修订达到上限" in text


@pytest.mark.asyncio
async def test_local_pace_revision_does_not_requery_unaffected_tools() -> None:
    tool_calls = 0

    async def forbidden_poi_query(**_: Any) -> dict[str, Any]:
        nonlocal tool_calls
        tool_calls += 1
        raise AssertionError("local removal must not query POIs")

    poi_tool = StructuredTool.from_function(
        coroutine=forbidden_poi_query,
        name="search_poi",
        description="must not be called",
        args_schema=PoiSearchInput,
    )
    previous = _plan()
    previous.days[1].activities = [
        Activity(place_name="西湖", poi_id="p1", activity_type="游览"),
        Activity(place_name="灵隐寺", poi_id="p2", activity_type="参观"),
    ]
    modified = previous.model_copy(deep=True)
    modified.days[1].activities.pop()
    stored = StoredTripPlan(
        id=uuid.uuid4(),
        request=_request(),
        plan=previous,
        status="active",
        version=1,
    )
    service = FakePlanService()
    model = FakeStructuredModel(
        {
            "TripRequestExtraction": [
                TripRequestExtraction(
                    request=_request(),
                    is_plan_revision=True,
                    revision_instructions="第二天减少一个景点",
                    affected_sections=["activities"],
                    change_summary="第二天减少一个景点",
                )
            ],
            "ItineraryPlan": [modified],
            "ExperienceValidation": [ExperienceValidation()],
        }
    )
    planner = TripPlanner(ToolExecutor([poi_tool]), service, _settings())

    events = [
        event
        async for event in planner.stream(
            model,  # type: ignore[arg-type]
            [ChatMessage(role="user", content="第二天太满了，减少一个景点")],
            "modify_trip_plan",
            execution_context=ToolExecutionContext(uuid.uuid4(), uuid.uuid4()),
            stored=stored,
        )
    ]

    assert tool_calls == 0
    assert any(
        isinstance(event, PlanningStageEvent)
        and event.stage == "collecting_pois"
        and event.status == "skipped"
        for event in events
    )
    assert len(service.saved) == 1


def test_deterministic_fallback_clusters_pois_uses_verified_routes_and_scores_hotel() -> None:
    request = TripRequest(
        destinations=["杭州"],
        start_date=date(2026, 7, 20),
        end_date=date(2026, 7, 21),
        pace="relaxed",
    )
    poi_results = [
        {
            "poi_id": "far-1",
            "name": "远郊一",
            "provider_rank": 0,
            "location": {"longitude": 119.0, "latitude": 30.0},
        },
        {
            "poi_id": "far-2",
            "name": "远郊二",
            "provider_rank": 1,
            "location": {"longitude": 121.2, "latitude": 31.0},
        },
        *[
            {
                "poi_id": f"central-{index}",
                "name": f"城市地标 {index}",
                "poi_type": "历史文化景点",
                "provider_rank": index + 2,
                "location": {
                    "longitude": 120.15 + index / 100,
                    "latitude": 30.25,
                },
            }
            for index in range(3)
        ],
    ]
    hotels = [
        HotelOption(
            name="远郊低价酒店",
            coordinates='{"longitude": 119.0, "latitude": 30.0}',
            nightly_price=100,
            check_in_date=date(2026, 7, 20),
            check_out_date=date(2026, 7, 21),
            source_tool="search_hotel",
        ),
        HotelOption(
            name="中心酒店",
            coordinates='{"longitude": 120.16, "latitude": 30.25}',
            nightly_price=300,
            check_in_date=date(2026, 7, 20),
            check_out_date=date(2026, 7, 21),
            source_tool="search_hotel",
        ),
    ]
    state: dict[str, Any] = {
        "poi_results": poi_results,
        "hotel_results": hotels,
        "route_results": [
            {
                "route_legs": [
                    {
                        "origin_id": "central-0",
                        "destination_id": "central-1",
                        "duration_minutes": 12,
                    }
                ]
            }
        ],
        "collection_diagnostics": {},
    }

    plan = _deterministic_itinerary(state, request, max_daily_activities=5)  # type: ignore[arg-type]

    assert [item.place_name for item in plan.days[0].activities] == [
        "城市地标 0",
        "城市地标 1",
    ]
    assert plan.days[0].estimated_transport_time_minutes == 12
    assert plan.days[1].estimated_transport_time_minutes == 0
    assert plan.hotel is not None and plan.hotel.name == "中心酒店"

    one_day_request = request.model_copy(
        update={"end_date": request.start_date, "duration_days": 1, "pace": "moderate"}
    )
    incomplete_routes = _deterministic_itinerary(
        state,
        one_day_request,
        max_daily_activities=5,
    )  # type: ignore[arg-type]
    assert len(incomplete_routes.days[0].activities) == 3
    assert incomplete_routes.days[0].estimated_transport_time_minutes is None
