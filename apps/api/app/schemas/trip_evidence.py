from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field

from app.schemas.map_planning import MapTripEvidence
from app.schemas.trip_capabilities import CapabilityPlan, TripPlanningRequest
from app.schemas.trip_planning import TripPlanningModel, TripWeatherEvidence


class EvidenceStatus(StrEnum):
    SKIPPED = "skipped"
    USABLE = "usable"
    EMPTY = "empty"
    FAILED = "failed"


class RawCapabilityEvidence(TripPlanningModel):
    capability: Literal["transport", "hotel"]
    provider: Literal["flyai"] = "flyai"
    status: EvidenceStatus
    query: dict[str, object]
    queried_at: datetime
    duration_ms: int = Field(ge=0)
    data: object | None = None
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None


class MapWeatherEvidenceBundle(TripPlanningModel):
    status: Literal["usable", "partial", "failed"]
    map: MapTripEvidence | None = None
    weather: TripWeatherEvidence | None = None
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None


class JoinedTripEvidence(TripPlanningModel):
    request: TripPlanningRequest
    capabilities: CapabilityPlan
    map_weather: MapWeatherEvidenceBundle
    transport: RawCapabilityEvidence
    hotel: RawCapabilityEvidence
    overall_status: Literal["usable", "partial", "failed"]
    warnings: list[str] = Field(default_factory=list)


__all__ = [
    "EvidenceStatus",
    "JoinedTripEvidence",
    "MapWeatherEvidenceBundle",
    "RawCapabilityEvidence",
]
