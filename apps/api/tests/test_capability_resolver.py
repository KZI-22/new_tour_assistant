from __future__ import annotations

from datetime import date

from app.schemas.chat import ChatMessage
from app.schemas.trip_capabilities import (
    CapabilityAction,
    HotelIntent,
    JourneyScope,
    TransportIntent,
    TransportMode,
    TripPlanningRequest,
)
from app.schemas.trip_planning import CityTripRequest
from app.services.capability_resolver import (
    check_requirements,
    render_requirement_clarification,
    resolve_capabilities,
)
from app.services.trip_requirement_extractor import (
    apply_trip_request_overrides,
    trip_request_extraction_prompt,
)

START_DATE = date(2026, 8, 1)


def _messages(*contents: str) -> list[ChatMessage]:
    return [ChatMessage(role="user", content=content) for content in contents if content]


def _request(
    *,
    text: str = "",
    transport: TransportIntent | None = None,
    hotel: HotelIntent | None = None,
    city: str | None = "成都",
    start_date: date | None = START_DATE,
    duration_days: int | None = 3,
) -> tuple[TripPlanningRequest, list[ChatMessage]]:
    return (
        TripPlanningRequest(
            core=CityTripRequest(
                destination_city=city,
                start_date=start_date,
                duration_days=duration_days,
            ),
            transport=transport or TransportIntent(),
            hotel=hotel or HotelIntent(),
        ),
        _messages(text),
    )


def test_plain_three_day_trip_only_enables_map_weather() -> None:
    request, messages = _request(text="帮我安排成都三日游")

    plan = resolve_capabilities(request, messages)

    assert plan.map_weather_enabled is True
    assert plan.transport.enabled is False
    assert plan.hotel.enabled is False


def test_explicit_flight_query_enables_only_flight() -> None:
    request, messages = _request(
        text="顺便查机票",
        transport=TransportIntent(origin_city="北京"),
    )

    plan = resolve_capabilities(request, messages)

    assert plan.transport.enabled is True
    assert plan.transport.modes == [TransportMode.FLIGHT]


def test_comparing_plane_and_high_speed_rail_enables_both() -> None:
    request, messages = _request(
        text="比较飞机和高铁",
        transport=TransportIntent(origin_city="北京"),
    )

    plan = resolve_capabilities(request, messages)

    assert plan.transport.modes == [TransportMode.FLIGHT, TransportMode.TRAIN]


def test_transport_mode_negation_is_scoped_before_positive_train_and_hotel_query() -> None:
    request, messages = _request(
        text="不要飞机，帮我查北京到西安的高铁和酒店",
        transport=TransportIntent(origin_city="北京"),
    )

    plan = resolve_capabilities(request, messages)

    assert plan.transport.enabled is True
    assert plan.transport.modes == [TransportMode.TRAIN]
    assert plan.hotel.enabled is True


def test_existing_hotel_clause_does_not_disable_separate_flight_query() -> None:
    request, messages = _request(
        text="酒店已经订好了，帮我查机票",
        transport=TransportIntent(origin_city="北京"),
    )

    plan = resolve_capabilities(request, messages)

    assert plan.hotel.enabled is False
    assert plan.transport.enabled is True
    assert plan.transport.modes == [TransportMode.FLIGHT]


def test_explicit_hotel_recommendation_enables_hotel() -> None:
    request, messages = _request(text="酒店推荐几个")

    plan = resolve_capabilities(request, messages)

    assert plan.hotel.enabled is True
    assert plan.hotel.check_in_date == START_DATE
    assert plan.hotel.check_out_date == date(2026, 8, 4)


def test_hotel_location_background_does_not_enable_search() -> None:
    request, messages = _request(text="我住春熙路")

    assert resolve_capabilities(request, messages).hotel.enabled is False


def test_existing_hotel_explicitly_disables_search() -> None:
    request, messages = _request(
        text="酒店已经订好了",
        hotel=HotelIntent(
            action=CapabilityAction.ENABLE,
            evidence_text="酒店已经订好了",
        ),
    )

    assert resolve_capabilities(request, messages).hotel.enabled is False


