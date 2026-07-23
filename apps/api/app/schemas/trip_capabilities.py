from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import Field

from app.schemas.trip_planning import CityTripRequest, TripPlanningModel


class CapabilityAction(StrEnum):
    UNSPECIFIED = "unspecified"
    ENABLE = "enable"
    DISABLE = "disable"


class TransportMode(StrEnum):
    FLIGHT = "flight"
    TRAIN = "train"


class JourneyScope(StrEnum):
    UNSPECIFIED = "unspecified"
    ONE_WAY = "one_way"
    ROUND_TRIP = "round_trip"


class TransportIntent(TripPlanningModel):
    action: CapabilityAction = CapabilityAction.UNSPECIFIED
    modes: list[TransportMode] = Field(default_factory=list)
    journey_scope: JourneyScope = JourneyScope.UNSPECIFIED
    origin_city: str | None = Field(default=None, max_length=80)
    outbound_date: date | None = None
    return_date: date | None = None
    max_price: float | None = Field(default=None, gt=0)
    evidence_text: str | None = Field(default=None, max_length=500)


class HotelIntent(TripPlanningModel):
    action: CapabilityAction = CapabilityAction.UNSPECIFIED
    check_in_date: date | None = None
    check_out_date: date | None = None
    nearby_poi: str | None = Field(default=None, max_length=200)
    keywords: str | None = Field(default=None, max_length=200)
    hotel_stars: list[int] = Field(default_factory=list)
    max_nightly_price: float | None = Field(default=None, gt=0)
    evidence_text: str | None = Field(default=None, max_length=500)


class TripPlanningRequest(TripPlanningModel):
    core: CityTripRequest
    transport: TransportIntent = Field(default_factory=TransportIntent)
    hotel: HotelIntent = Field(default_factory=HotelIntent)


DerivationSource = Literal[
    "explicit_user_input",
    "conversation_context",
    "derived_from_trip_dates",
    "default_policy",
]


class ValueDerivation(TripPlanningModel):
    field: str
    value: str
    source: DerivationSource
    explanation: str


class TransportCapabilityPlan(TripPlanningModel):
    enabled: bool = False
    modes: list[TransportMode] = Field(default_factory=list)
    journey_scope: JourneyScope = JourneyScope.UNSPECIFIED
    origin: str | None = None
    destination: str | None = None
    outbound_date: date | None = None
    return_date: date | None = None
    max_price: float | None = Field(default=None, gt=0)
    reason: str | None = None


class HotelCapabilityPlan(TripPlanningModel):
    enabled: bool = False
    destination: str | None = None
    check_in_date: date | None = None
    check_out_date: date | None = None
    nearby_poi: str | None = None
    keywords: str | None = None
    hotel_stars: list[int] = Field(default_factory=list)
    max_nightly_price: float | None = Field(default=None, gt=0)
    reason: str | None = None


class CapabilityPlan(TripPlanningModel):
    map_weather_enabled: Literal[True] = True
    transport: TransportCapabilityPlan = Field(default_factory=TransportCapabilityPlan)
    hotel: HotelCapabilityPlan = Field(default_factory=HotelCapabilityPlan)
    derivations: list[ValueDerivation] = Field(default_factory=list)


class MissingRequirement(TripPlanningModel):
    field: str
    capability: Literal["core", "transport", "hotel"]
    display_name: str
    reason: str


class RequirementCheck(TripPlanningModel):
    complete: bool
    missing: list[MissingRequirement] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


__all__ = [
    "CapabilityAction",
    "CapabilityPlan",
    "DerivationSource",
    "HotelCapabilityPlan",
    "HotelIntent",
    "JourneyScope",
    "MissingRequirement",
    "RequirementCheck",
    "TransportCapabilityPlan",
    "TransportIntent",
    "TransportMode",
    "TripPlanningRequest",
    "ValueDerivation",
]
