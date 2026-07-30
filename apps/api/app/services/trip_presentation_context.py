from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from app.schemas.trip_evidence import EvidenceStatus
from app.schemas.trip_plan_snapshot import (
    TripHotelSnapshot,
    TripPlanDaySnapshot,
    TripPlanPlaceSnapshot,
    TripPlanRouteLegSnapshot,
    TripPlanSnapshot,
    TripPlanWeatherSnapshot,
    TripTransportSnapshot,
)
from app.schemas.trip_presentation import (
    DayPresentationContext,
    HotelPresentationContext,
    PlacePresentationContext,
    RouteLegPresentationContext,
    TransportPresentationContext,
    TripPresentationContext,
    TripSummaryContext,
    WeatherPresentationContext,
)

_WEEKDAYS = "一二三四五六日"
_DETAIL_LINK_MARKER = "｜[查看详情]"


def build_trip_presentation_context(
    snapshot: TripPlanSnapshot,
) -> TripPresentationContext:
    core = snapshot.request.core
    duration_days = core.duration_days
    end_date = (
        core.start_date + timedelta(days=duration_days - 1)
        if core.start_date is not None and duration_days is not None
        else None
    )

    return TripPresentationContext(
        trip=TripSummaryContext(
            origin_city=snapshot.capabilities.transport.origin,
            destination_city=core.destination_city,
            start_date=core.start_date,
            end_date=end_date,
            duration_days=duration_days,
            interests=[item.value for item in core.interests],
            food_preferences=list(core.food_preferences),
            assumptions=[
                f"{item.explanation}：{item.value}"
                for item in snapshot.capabilities.derivations
                if item.source in {"derived_from_trip_dates", "default_policy"}
            ],
        ),
        transport=_transport_context(snapshot.transport),
        hotel=_hotel_context(snapshot.hotel),
        days=[_day_context(day) for day in snapshot.days],
        warnings=list(snapshot.warnings),
    )


def _transport_context(
    transport: TripTransportSnapshot,
) -> TransportPresentationContext:
    return TransportPresentationContext(
        enabled=transport.enabled,
        status=transport.status.value,
        modes=[mode.value for mode in transport.modes],
        journey_scope=transport.journey_scope.value,
        origin=transport.origin,
        destination=transport.destination,
        outbound_date=transport.outbound_date,
        return_date=transport.return_date,
        options=_readable_options(
            transport.enabled,
            transport.status,
            transport.display_options,
        ),
        warnings=list(transport.warnings),
        error_code=transport.error_code,
    )


def _hotel_context(
    hotel: TripHotelSnapshot,
) -> HotelPresentationContext:
    return HotelPresentationContext(
        enabled=hotel.enabled,
        status=hotel.status.value,
        destination=hotel.destination,
        check_in_date=hotel.check_in_date,
        check_out_date=hotel.check_out_date,
        nearby_poi=hotel.nearby_poi,
        options=_readable_options(
            hotel.enabled,
            hotel.status,
            hotel.display_options,
        ),
        warnings=list(hotel.warnings),
        error_code=hotel.error_code,
    )


def _day_context(
    day: TripPlanDaySnapshot,
) -> DayPresentationContext:
    places_by_id = {place.plan_item_id: place for place in day.places}
    return DayPresentationContext(
        day_index=day.day_index,
        date=day.date,
        weekday=f"周{_WEEKDAYS[day.date.weekday()]}",
        weather=_weather_context(day.weather),
        estimated_visit_minutes=day.estimated_visit_minutes,
        estimated_transport_minutes=day.estimated_transport_minutes,
        places=[
            PlacePresentationContext(
                reference_id=place.reference_id,
                name=place.name,
                address=place.address,
                poi_type=place.poi_type,
                estimated_visit_minutes=place.estimated_visit_minutes,
                matched_preferences=list(place.matched_preferences),
                selection_reasons=list(place.selection_reasons),
            )
            for place in day.places
        ],
        route_legs=[
            _route_leg_context(leg, places_by_id)
            for leg in day.route_legs
            if leg.origin_plan_item_id in places_by_id
            and leg.destination_plan_item_id in places_by_id
        ],
        warnings=list(day.warnings),
    )


def _weather_context(
    weather: TripPlanWeatherSnapshot,
) -> WeatherPresentationContext:
    return WeatherPresentationContext(
        coverage=weather.coverage,
        day_weather=weather.day_weather,
        night_weather=weather.night_weather,
        day_temperature=weather.day_temperature,
        night_temperature=weather.night_temperature,
        advice=list(weather.advice),
    )


def _route_leg_context(
    leg: TripPlanRouteLegSnapshot,
    places_by_id: dict[UUID, TripPlanPlaceSnapshot],
) -> RouteLegPresentationContext:
    origin = places_by_id[leg.origin_plan_item_id]
    destination = places_by_id[leg.destination_plan_item_id]
    return RouteLegPresentationContext(
        origin_ref=origin.reference_id,
        origin_name=origin.name,
        destination_ref=destination.reference_id,
        destination_name=destination.name,
        mode=leg.mode,
        distance_meters=leg.distance_meters,
        duration_minutes=(
            max(1, round(leg.duration_seconds / 60)) if leg.duration_seconds is not None else None
        ),
        transfer_count=leg.transfer_count,
        is_fallback=leg.is_fallback,
    )


def _readable_options(
    enabled: bool,
    status: EvidenceStatus,
    display_options: list[str],
) -> list[str]:
    if not enabled or status is not EvidenceStatus.USABLE:
        return []
    return [item.partition(_DETAIL_LINK_MARKER)[0].strip() for item in display_options]


__all__ = ["build_trip_presentation_context"]
