from __future__ import annotations

from typing import TypedDict

from app.schemas.chat import ChatMessage
from app.schemas.trip_capabilities import CapabilityPlan, RequirementCheck, TripPlanningRequest
from app.schemas.trip_evidence import (
    JoinedTripEvidence,
    MapWeatherEvidenceBundle,
    RawCapabilityEvidence,
)
from app.schemas.trip_validation import ValidationIssue


class TripPlanningState(TypedDict, total=False):
    messages: list[ChatMessage]
    planning_run_id: str
    conversation_id: str
    assistant_message_id: str
    extraction_method: str
    extraction_overrides: dict[str, bool]

    request: TripPlanningRequest
    capability_plan: CapabilityPlan
    requirement_check: RequirementCheck

    map_weather_evidence: MapWeatherEvidenceBundle
    transport_evidence: RawCapabilityEvidence
    hotel_evidence: RawCapabilityEvidence
    joined_evidence: JoinedTripEvidence

    narrative: object
    validation_issues: list[ValidationIssue]
    revision_count: int

    final_answer: str
    controlled_error: bool
    current_stage: str


__all__ = ["TripPlanningState"]
