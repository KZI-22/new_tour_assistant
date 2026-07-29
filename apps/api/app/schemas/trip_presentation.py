from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TripPresentationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TripSummaryContext(TripPresentationModel):
    origin_city: str | None = None
    destination_city: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    duration_days: int | None = Field(default=None, ge=1)
    interests: list[str] = Field(default_factory=list)
    food_preferences: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class TransportPresentationContext(TripPresentationModel):
    enabled: bool
    status: Literal["skipped", "usable", "empty", "failed"]
    modes: list[str] = Field(default_factory=list)
    journey_scope: str
    origin: str | None = None
    destination: str | None = None
    outbound_date: date | None = None
    return_date: date | None = None
    options: list[str] = Field(default_factory=list, max_length=20)
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None


class HotelPresentationContext(TripPresentationModel):
    enabled: bool
    status: Literal["skipped", "usable", "empty", "failed"]
    destination: str | None = None
    check_in_date: date | None = None
    check_out_date: date | None = None
    nearby_poi: str | None = None
    options: list[str] = Field(default_factory=list, max_length=20)
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None


class WeatherPresentationContext(TripPresentationModel):
    coverage: Literal["available", "unavailable"]
    day_weather: str | None = None
    night_weather: str | None = None
    day_temperature: str | None = None
    night_temperature: str | None = None
    advice: list[str] = Field(default_factory=list)


class PlacePresentationContext(TripPresentationModel):
    reference_id: str
    name: str
    address: str
    poi_type: str
    estimated_visit_minutes: int = Field(ge=15, le=360)
    matched_preferences: list[str] = Field(default_factory=list)
    selection_reasons: list[str] = Field(default_factory=list)


class RouteLegPresentationContext(TripPresentationModel):
    origin_ref: str
    origin_name: str
    destination_ref: str
    destination_name: str
    mode: Literal["walking", "transit", "driving", "estimated", "unverified"]
    distance_meters: int | None = Field(default=None, ge=0)
    duration_minutes: int | None = Field(default=None, ge=0)
    transfer_count: int | None = Field(default=None, ge=0)
    is_fallback: bool


class DayPresentationContext(TripPresentationModel):
    day_index: int = Field(ge=1)
    date: date
    weekday: str
    weather: WeatherPresentationContext
    estimated_visit_minutes: int = Field(ge=0)
    estimated_transport_minutes: int = Field(ge=0)
    places: list[PlacePresentationContext]
    route_legs: list[RouteLegPresentationContext] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class TripPresentationContext(TripPresentationModel):
    trip: TripSummaryContext
    transport: TransportPresentationContext
    hotel: HotelPresentationContext
    days: list[DayPresentationContext]
    warnings: list[str] = Field(default_factory=list)


__all__ = [
    "DayPresentationContext",
    "HotelPresentationContext",
    "PlacePresentationContext",
    "RouteLegPresentationContext",
    "TransportPresentationContext",
    "TripPresentationContext",
    "TripSummaryContext",
    "WeatherPresentationContext",
]
