from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.schemas.amap import AmapCoordinate
from app.schemas.trip_capabilities import (
    CapabilityPlan,
    JourneyScope,
    TransportMode,
    TripPlanningRequest,
)
from app.schemas.trip_evidence import EvidenceStatus
from app.schemas.trip_options import HotelOptionSnapshot, TransportOptionSnapshot
from app.schemas.trip_planning import TripPlanningModel

RouteMode = Literal["walking", "transit", "driving", "estimated", "unverified"]


class TripPlanPlaceSnapshot(TripPlanningModel):
    plan_item_id: UUID
    provider: Literal["amap"] = "amap"
    provider_place_id: str
    reference_id: str
    name: str
    address: str
    poi_type: str
    location: AmapCoordinate
    adcode: str | None = None
    city: str | None = None
    source_query: str
    source_rank: int = Field(ge=1)
    candidate_score: float
    estimated_visit_minutes: int = Field(ge=15, le=360)
    matched_preferences: list[str] = Field(default_factory=list)
    selection_reasons: list[str] = Field(default_factory=list)


class TripPlanRouteLegSnapshot(TripPlanningModel):
    origin_plan_item_id: UUID
    destination_plan_item_id: UUID
    mode: RouteMode
    distance_meters: int | None = Field(default=None, ge=0)
    duration_seconds: int | None = Field(default=None, ge=0)
    transfer_count: int | None = Field(default=None, ge=0)
    route_summary: str | None = None
    is_fallback: bool = False


class TripPlanWeatherSnapshot(TripPlanningModel):
    provider: Literal["amap"] = "amap"
    queried_at: datetime | None = None
    coverage: Literal["available", "unavailable"]
    day_weather: str | None = None
    night_weather: str | None = None
    day_temperature: str | None = None
    night_temperature: str | None = None
    day_wind_direction: str | None = None
    night_wind_direction: str | None = None
    day_wind_power: str | None = None
    night_wind_power: str | None = None
    advice: list[str] = Field(default_factory=list)
    unavailable_reason: str | None = None


class TripPlanDaySnapshot(TripPlanningModel):
    day_id: UUID
    day_index: int = Field(ge=1)
    date: date
    places: list[TripPlanPlaceSnapshot]
    route_legs: list[TripPlanRouteLegSnapshot] = Field(default_factory=list)
    weather: TripPlanWeatherSnapshot
    estimated_visit_minutes: int = Field(ge=0)
    estimated_transport_minutes: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)


class TripTransportSnapshot(TripPlanningModel):
    enabled: bool
    status: EvidenceStatus
    query: dict[str, object]
    queried_at: datetime | None = None
    modes: list[TransportMode] = Field(default_factory=list)
    journey_scope: JourneyScope
    origin: str | None = None
    destination: str | None = None
    outbound_date: date | None = None
    return_date: date | None = None
    options: list[TransportOptionSnapshot] = Field(default_factory=list)
    display_options: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None


class TripHotelSnapshot(TripPlanningModel):
    enabled: bool
    status: EvidenceStatus
    query: dict[str, object]
    queried_at: datetime | None = None
    destination: str | None = None
    check_in_date: date | None = None
    check_out_date: date | None = None
    nearby_poi: str | None = None
    keywords: str | None = None
    hotel_stars: list[int] = Field(default_factory=list)
    max_nightly_price: float | None = Field(default=None, gt=0)
    options: list[HotelOptionSnapshot] = Field(default_factory=list)
    display_options: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None


class TripPlanSourceMetadata(TripPlanningModel):
    planning_run_id: str
    generated_at: datetime
    map_queried_at: datetime | None = None
    weather_queried_at: datetime | None = None
    transport_queried_at: datetime | None = None
    hotel_queried_at: datetime | None = None


class TripPlanSnapshot(TripPlanningModel):
    schema_version: Literal["trip_plan.v1"] = "trip_plan.v1"
    request: TripPlanningRequest
    request_field_sources: dict[str, str] = Field(default_factory=dict)
    capabilities: CapabilityPlan
    days: list[TripPlanDaySnapshot]
    transport: TripTransportSnapshot
    hotel: TripHotelSnapshot
    overall_status: Literal["usable", "partial", "failed"]
    warnings: list[str] = Field(default_factory=list)
    source_metadata: TripPlanSourceMetadata


__all__ = [
    "RouteMode",
    "TripHotelSnapshot",
    "TripPlanDaySnapshot",
    "TripPlanPlaceSnapshot",
    "TripPlanRouteLegSnapshot",
    "TripPlanSnapshot",
    "TripPlanSourceMetadata",
    "TripPlanWeatherSnapshot",
    "TripTransportSnapshot",
]
