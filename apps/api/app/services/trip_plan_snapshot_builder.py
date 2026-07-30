from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from app.schemas.map_planning import MapDayEvidence, MapPlaceEvidence
from app.schemas.trip_evidence import JoinedTripEvidence
from app.schemas.trip_options import HotelOptionSnapshot, TransportOptionSnapshot
from app.schemas.trip_plan_snapshot import (
    TripHotelSnapshot,
    TripPlanDaySnapshot,
    TripPlanPlaceSnapshot,
    TripPlanRouteLegSnapshot,
    TripPlanSnapshot,
    TripPlanSourceMetadata,
    TripPlanWeatherSnapshot,
    TripTransportSnapshot,
)
from app.schemas.trip_planning import DailyWeatherEvidence
from app.services.weather_advice_service import build_weather_advice

_UNAVAILABLE_WEATHER_ADVICE = "该日期暂无对应天气预报，出发前请再次确认。"


class TripPlanSnapshotBuilder:
    def build(
        self,
        evidence: JoinedTripEvidence,
        *,
        request_field_sources: Mapping[str, object] | None = None,
        planning_run_id: str | None = None,
    ) -> TripPlanSnapshot:
        map_evidence = evidence.map_weather.map
        weather_evidence = evidence.map_weather.weather
        effective_run_id = planning_run_id or (
            map_evidence.planning_run_id if map_evidence is not None else "unavailable"
        )
        weather_by_date = (
            {day.date: day for day in weather_evidence.days}
            if weather_evidence is not None
            else {}
        )
        days = [
            self._build_day(
                day,
                weather_by_date.get(day.date),
                planning_run_id=effective_run_id,
                weather_queried_at=(
                    weather_evidence.queried_at if weather_evidence is not None else None
                ),
            )
            for day in (map_evidence.days if map_evidence is not None else [])
        ]
        transport_plan = evidence.capabilities.transport
        hotel_plan = evidence.capabilities.hotel
        transport_options = [
            item
            for item in evidence.transport.normalized_options
            if isinstance(item, TransportOptionSnapshot)
        ]
        hotel_options = [
            item
            for item in evidence.hotel.normalized_options
            if isinstance(item, HotelOptionSnapshot)
        ]
        return TripPlanSnapshot(
            request=evidence.request,
            request_field_sources=_normalize_field_sources(request_field_sources),
            capabilities=evidence.capabilities,
            days=days,
            transport=TripTransportSnapshot(
                enabled=transport_plan.enabled,
                status=evidence.transport.status,
                query=dict(evidence.transport.query),
                queried_at=evidence.transport.queried_at,
                modes=list(transport_plan.modes),
                journey_scope=transport_plan.journey_scope,
                origin=transport_plan.origin,
                destination=transport_plan.destination,
                outbound_date=transport_plan.outbound_date,
                return_date=transport_plan.return_date,
                options=transport_options,
                display_options=list(evidence.transport.display_options),
                warnings=list(evidence.transport.warnings),
                error_code=evidence.transport.error_code,
            ),
            hotel=TripHotelSnapshot(
                enabled=hotel_plan.enabled,
                status=evidence.hotel.status,
                query=dict(evidence.hotel.query),
                queried_at=evidence.hotel.queried_at,
                destination=hotel_plan.destination,
                check_in_date=hotel_plan.check_in_date,
                check_out_date=hotel_plan.check_out_date,
                nearby_poi=hotel_plan.nearby_poi,
                keywords=hotel_plan.keywords,
                hotel_stars=list(hotel_plan.hotel_stars),
                max_nightly_price=hotel_plan.max_nightly_price,
                options=hotel_options,
                display_options=list(evidence.hotel.display_options),
                warnings=list(evidence.hotel.warnings),
                error_code=evidence.hotel.error_code,
            ),
            overall_status=evidence.overall_status,
            warnings=list(evidence.warnings),
            source_metadata=TripPlanSourceMetadata(
                planning_run_id=effective_run_id,
                generated_at=datetime.now(UTC),
                map_queried_at=map_evidence.queried_at if map_evidence is not None else None,
                weather_queried_at=(
                    weather_evidence.queried_at if weather_evidence is not None else None
                ),
                transport_queried_at=evidence.transport.queried_at,
                hotel_queried_at=evidence.hotel.queried_at,
            ),
        )

    def _build_day(
        self,
        day: MapDayEvidence,
        weather: DailyWeatherEvidence | None,
        *,
        planning_run_id: str,
        weather_queried_at: datetime | None,
    ) -> TripPlanDaySnapshot:
        day_id = uuid5(
            NAMESPACE_URL,
            f"trip-plan:{planning_run_id}:day:{day.day_index}:{day.date.isoformat()}",
        )
        places = [
            self._build_place(
                place,
                day_id=day_id,
            )
            for place in day.ordered_places()
        ]
        places_by_reference = {place.reference_id: place for place in places}
        route_legs = [
            TripPlanRouteLegSnapshot(
                origin_plan_item_id=places_by_reference[leg.origin_ref].plan_item_id,
                destination_plan_item_id=places_by_reference[leg.destination_ref].plan_item_id,
                mode=leg.mode,
                distance_meters=leg.distance_meters,
                duration_seconds=leg.duration_seconds,
                transfer_count=leg.transfer_count,
                route_summary=leg.route_summary,
                is_fallback=leg.is_fallback,
            )
            for leg in day.route_legs
            if leg.origin_ref in places_by_reference and leg.destination_ref in places_by_reference
        ]
        return TripPlanDaySnapshot(
            day_id=day_id,
            day_index=day.day_index,
            date=day.date,
            places=places,
            route_legs=route_legs,
            weather=_build_weather_snapshot(weather, queried_at=weather_queried_at),
            estimated_visit_minutes=day.estimated_visit_minutes,
            estimated_transport_minutes=day.estimated_transport_minutes,
            warnings=list(day.warnings),
        )

    def _build_place(
        self,
        place: MapPlaceEvidence,
        *,
        day_id: UUID,
    ) -> TripPlanPlaceSnapshot:
        plan_item_id = uuid5(
            NAMESPACE_URL,
            f"trip-plan:{day_id}:place:{place.reference_id}:{place.poi_id}",
        )
        return TripPlanPlaceSnapshot(
            plan_item_id=plan_item_id,
            provider_place_id=place.poi_id,
            reference_id=place.reference_id,
            name=place.name,
            address=place.address,
            poi_type=place.poi_type,
            location=place.location,
            adcode=place.adcode,
            city=place.city,
            source_query=place.search_query,
            source_rank=place.search_rank,
            candidate_score=place.candidate_score,
            estimated_visit_minutes=place.estimated_visit_minutes,
            matched_preferences=list(place.matched_preferences),
            selection_reasons=list(place.selection_reasons),
        )


