from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any

from app.graphs.trip_planning_state import TripPlanningState
from app.schemas.trip_evidence import EvidenceStatus

logger = logging.getLogger(__name__)

NodeCallable = Callable[[TripPlanningState], Awaitable[dict[str, Any]]]


def logged_trip_planner_node(node: str, function: NodeCallable) -> NodeCallable:
    async def invoke(state: TripPlanningState) -> dict[str, Any]:
        started = perf_counter()
        context = _context_fields(state)
        logger.info(
            "event=trip_planner_node_started %s node=%s status=started duration_ms=0",
            context,
            node,
        )
        try:
            result = await function(state)
        except asyncio.CancelledError:
            logger.info(
                "event=trip_planner_node_cancelled %s node=%s status=cancelled duration_ms=%d",
                context,
                node,
                _duration_ms(started),
            )
            raise
        except Exception as exc:
            logger.error(
                "event=trip_planner_node_failed %s node=%s status=failed "
                "duration_ms=%d exception_type=%s",
                context,
                node,
                _duration_ms(started),
                type(exc).__name__,
                exc_info=(
                    RuntimeError,
                    RuntimeError("trip planner node failed; exception detail redacted"),
                    exc.__traceback__,
                ),
            )
            raise
        metrics = _completion_metrics(node, state, result)
        log_method = _completion_log_method(node, result)
        log_method(
            "event=trip_planner_node_completed %s node=%s status=completed duration_ms=%d%s",
            context,
            node,
            _duration_ms(started),
            metrics,
        )
        return result

    return invoke


def _completion_log_method(
    node: str,
    result: dict[str, Any],
) -> Callable[..., None]:
    if node == "collect_map_weather":
        evidence = result.get("map_weather_evidence")
        if evidence is not None and evidence.status == "failed":
            return logger.error
    if node in {"collect_transport", "collect_hotels"}:
        key = "transport_evidence" if node == "collect_transport" else "hotel_evidence"
        evidence = result.get(key)
        if evidence is not None and evidence.status is EvidenceStatus.FAILED:
            return logger.warning
    if node == "join_evidence":
        evidence = result.get("joined_evidence")
        if evidence is not None:
            if evidence.overall_status == "failed":
                return logger.error
            if evidence.overall_status == "partial":
                return logger.warning
    if node == "controlled_failure":
        return logger.error
    return logger.info


def _context_fields(state: TripPlanningState) -> str:
    fields: list[str] = []
    for key in ("planning_run_id", "conversation_id", "assistant_message_id"):
        value = state.get(key)  # type: ignore[literal-required]
        if value:
            fields.append(f"{key}={safe_log_value(value)}")
    return " ".join(fields)


def _completion_metrics(
    node: str,
    state: TripPlanningState,
    result: dict[str, Any],
) -> str:
    if node == "extract_requirements":
        method = safe_log_value(result.get("extraction_method", "unknown"))
        return f" extraction_method={method}"
    if node == "resolve_capabilities":
        plan = result.get("capability_plan")
        if plan is not None:
            modes = ",".join(mode.value for mode in plan.transport.modes) or "none"
            derivations = (
                ",".join(safe_log_value(item.field) for item in plan.derivations) or "none"
            )
            return (
                f" transport_enabled={str(plan.transport.enabled).lower()}"
                f" hotel_enabled={str(plan.hotel.enabled).lower()}"
                f" transport_modes={modes} derivation_fields={derivations}"
            )
    if node == "validate_requirements":
        check = result.get("requirement_check")
        if check is not None:
            missing = ",".join(safe_log_value(item.field) for item in check.missing) or "none"
            return f" complete={str(check.complete).lower()} missing_fields={missing}"
    if node == "collect_map_weather":
        bundle = result.get("map_weather_evidence")
        if bundle is not None:
            map_evidence = bundle.map
            weather = bundle.weather
            day_count = len(map_evidence.days) if map_evidence is not None else 0
            poi_count = (
                sum(len(day.ordered_places()) for day in map_evidence.days)
                if map_evidence is not None
                else 0
            )
            route_count = (
                sum(len(day.route_legs) for day in map_evidence.days)
                if map_evidence is not None
                else 0
            )
            weather_coverage = (
                sum(day.coverage == "available" for day in weather.days)
                if weather is not None
                else 0
            )
            return (
                f" evidence_status={bundle.status} day_count={day_count}"
                f" poi_count={poi_count} route_leg_count={route_count}"
                f" weather_coverage_count={weather_coverage}"
            )
    if node in {"collect_transport", "collect_hotels"}:
        key = "transport_evidence" if node == "collect_transport" else "hotel_evidence"
        evidence = result.get(key)
        if evidence is not None:
            queries = ",".join(sorted(safe_log_value(key) for key in evidence.query))
            flyai_success = evidence.status in {
                EvidenceStatus.USABLE,
                EvidenceStatus.EMPTY,
            }
            return (
                f" evidence_status={evidence.status.value}"
                f" flyai_success={str(flyai_success).lower()}"
                f" data_empty={str(evidence.status is EvidenceStatus.EMPTY).lower()}"
                f" query_fields={queries or 'none'} provider_duration_ms={evidence.duration_ms}"
            )
    if node == "join_evidence":
        evidence = result.get("joined_evidence")
        if evidence is not None:
            return (
                f" map_weather_status={evidence.map_weather.status}"
                f" transport_status={evidence.transport.status.value}"
                f" hotel_status={evidence.hotel.status.value}"
                f" overall_status={evidence.overall_status}"
            )
    if node == "generate_itinerary":
        narrative = result.get("narrative")
        if narrative is not None:
            if hasattr(narrative, "model_dump_json"):
                output_chars = len(narrative.model_dump_json())
            else:
                output_chars = len(str(narrative))
            return (
                f" model_call_count={state.get('revision_count', 0) + 1}"
                f" output_chars={output_chars}"
            )
    if node == "validate_itinerary":
        issues = result.get("validation_issues", [])
        codes = ",".join(safe_log_value(issue.code) for issue in issues) or "none"
        return (
            f" validation_count={state.get('revision_count', 0) + 1}"
            f" issue_count={len(issues)} issue_codes={codes}"
        )
    if node == "render_response":
        return f" output_chars={len(result.get('final_answer', ''))}"
    return ""


def safe_log_value(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9_.:,/-]", "_", str(value))[:160] or "none"


def _duration_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1_000))


__all__ = ["logged_trip_planner_node", "safe_log_value"]
