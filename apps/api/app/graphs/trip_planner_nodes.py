from __future__ import annotations

import hashlib
import logging
from typing import Any

from langchain_core.language_models import BaseChatModel

from app.core.settings import Settings
from app.graphs.trip_planning_state import TripPlanningState
from app.schemas.trip_capabilities import TripPlanningRequest
from app.schemas.trip_planning import CityTripRequest
from app.services.capability_resolver import (
    check_requirements,
    render_requirement_clarification,
    resolve_capabilities,
)
from app.services.hotel_search_service import HotelSearchService
from app.services.intercity_transport_service import IntercityTransportService
from app.services.map_weather_collection_service import MapWeatherCollectionService
from app.services.rule_first_trip_requirement_extractor import (
    RuleFirstTripRequirementExtractor,
)
from app.services.trip_evidence_joiner import join_trip_evidence
from app.services.trip_itinerary_generator import build_trip_narrative_skeleton
from app.services.trip_itinerary_renderer import render_trip_itinerary
from app.services.trip_plan_validator import TripPlanValidator
from app.services.trip_planner_logging import safe_log_value

logger = logging.getLogger(__name__)


class ExtractRequirementsNode:
    def __init__(
        self,
        model: BaseChatModel,
        *,
        timeout_seconds: float,
    ) -> None:
        self._extractor = RuleFirstTripRequirementExtractor(
            model,
            timeout_seconds=timeout_seconds,
        )

    async def __call__(self, state: TripPlanningState) -> dict[str, Any]:
        try:
            result = await self._extractor.extract(state["messages"])
        except Exception:
            logger.exception("Rule-first trip requirement extraction failed")
            request = TripPlanningRequest(core=CityTripRequest())
            return {
                "request": request,
                "extraction_method": "fallback",
                "extraction_overrides": {
                    "explicit_duration_override": False,
                    "explicit_start_date_override": False,
                },
                "extraction_details": {
                    "path": "fallback",
                    "rule_duration_ms": 0,
                    "llm_duration_ms": 0,
                    "llm_call_count": 0,
                    "llm_retry_count": 0,
                    "ambiguity_fields": [],
                    "field_sources": {},
                    "explicit_missing": [
                        "core.destination_city",
                        "core.duration_days",
                        "core.start_date",
                    ],
                },
                "current_stage": "understanding_request",
            }

        metrics = result.metrics.model_dump()
        return {
            "request": result.request,
            "extraction_method": result.metrics.path,
            "extraction_overrides": {
                "explicit_duration_override": (
                    result.field_sources.get("core.duration_days") == "explicit_rule"
                ),
                "explicit_start_date_override": (
                    result.field_sources.get("core.start_date") == "explicit_rule"
                ),
            },
            "extraction_details": {
                **metrics,
                "field_sources": result.field_sources,
                "explicit_missing": result.explicit_missing,
                "ambiguity_count": len(result.ambiguities),
            },
            "current_stage": "understanding_request",
        }


class ResolveCapabilitiesNode:
    async def __call__(self, state: TripPlanningState) -> dict[str, Any]:
        plan = resolve_capabilities(
            state["request"],
            state["messages"],
        )
        return {
            "capability_plan": plan,
            "current_stage": "resolving_capabilities",
        }


class ValidateRequirementsNode:
    def __init__(self, settings: Settings) -> None:
        self._maximum_days = settings.trip_planner_max_days

    async def __call__(self, state: TripPlanningState) -> dict[str, Any]:
        check = check_requirements(
            state["request"],
            state["capability_plan"],
            maximum_days=self._maximum_days,
        )
        return {
            "requirement_check": check,
            "current_stage": "checking_requirements",
        }


class ClarifyRequirementsNode:
    async def __call__(self, state: TripPlanningState) -> dict[str, Any]:
        return {
            "final_answer": render_requirement_clarification(state["requirement_check"]),
            "current_stage": "awaiting_clarification",
        }


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


class BuildItinerarySkeletonNode:
    def __init__(self, validator: TripPlanValidator | None = None) -> None:
        self._validator = validator or TripPlanValidator()

    async def __call__(self, state: TripPlanningState) -> dict[str, Any]:
        evidence = state["joined_evidence"]
        narrative = build_trip_narrative_skeleton(evidence)
        issues = self._validator.validate(evidence, narrative)
        _log_validation_issues(state, issues, node="build_itinerary_skeleton")
        return {
            "narrative_skeleton": narrative,
            "skeleton_validation_issues": issues,
            "skeleton_answer": (render_trip_itinerary(evidence, narrative) if not issues else ""),
            "current_stage": "building_itinerary_skeleton",
        }


def _log_validation_issues(
    state: TripPlanningState,
    issues: list[Any],
    *,
    node: str = "validate_itinerary",
) -> None:
    if not issues:
        return

    planning_run_id = safe_log_value(state.get("planning_run_id", "unavailable"))
    issue_codes = ",".join(safe_log_value(issue.code) for issue in issues)
    issue_paths = ",".join(safe_log_value(issue.path) for issue in issues)
    logger.error(
        "event=trip_plan_validation_failed planning_run_id=%s "
        "node=%s status=failed duration_ms=0 "
        "issue_count=%d issue_codes=%s issue_paths=%s",
        planning_run_id,
        node,
        len(issues),
        issue_codes,
        issue_paths,
    )
    for issue in issues:
        logger.error(
            "event=trip_plan_validation_issue planning_run_id=%s "
            "node=%s status=failed duration_ms=0 "
            "code=%s path=%s reference_id=%s "
            "expected_summary=%s actual_summary=%s",
            planning_run_id,
            node,
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
    "BuildItinerarySkeletonNode",
    "ClarifyRequirementsNode",
    "EvidenceJoinNode",
    "ExtractRequirementsNode",
    "HotelNode",
    "MapWeatherNode",
    "ResolveCapabilitiesNode",
    "TransportNode",
    "ValidateRequirementsNode",
]
