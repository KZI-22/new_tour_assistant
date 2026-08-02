from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from app.schemas.amap import AmapCoordinate
from app.schemas.trip_options import TripOptionSnapshot
from app.schemas.trip_planning import TripPlanningModel, TripPreference


class StructuredTripRequest(TripPlanningModel):
    destination_city: str = Field(min_length=1, max_length=80)
    start_date: date
    duration_days: int = Field(ge=1, le=10)
    interests: list[TripPreference] = Field(default_factory=list, max_length=9)

    @field_validator("interests")
    @classmethod
    def unique_interests(cls, value: list[TripPreference]) -> list[TripPreference]:
        return list(dict.fromkeys(value))


class TripPlanCreateRequest(TripPlanningModel):
    model_id: str = Field(min_length=1, max_length=100)
    request: StructuredTripRequest
    plan_id: UUID | None = None


class RestaurantSearchInput(TripPlanningModel):
    city: str = Field(min_length=1, max_length=80)
    keyword: str = Field(default="本地特色美食", min_length=1, max_length=100)
    limit: int = Field(default=10, ge=1, le=10)


class RestaurantRecommendation(TripPlanningModel):
    provider: Literal["amap"] = "amap"
    provider_place_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    address: str = ""
    poi_type: str = ""
    rating: float | None = Field(default=None, ge=0, le=5)
    business_area: str | None = None
    city: str | None = None
    adcode: str | None = None
    location: AmapCoordinate
    source_queries: list[str] = Field(default_factory=list, max_length=10)
    best_search_rank: int = Field(ge=1)
    selection_reasons: list[str] = Field(default_factory=list, max_length=5)
    recommendation_reason: str = Field(min_length=1, max_length=500)


class RestaurantSearchEvidence(TripPlanningModel):
    status: Literal["usable", "empty", "failed"]
    queried_at: datetime
    recommendations: list[RestaurantRecommendation] = Field(default_factory=list, max_length=3)
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None


class TravelPlanSummaryResponse(TripPlanningModel):
    plan_id: UUID
    title: str
    status: Literal["draft", "active", "archived"]
    current_version: int = Field(ge=1)
    destination_city: str
    start_date: date
    duration_days: int = Field(ge=1, le=10)
    created_at: datetime
    updated_at: datetime


class DirectTravelSearchResponse(TripPlanningModel):
    kind: Literal["hotel", "flight", "train"]
    tool_call_id: str
    tool_name: Literal["search_hotel", "search_flight", "search_train"]
    arguments: dict[str, object]
    success: bool
    summary: str
    options: list[TripOptionSnapshot] = Field(default_factory=list, max_length=10)
    error_code: str | None = None
    provider_error_code: str | None = None
    provider_item_count: int = Field(default=0, ge=0)
    rejected_item_count: int = Field(default=0, ge=0)
    queried_at: datetime


class TravelSearchPresentation(TripPlanningModel):
    summary: str = Field(min_length=1, max_length=1_000)


__all__ = [
    "RestaurantRecommendation",
    "RestaurantSearchEvidence",
    "RestaurantSearchInput",
    "DirectTravelSearchResponse",
    "StructuredTripRequest",
    "TravelPlanSummaryResponse",
    "TravelSearchPresentation",
    "TripPlanCreateRequest",
]
