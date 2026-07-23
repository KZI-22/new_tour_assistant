from __future__ import annotations

import hashlib
import logging
from typing import Any

from app.graphs.trip_planning_state import TripPlanningState
from app.services.hotel_search_service import HotelSearchService
from app.services.intercity_transport_service import IntercityTransportService
from app.services.map_weather_collection_service import MapWeatherCollectionService
from app.services.trip_evidence_joiner import join_trip_evidence
from app.services.trip_itinerary_generator import TripItineraryGenerator
from app.services.trip_itinerary_renderer import render_trip_itinerary
from app.services.trip_plan_validator import TripPlanValidator
from app.services.trip_planner_logging import safe_log_value

logger = logging.getLogger(__name__)


class MapWeatherNode:
    def __init__(self, collection_service: MapWeatherCollectionService) -> None:
        self._collection_service = collection_service

    async def __call__(self, state: TripPlanningState) -> dict[str, Any]:
        request = state["request"]
        evidence = await self._collection_service.collect(request.core)
        return {
            "map_weather_evidence": evidence,
            "current_stage": "collecting_map_weather",
        }


class TransportNode:
    def __init__(self, service: IntercityTransportService) -> None:
        self._service = service

    async def __call__(self, state: TripPlanningState) -> dict[str, Any]:
        evidence = await self._service.search(state["capability_plan"].transport)
        return {"transport_evidence": evidence}


class HotelNode:
    def __init__(self, service: HotelSearchService) -> None:
        self._service = service

    async def __call__(self, state: TripPlanningState) -> dict[str, Any]:
        evidence = await self._service.search(state["capability_plan"].hotel)
        return {"hotel_evidence": evidence}


class EvidenceJoinNode:
    async def __call__(self, state: TripPlanningState) -> dict[str, Any]:
        evidence = join_trip_evidence(
            state["request"],
            state["capability_plan"],
            state["map_weather_evidence"],
            state["transport_evidence"],
            state["hotel_evidence"],
        )
        return {
            "joined_evidence": evidence,
            "current_stage": "joining_evidence",
        }


class GenerateItineraryNode:
    def __init__(self, generator: TripItineraryGenerator) -> None:
        self._generator = generator

    async def __call__(self, state: TripPlanningState) -> dict[str, Any]:
        narrative = await self._generator.generate(
            state["joined_evidence"],
            validation_issues=state.get("validation_issues"),
        )
        return {
            "narrative": narrative,
            "current_stage": "generating_itinerary",
        }


class RenderResponseNode:
    async def __call__(self, state: TripPlanningState) -> dict[str, Any]:
        answer = render_trip_itinerary(
            state["joined_evidence"],
            state["narrative"],
        )
        return {
            "final_answer": answer,
            "current_stage": "completed",
        }


class ValidateItineraryNode:
    def __init__(self, validator: TripPlanValidator | None = None) -> None:
        self._validator = validator or TripPlanValidator()

    async def __call__(self, state: TripPlanningState) -> dict[str, Any]:
        issues = self._validator.validate(
            state["joined_evidence"],
            state["narrative"],
        )
        _log_validation_issues(state, issues)
        return {
            "validation_issues": issues,
            "current_stage": "validating_itinerary",
        }


def _log_validation_issues(
    state: TripPlanningState,
    issues: list[Any],
) -> None:
    if not issues:
        return

    revision_count = state.get("revision_count", 0)
    planning_run_id = safe_log_value(state.get("planning_run_id", "unavailable"))
    issue_codes = ",".join(safe_log_value(issue.code) for issue in issues)
    issue_paths = ",".join(safe_log_value(issue.path) for issue in issues)
    log_method = logger.warning if revision_count < 1 else logger.error
    log_method(
        "event=trip_plan_validation_failed planning_run_id=%s "
        "node=validate_itinerary status=failed duration_ms=0 revision_count=%d "
        "issue_count=%d issue_codes=%s issue_paths=%s",
        planning_run_id,
        revision_count,
        len(issues),
        issue_codes,
        issue_paths,
    )
    for issue in issues:
        log_method(
            "event=trip_plan_validation_issue planning_run_id=%s "
            "node=validate_itinerary status=failed duration_ms=0 "
            "revision_count=%d code=%s path=%s reference_id=%s "
            "expected_summary=%s actual_summary=%s",
            planning_run_id,
            revision_count,
            safe_log_value(issue.code),
            safe_log_value(issue.path),
            _reference_fingerprint(issue.reference_id),
            safe_log_value(issue.expected_summary or "none"),
            safe_log_value(issue.actual_summary or "none"),
        )


def _reference_fingerprint(reference_id: str | None) -> str:
    if not reference_id:
        return "none"
    digest = hashlib.sha256(reference_id.encode("utf-8")).hexdigest()[:12]
    return f"sha256:{digest}"


__all__ = [
    "EvidenceJoinNode",
    "GenerateItineraryNode",
    "HotelNode",
    "MapWeatherNode",
    "RenderResponseNode",
    "TransportNode",
    "ValidateItineraryNode",
]
