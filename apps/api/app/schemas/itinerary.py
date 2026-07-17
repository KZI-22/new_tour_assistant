from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PlanningIntent = Literal[
    "general_chat",
    "single_travel_query",
    "new_trip_plan",
    "modify_trip_plan",
]
AffectedSection = Literal[
    "dates",
    "destination",
    "transport",
    "hotel",
    "activities",
    "weather",
    "routes",
    "budget",
]
IssueSeverity = Literal["info", "warning", "error"]


class ItineraryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TripRequest(ItineraryModel):
    origin: str | None = None
    destinations: list[str] = Field(default_factory=list)

    start_date: date | None = None
    end_date: date | None = None
    duration_days: int | None = Field(default=None, ge=1)

    traveler_count: int | None = Field(default=None, ge=1)
    adults: int | None = Field(default=None, ge=0)
    children: int | None = Field(default=None, ge=0)

    total_budget: float | None = Field(default=None, ge=0)
    hotel_budget_per_night: float | None = Field(default=None, ge=0)

    transport_preferences: list[str] = Field(default_factory=list)
    hotel_preferences: dict[str, Any] = Field(default_factory=dict)
    interests: list[str] = Field(default_factory=list)
    pace: Literal["relaxed", "moderate", "packed"] | None = None

    must_visit: list[str] = Field(default_factory=list)
    avoid_places: list[str] = Field(default_factory=list)
    special_requirements: list[str] = Field(default_factory=list)

    @field_validator(
        "origin",
        "destinations",
        "transport_preferences",
        "interests",
        "must_visit",
        "avoid_places",
        "special_requirements",
    )
    @classmethod
    def normalize_text_values(cls, value: Any) -> Any:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        if isinstance(value, list):
            normalized = [str(item).strip() for item in value if str(item).strip()]
            return list(dict.fromkeys(normalized))
        return value

    @model_validator(mode="after")
    def complete_dates_and_travelers(self) -> Self:
        if self.start_date is not None and self.end_date is None and self.duration_days:
            self.end_date = self.start_date + timedelta(days=self.duration_days - 1)
        elif self.start_date is not None and self.end_date is not None:
            self.duration_days = (self.end_date - self.start_date).days + 1

        if self.traveler_count is None and (self.adults is not None or self.children is not None):
            total = (self.adults or 0) + (self.children or 0)
            self.traveler_count = total or None
        return self


class TransportOption(ItineraryModel):
    transport_type: Literal["flight", "train", "other"]
    provider: str | None = None
    timezone: str | None = None

    departure_city: str
    arrival_city: str
    departure_time: datetime | None = None
    arrival_time: datetime | None = None

    flight_number: str | None = None
    train_number: str | None = None
    origin_station: str | None = None
    destination_station: str | None = None

    price: float | None = Field(default=None, ge=0)
    seat_or_cabin: str | None = None
    duration_minutes: int | None = Field(default=None, ge=0)

    source_tool: str
    source_reference: str | None = None
    queried_at: datetime | None = None


class HotelOption(ItineraryModel):
    name: str
    address: str | None = None
    poi_id: str | None = None
    coordinates: str | None = None

    star_level: str | None = None
    room_type: str | None = None
    bed_type: str | None = None
    nightly_price: float | None = Field(default=None, ge=0)
    total_price: float | None = Field(default=None, ge=0)

    check_in_date: date
    check_out_date: date
    source_tool: str
    source_reference: str | None = None
    queried_at: datetime | None = None


class Activity(ItineraryModel):
    start_time: time | None = None
    end_time: time | None = None

    place_name: str
    poi_id: str | None = None
    coordinates: str | None = None
    activity_type: str
    estimated_duration_minutes: int | None = Field(default=None, ge=0)
    estimated_cost: float | None = Field(default=None, ge=0)
    indoor: bool | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def validate_time_order(self) -> Self:
        if (
            self.start_time is not None
            and self.end_time is not None
            and self.end_time <= self.start_time
        ):
            raise ValueError("end_time must be later than start_time")
        return self


class DayPlan(ItineraryModel):
    date: date
    day_index: int = Field(ge=1)
    theme: str | None = None
    activities: list[Activity] = Field(default_factory=list)
    estimated_transport_time_minutes: int = Field(default=0, ge=0)
    estimated_activity_cost: float | None = Field(default=None, ge=0)
    weather_summary: str | None = None
    warnings: list[str] = Field(default_factory=list)


class BudgetSummary(ItineraryModel):
    transport_cost: float | None = Field(default=None, ge=0)
    hotel_cost: float | None = Field(default=None, ge=0)
    activity_cost: float | None = Field(default=None, ge=0)
    local_transport_cost: float | None = Field(default=None, ge=0)
    food_estimate: float | None = Field(default=None, ge=0)
    total_estimated_cost: float | None = Field(default=None, ge=0)
    user_budget: float | None = Field(default=None, ge=0)
    over_budget: bool | None = None
    assumptions: list[str] = Field(default_factory=list)


class ItineraryPlan(ItineraryModel):
    title: str
    origin: str | None = None
    destination: str
    start_date: date
    end_date: date
    outbound_transport: TransportOption | None = None
    return_transport: TransportOption | None = None
    hotel: HotelOption | None = None
    days: list[DayPlan] = Field(default_factory=list)
    budget: BudgetSummary | None = None
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_plan_dates(self) -> Self:
        if self.end_date < self.start_date:
            raise ValueError("end_date cannot be earlier than start_date")
        return self


class ValidationIssue(ItineraryModel):
    code: str
    severity: IssueSeverity
    message: str
    day_index: int | None = Field(default=None, ge=1)
    activity_index: int | None = Field(default=None, ge=0)
    suggested_action: str | None = None


class ExtractedLocation(ItineraryModel):
    """A location candidate anchored to the user's original wording."""

    value: str | None = None
    evidence: str | None = None
    explicit: bool = False

    @model_validator(mode="after")
    def validate_explicit_evidence(self) -> Self:
        if self.explicit and (not self.value or not self.evidence):
            raise ValueError("explicit locations require both value and evidence")
        return self


class TripRequestExtraction(ItineraryModel):
    request: TripRequest
    origin_location: ExtractedLocation | None = None
    destination_locations: list[ExtractedLocation] = Field(default_factory=list)
    is_plan_revision: bool = False
    revision_instructions: str | None = None
    affected_sections: list[AffectedSection] = Field(default_factory=list)
    change_summary: str | None = None


class ExperienceValidation(ItineraryModel):
    issues: list[ValidationIssue] = Field(default_factory=list)


__all__ = [
    "Activity",
    "AffectedSection",
    "BudgetSummary",
    "DayPlan",
    "ExtractedLocation",
    "ExperienceValidation",
    "HotelOption",
    "ItineraryPlan",
    "PlanningIntent",
    "TransportOption",
    "TripRequest",
    "TripRequestExtraction",
    "ValidationIssue",
]