def build_trip_plan_snapshot(
    evidence: JoinedTripEvidence,
    *,
    request_field_sources: Mapping[str, object] | None = None,
    planning_run_id: str | None = None,
) -> TripPlanSnapshot:
    return TripPlanSnapshotBuilder().build(
        evidence,
        request_field_sources=request_field_sources,
        planning_run_id=planning_run_id,
    )


def _build_weather_snapshot(
    weather: DailyWeatherEvidence | None,
    *,
    queried_at: datetime | None,
) -> TripPlanWeatherSnapshot:
    if weather is None:
        return TripPlanWeatherSnapshot(
            queried_at=queried_at,
            coverage="unavailable",
            advice=[_UNAVAILABLE_WEATHER_ADVICE],
        )
    return TripPlanWeatherSnapshot(
        queried_at=queried_at,
        coverage=weather.coverage,
        day_weather=weather.day_weather,
        night_weather=weather.night_weather,
        day_temperature=weather.day_temperature,
        night_temperature=weather.night_temperature,
        day_wind_direction=weather.day_wind_direction,
        night_wind_direction=weather.night_wind_direction,
        day_wind_power=weather.day_wind_power,
        night_wind_power=weather.night_wind_power,
        advice=build_weather_advice(weather),
        unavailable_reason=weather.unavailable_reason,
    )


def _normalize_field_sources(
    sources: Mapping[str, object] | None,
) -> dict[str, str]:
    if sources is None:
        return {}
    return {
        str(field): str(source)
        for field, source in sources.items()
        if str(field).strip() and str(source).strip()
    }


__all__ = [
    "TripPlanSnapshotBuilder",
    "build_trip_plan_snapshot",
]
