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
from app.services.structured_output_service import (
    StructuredOutputError,
    StructuredOutputService,
)
from app.services.trip_evidence_joiner import join_trip_evidence
from app.services.trip_itinerary_generator import TripItineraryGenerator
from app.services.trip_itinerary_renderer import render_trip_itinerary
from app.services.trip_plan_validator import TripPlanValidator
from app.services.trip_planner_logging import safe_log_value
from app.services.trip_requirement_extractor import (
    apply_trip_request_overrides,
    trip_request_extraction_prompt,
)

logger = logging.getLogger(__name__)

_TRIP_EXTRACTION_SYSTEM_PROMPT = """你只负责从最近对话提取统一旅行规划请求。
不得猜测城市、日期、天数、交通、酒店或预算。交通和酒店只有在用户明确要求实时查询、
查找、推荐或比较时才能启用；仅描述出发地、交通方式、住宿区域或已有预订不能启用。
只输出符合指定 JSON Schema 的结构化结果。"""


class ExtractRequirementsNode:
    def __init__(
        self,
        model: BaseChatModel,
        *,
        timeout_seconds: float,
    ) -> None:
        self._structured = StructuredOutputService(model)
        self._timeout_seconds = timeout_seconds

    async def __call__(self, state: TripPlanningState) -> dict[str, Any]:
        try:
            request = await self._structured.invoke(
                TripPlanningRequest,
                _TRIP_EXTRACTION_SYSTEM_PROMPT,
                trip_request_extraction_prompt(state["messages"]),
                timeout_seconds=self._timeout_seconds,
            )
            method = "model"
        except StructuredOutputError:
            request = TripPlanningRequest(core=CityTripRequest())
            method = "fallback"
        request, overrides = apply_trip_request_overrides(
            request,
            state["messages"],
        )
        return {
            "request": request,
            "extraction_method": method,
            "extraction_overrides": overrides,
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
    "ClarifyRequirementsNode",
    "EvidenceJoinNode",
    "ExtractRequirementsNode",
    "GenerateItineraryNode",
    "HotelNode",
    "MapWeatherNode",
    "RenderResponseNode",
    "ResolveCapabilitiesNode",
    "TransportNode",
    "ValidateItineraryNode",
    "ValidateRequirementsNode",
]
