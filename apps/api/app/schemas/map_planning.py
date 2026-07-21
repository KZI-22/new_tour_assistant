from __future__ import annotations

import datetime as dt
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.amap import AmapCoordinate


class MapPlanningModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class MapPlaceEvidence(MapPlanningModel):
    reference_id: str = Field(min_length=1, max_length=100)
    poi_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    address: str
    poi_type: str
    location: AmapCoordinate
    adcode: str | None = None
    city: str | None = None
    search_query: str = Field(min_length=1, max_length=100)
    search_rank: int = Field(ge=1)
    estimated_visit_minutes: int = Field(ge=15, le=360)
    matched_preferences: list[str] = Field(default_factory=list)
    selection_reasons: list[str] = Field(default_factory=list)
    candidate_score: float


class RouteLegEvidence(MapPlanningModel):
    origin_ref: str = Field(min_length=1, max_length=100)
    destination_ref: str = Field(min_length=1, max_length=100)
    mode: Literal["walking", "transit", "driving", "estimated", "unverified"]
    distance_meters: int | None = Field(default=None, ge=0)
    duration_seconds: int | None = Field(default=None, ge=0)
    transfer_count: int | None = Field(default=None, ge=0)
    route_summary: str | None = None
    is_fallback: bool = False


class MapDayEvidence(MapPlanningModel):
    day_index: int = Field(ge=1)
    date: dt.date
    attractions: list[MapPlaceEvidence] = Field(default_factory=list, max_length=5)
    estimated_visit_minutes: int = Field(default=0, ge=0)
    estimated_transport_minutes: int = Field(default=0, ge=0)
    route_legs: list[RouteLegEvidence] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def ordered_places(self) -> list[MapPlaceEvidence]:
        return list(self.attractions)


class ExcludedAttractionEvidence(MapPlanningModel):
    poi_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)


class MapTripEvidence(MapPlanningModel):
    provider: Literal["amap"] = "amap"
    city: str
    planning_run_id: str = Field(min_length=1, max_length=100)
    queried_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC))
    days: list[MapDayEvidence]
    excluded_attractions: list[ExcludedAttractionEvidence] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class MapPlaceNarrative(MapPlanningModel):
    reference_id: str = Field(min_length=1, max_length=100)
    recommendation_reason: str = Field(min_length=1, max_length=1_000)


class MapDayNarrative(MapPlanningModel):
    day_index: int = Field(ge=1)
    date: dt.date
    theme: str = Field(min_length=1, max_length=200)
    places: list[MapPlaceNarrative]
    weather_advice: list[str] = Field(default_factory=list)
    tips: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_references(self) -> Self:
        references = [place.reference_id for place in self.places]
        if len(references) != len(set(references)):
            raise ValueError("map narrative place references must be unique")
        return self


class MapNarrativePlan(MapPlanningModel):
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=2_000)
    days: list[MapDayNarrative]
    practical_tips: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


__all__ = [
    "ExcludedAttractionEvidence",
    "MapDayEvidence",
    "MapDayNarrative",
    "MapNarrativePlan",
    "MapPlaceEvidence",
    "MapPlaceNarrative",
    "MapTripEvidence",
    "RouteLegEvidence",
]
