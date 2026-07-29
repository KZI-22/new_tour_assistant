from __future__ import annotations

from datetime import timedelta

from app.schemas.map_planning import (
    MapDayEvidence,
    MapPlaceEvidence,
    RouteLegEvidence,
)
from app.schemas.trip_evidence import (
    EvidenceStatus,
    JoinedTripEvidence,
    RawCapabilityEvidence,
)
from app.schemas.trip_planning import DailyWeatherEvidence
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
from app.services.weather_advice_service import build_weather_advice

_WEEKDAYS = "一二三四五六日"
_DETAIL_LINK_MARKER = "｜[查看详情]"


def build_trip_presentation_context(
    evidence: JoinedTripEvidence,
) -> TripPresentationContext:
    core = evidence.request.core
    map_evidence = evidence.map_weather.map
    weather = evidence.map_weather.weather
    weather_by_date = {day.date: day for day in weather.days} if weather is not None else {}
    duration_days = core.duration_days
    end_date = (
        core.start_date + timedelta(days=duration_days - 1)
        if core.start_date is not None and duration_days is not None
        else None
    )

    return TripPresentationContext(
        trip=TripSummaryContext(
            origin_city=evidence.capabilities.transport.origin,
            destination_city=core.destination_city,
            start_date=core.start_date,
            end_date=end_date,
            duration_days=duration_days,
            interests=[item.value for item in core.interests],
            food_preferences=list(core.food_preferences),
            assumptions=[
                f"{item.explanation}：{item.value}"
                for item in evidence.capabilities.derivations
                if item.source in {"derived_from_trip_dates", "default_policy"}
            ],
        ),
        transport=_transport_context(evidence),
        hotel=_hotel_context(evidence),
        days=[
            _day_context(day, weather_by_date.get(day.date))
            for day in (map_evidence.days if map_evidence is not None else [])
        ],
        warnings=list(evidence.warnings),
    )


def _transport_context(
    evidence: JoinedTripEvidence,
) -> TransportPresentationContext:
    plan = evidence.capabilities.transport
    raw = evidence.transport
    return TransportPresentationContext(
        enabled=plan.enabled,
        status=raw.status.value,
        modes=[mode.value for mode in plan.modes],
        journey_scope=plan.journey_scope.value,
        origin=plan.origin,
        destination=plan.destination,
        outbound_date=plan.outbound_date,
        return_date=plan.return_date,
        options=_readable_options(plan.enabled, raw),
        warnings=list(raw.warnings),
        error_code=raw.error_code,
    )


def _hotel_context(
    evidence: JoinedTripEvidence,
) -> HotelPresentationContext:
    plan = evidence.capabilities.hotel
    raw = evidence.hotel
    return HotelPresentationContext(
        enabled=plan.enabled,
        status=raw.status.value,
        destination=plan.destination,
        check_in_date=plan.check_in_date,
        check_out_date=plan.check_out_date,
        nearby_poi=plan.nearby_poi,
        options=_readable_options(plan.enabled, raw),
        warnings=list(raw.warnings),
        error_code=raw.error_code,
    )


def _day_context(
    day: MapDayEvidence,
    weather: DailyWeatherEvidence | None,
) -> DayPresentationContext:
    places = day.ordered_places()
    places_by_ref = {place.reference_id: place for place in places}
    return DayPresentationContext(
        day_index=day.day_index,
        date=day.date,
        weekday=f"周{_WEEKDAYS[day.date.weekday()]}",
        weather=_weather_context(weather),
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
            for place in places
        ],
        route_legs=[
            _route_leg_context(leg, places_by_ref)
            for leg in day.route_legs
            if leg.origin_ref in places_by_ref and leg.destination_ref in places_by_ref
        ],
        warnings=list(day.warnings),
    )


def _weather_context(
    weather: DailyWeatherEvidence | None,
) -> WeatherPresentationContext:
    if weather is None:
        return WeatherPresentationContext(
            coverage="unavailable",
            advice=["该日期暂无对应天气预报，出发前请再次确认。"],
        )
    return WeatherPresentationContext(
        coverage=weather.coverage,
        day_weather=weather.day_weather,
        night_weather=weather.night_weather,
        day_temperature=weather.day_temperature,
        night_temperature=weather.night_temperature,
        advice=build_weather_advice(weather),
    )


def _route_leg_context(
    leg: RouteLegEvidence,
    places_by_ref: dict[str, MapPlaceEvidence],
) -> RouteLegPresentationContext:
    origin = places_by_ref[leg.origin_ref]
    destination = places_by_ref[leg.destination_ref]
    return RouteLegPresentationContext(
        origin_ref=leg.origin_ref,
        origin_name=origin.name,
        destination_ref=leg.destination_ref,
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
    evidence: RawCapabilityEvidence,
) -> list[str]:
    if not enabled or evidence.status is not EvidenceStatus.USABLE:
        return []
    return [item.partition(_DETAIL_LINK_MARKER)[0].strip() for item in evidence.display_options]


__all__ = ["build_trip_presentation_context"]
