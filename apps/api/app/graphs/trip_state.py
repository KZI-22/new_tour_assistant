from __future__ import annotations

from typing import Any, TypedDict

from app.schemas.chat import ChatMessage
from app.schemas.itinerary import (
    AffectedSection,
    HotelOption,
    ItineraryPlan,
    PlanningIntent,
    TransportOption,
    TripRequest,
    ValidationIssue,
)


class TripPlanningState(TypedDict, total=False):
    messages: list[ChatMessage]
    intent: PlanningIntent
    is_plan_revision: bool
    revision_instructions: str | None
    affected_sections: list[AffectedSection]
    change_summary: str | None
    request_extraction_available: bool
    itinerary_generation_available: bool
    experience_validation_available: bool

    request: TripRequest | None
    missing_fields: list[str]
    requirement_errors: list[str]

    transport_results: list[TransportOption]
    hotel_results: list[HotelOption]
    poi_results: list[dict[str, Any]]
    weather_results: list[dict[str, Any]]
    route_results: list[dict[str, Any]]
    tool_evidence: list[dict[str, Any]]
    tool_failures: list[str]

    current_plan: ItineraryPlan | None
    previous_plan: ItineraryPlan | None
    previous_request: TripRequest | None
    validation_issues: list[ValidationIssue]
    revision_count: int

    plan_id: str | None
    plan_version: int | None
    current_stage: str
    clarification_question: str | None
    final_answer: str | None


__all__ = ["TripPlanningState"]