def test_latest_hotel_disable_overrides_previous_enable() -> None:
    request, _ = _request()
    messages = _messages("请推荐几家酒店", "酒店不用查了")

    plan = resolve_capabilities(request, messages)

    assert plan.hotel.enabled is False
    assert plan.hotel.reason == "用户明确关闭酒店查询"


def test_unspecified_latest_turn_inherits_recent_explicit_request() -> None:
    request, _ = _request(
        hotel=HotelIntent(),
    )
    messages = _messages("请推荐几家酒店", "把第二天改成轻松一点")

    plan = resolve_capabilities(request, messages)

    assert plan.hotel.enabled is True
    assert any(
        item.field == "hotel.enabled" and item.source == "conversation_context"
        for item in plan.derivations
    )


def test_model_enable_without_latest_evidence_is_rejected() -> None:
    request, messages = _request(
        text="帮我安排成都三日游",
        transport=TransportIntent(
            action=CapabilityAction.ENABLE,
            modes=[TransportMode.FLIGHT],
            origin_city="北京",
            evidence_text="查机票",
        ),
    )

    assert resolve_capabilities(request, messages).transport.enabled is False


def test_enabled_transport_without_origin_is_asked_once_with_core_fields() -> None:
    request, messages = _request(
        text="比较飞机和高铁",
        city=None,
        start_date=None,
    )
    plan = resolve_capabilities(request, messages)

    check = check_requirements(request, plan, maximum_days=14)
    question = render_requirement_clarification(check)

    assert check.complete is False
    assert [item.field for item in check.missing] == [
        "core.destination_city",
        "core.start_date",
        "transport.origin",
    ]
    assert question == "请一次补充：目标城市、出行开始日期、交通出发城市。"


def test_explicit_one_way_does_not_derive_return_query() -> None:
    request, messages = _request(
        text="查一下单程机票",
        transport=TransportIntent(origin_city="北京"),
    )

    plan = resolve_capabilities(request, messages)

    assert plan.transport.journey_scope is JourneyScope.ONE_WAY
    assert plan.transport.outbound_date == START_DATE
    assert plan.transport.return_date is None


def test_unspecified_journey_scope_defaults_to_round_trip_and_last_day() -> None:
    request, messages = _request(
        text="查一下机票",
        transport=TransportIntent(origin_city="北京"),
    )

    plan = resolve_capabilities(request, messages)

    assert plan.transport.journey_scope is JourneyScope.ROUND_TRIP
    assert plan.transport.return_date == date(2026, 8, 3)
    assert any(
        item.field == "transport.journey_scope" and item.source == "default_policy"
        for item in plan.derivations
    )


def test_requirement_check_rejects_inverted_dates() -> None:
    request, messages = _request(
        text="查往返机票并推荐酒店",
        transport=TransportIntent(
            origin_city="北京",
            outbound_date=date(2026, 8, 3),
            return_date=date(2026, 8, 2),
        ),
        hotel=HotelIntent(
            check_in_date=date(2026, 8, 3),
            check_out_date=date(2026, 8, 3),
        ),
    )
    plan = resolve_capabilities(request, messages)

    check = check_requirements(request, plan, maximum_days=14)

    assert check.errors == [
        "返程日期不能早于去程日期。",
        "酒店入住日期必须早于退房日期。",
    ]


def test_extraction_prompt_and_core_overrides_keep_optional_intents() -> None:
    request, messages = _request(
        text="改成 2026-08-05 开始玩 4 天，酒店继续推荐",
        hotel=HotelIntent(
            action=CapabilityAction.ENABLE,
            evidence_text="酒店继续推荐",
        ),
    )

    updated, overrides = apply_trip_request_overrides(request, messages)
    prompt = trip_request_extraction_prompt(messages)

    assert updated.core.start_date == date(2026, 8, 5)
    assert updated.core.duration_days == 4
    assert updated.hotel.action is CapabilityAction.ENABLE
    assert overrides == {
        "explicit_duration_override": True,
        "explicit_start_date_override": True,
    }
    assert "evidence_text" in prompt
    assert "仅说明出发地" in prompt
