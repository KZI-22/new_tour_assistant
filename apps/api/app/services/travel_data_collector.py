from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime
from difflib import SequenceMatcher
from typing import Any
from zoneinfo import ZoneInfo

from app.schemas.itinerary import (
    Activity,
    AffectedSection,
    HotelOption,
    ItineraryPlan,
    TransportOption,
    TripRequest,
)
from app.schemas.tool_execution import ChatStreamEvent, PlanningStageEvent, ToolResultEvent
from app.services.flyai_transport_adapter import (
    FlyAITransportNormalization,
    normalize_flyai_transport,
)
from app.services.tool_execution import ToolExecutionContext, ToolExecutionOutcome, ToolExecutor

EventWriter = Callable[[ChatStreamEvent], None]

_STAGE_LABELS = {
    "collecting_transport": "正在查询交通方案",
    "collecting_hotels": "正在筛选住宿",
    "collecting_pois": "正在查询景点",
    "collecting_weather": "正在查询天气",
    "calculating_routes": "正在计算地点间路线与耗时",
}


class TravelDataCollector:
    def __init__(
        self,
        tool_executor: ToolExecutor,
        *,
        max_poi_candidates: int,
        max_transport_options: int = 16,
        max_hotel_options: int = 10,
        max_hotel_geocodes: int = 3,
        result_max_length: int,
        timezone: str = "Asia/Shanghai",
    ) -> None:
        self._executor = tool_executor
        self._max_poi_candidates = max_poi_candidates
        self._max_transport_options = max_transport_options
        self._max_hotel_options = max_hotel_options
        self._max_hotel_geocodes = max_hotel_geocodes
        self._result_max_length = result_max_length
        self._timezone = timezone

    async def collect(
        self,
        request: TripRequest,
        affected_sections: Sequence[AffectedSection],
        *,
        execution_context: ToolExecutionContext,
        writer: EventWriter,
    ) -> dict[str, Any]:
        sections = set(affected_sections) or {
            "dates",
            "destination",
            "transport",
            "hotel",
            "activities",
            "weather",
            "routes",
            "budget",
        }
        calls_by_stage = self._initial_calls(request, sections)
        active_stages = [stage for stage, calls in calls_by_stage.items() if calls]
        for stage in active_stages:
            _stage(writer, stage, "running")

        flattened = [call for calls in calls_by_stage.values() for call in calls]
        outcomes: list[ToolExecutionOutcome] = []
        prepared = []
        if flattened:
            prepared = self._executor.prepare_calls(flattened, round_index=0)
            for call in prepared:
                writer(call.event)
            outcomes = await self._executor.execute_many(prepared, context=execution_context)
            for outcome in outcomes:
                writer(outcome.event)

        stage_results: dict[str, list[tuple[Any, ToolExecutionOutcome]]] = {
            stage: [] for stage in calls_by_stage
        }
        offset = 0
        for stage, calls in calls_by_stage.items():
            count = len(calls)
            stage_results[stage] = list(zip(calls, outcomes[offset : offset + count], strict=True))
            offset += count
            if not calls and stage in {
                "collecting_transport",
                "collecting_hotels",
                "collecting_pois",
                "collecting_weather",
            }:
                _stage(writer, stage, "skipped")

        normalized = self._normalize_initial(stage_results, request)
        for quality_event in normalized.pop("quality_events"):
            writer(quality_event)
            await self._executor.record_data_quality(quality_event, execution_context)
        geocoded = await self._geocode_hotels(
            normalized["hotel_results"],
            request,
            execution_context=execution_context,
            writer=writer,
        )
        normalized["hotel_results"] = geocoded["hotels"]
        normalized["tool_evidence"].extend(geocoded["evidence"])
        normalized["tool_failures"].extend(geocoded["failures"])
        for stage, diagnostic in normalized["collection_diagnostics"].items():
            _stage(
                writer,
                stage,
                str(diagnostic["status"]),
                detail=str(diagnostic["detail"]),
            )
        return normalized

    def _initial_calls(
        self,
        request: TripRequest,
        sections: set[str],
    ) -> dict[str, list[dict[str, Any]]]:
        destination = request.destinations[0]
        calls: dict[str, list[dict[str, Any]]] = {
            "collecting_transport": [],
            "collecting_hotels": [],
            "collecting_pois": [],
            "collecting_weather": [],
        }
        if (
            ("transport" in sections or "dates" in sections or "destination" in sections)
            and request.origin
            and request.start_date
            and request.end_date
        ):
            preferences = " ".join(request.transport_preferences).casefold()
            allow_flight = not preferences or any(
                marker in preferences for marker in ("flight", "plane", "飞机", "航班")
            )
            allow_train = not preferences or any(
                marker in preferences for marker in ("train", "rail", "火车", "高铁")
            )
            if allow_flight:
                calls["collecting_transport"].extend(
                    self._transport_calls("search_flight", request, destination)
                )
            if allow_train:
                calls["collecting_transport"].extend(
                    self._transport_calls("search_train", request, destination)
                )

        if (
            (
                "hotel" in sections
                or "dates" in sections
                or "destination" in sections
                or "budget" in sections
            )
            and request.start_date
            and request.end_date
            and "search_hotel" in self._executor.tool_names
        ):
            arguments: dict[str, Any] = {
                "destination": destination,
                "check_in_date": request.start_date.isoformat(),
                "check_out_date": request.end_date.isoformat(),
            }
            if request.hotel_budget_per_night is not None:
                arguments["max_price"] = request.hotel_budget_per_night
            nearby = request.hotel_preferences.get("nearby_poi")
            if isinstance(nearby, str) and nearby.strip():
                arguments["nearby_poi"] = nearby.strip()
            calls["collecting_hotels"].append(_tool_call("search_hotel", arguments))

        if "activities" in sections or "destination" in sections:
            default_queries = ["热门景点", "历史文化景点", "城市地标"]
            queries = list(
                dict.fromkeys([*request.must_visit, *request.interests, *default_queries])
            )[:3]
            per_query = max(1, self._max_poi_candidates // max(1, len(queries)))
            for query in queries:
                if "search_poi" in self._executor.tool_names:
                    calls["collecting_pois"].append(
                        _tool_call("search_poi", {"city": destination, "keyword": query})
                    )
                if "amap_search_places" in self._executor.tool_names:
                    calls["collecting_pois"].append(
                        _tool_call(
                            "amap_search_places",
                            {"keywords": query, "city": destination, "limit": min(25, per_query)},
                        )
                    )

        if (
            "weather" in sections or "dates" in sections or "destination" in sections
        ) and "amap_get_weather" in self._executor.tool_names:
            calls["collecting_weather"].append(
                _tool_call("amap_get_weather", {"city": destination, "forecast": True})
            )
        return calls

    def _transport_calls(
        self,
        tool_name: str,
        request: TripRequest,
        destination: str,
    ) -> list[dict[str, Any]]:
        if tool_name not in self._executor.tool_names:
            return []
        assert request.origin and request.start_date and request.end_date
        outbound = _tool_call(
            tool_name,
            {
                "origin": request.origin,
                "destination": destination,
                "departure_date": request.start_date.isoformat(),
                "sort_type": 2,
            },
        )
        returning = _tool_call(
            tool_name,
            {
                "origin": destination,
                "destination": request.origin,
                "departure_date": request.end_date.isoformat(),
                "sort_type": 2,
            },
        )
        return [outbound, returning]

    def _normalize_initial(
        self,
        stage_results: Mapping[str, list[tuple[Any, ToolExecutionOutcome]]],
        request: TripRequest,
    ) -> dict[str, Any]:
        transport_results: list[TransportOption] = []
        hotel_results: list[HotelOption] = []
        poi_results: list[dict[str, Any]] = []
        weather_results: list[dict[str, Any]] = []
        evidence: list[dict[str, Any]] = []
        failures: list[str] = []
        diagnostics: dict[str, dict[str, Any]] = {}
        quality_events: list[ToolResultEvent] = []
        transport_call_count = max(1, len(stage_results.get("collecting_transport", [])))
        transport_per_call = max(1, self._max_transport_options // transport_call_count)

        for stage, pairs in stage_results.items():
            for call, outcome in pairs:
                if not outcome.result.success:
                    if outcome.result.error:
                        failures.append(f"{call['name']}: {outcome.result.error.message}")
                    continue
                item = _evidence(call, outcome, self._result_max_length)
                evidence.append(item)
                if stage == "collecting_transport":
                    transport_normalization = _transport_normalization(
                        call,
                        outcome,
                        request,
                        timezone=self._timezone,
                    )
                    transport_results.extend(
                        transport_normalization.options[:transport_per_call]
                    )
                    quality_events.append(
                        _quality_event(
                            outcome,
                            provider_item_count=transport_normalization.provider_item_count,
                            normalized_item_count=len(transport_normalization.options),
                            rejected_item_count=transport_normalization.rejected_count,
                            schema_version=transport_normalization.schema_version,
                        )
                    )
                elif stage == "collecting_hotels":
                    options = _hotel_options(call, outcome, request)
                    hotel_results.extend(options[: self._max_hotel_options])
                    provider_count = _payload_item_count(outcome.result.data)
                    quality_events.append(
                        _quality_event(
                            outcome,
                            provider_item_count=provider_count,
                            normalized_item_count=len(options),
                            rejected_item_count=max(0, provider_count - len(options)),
                            schema_version="hotel-generic-v1",
                        )
                    )
                elif stage == "collecting_pois":
                    arguments = call.get("args") if isinstance(call.get("args"), Mapping) else {}
                    query = _text(arguments, "keyword", "keywords")
                    parsed_items = _poi_items(
                        outcome.result.data,
                        query=query,
                        source_tool=str(call.get("name") or "unknown"),
                    )
                    items = _relevant_pois(parsed_items, request)
                    poi_results.extend(items)
                    provider_count = _payload_item_count(outcome.result.data)
                    parse_failure_count = max(0, provider_count - len(parsed_items))
                    relevance_filtered_count = max(0, len(parsed_items) - len(items))
                    quality_events.append(
                        _quality_event(
                            outcome,
                            provider_item_count=provider_count,
                            normalized_item_count=len(items),
                            rejected_item_count=max(0, provider_count - len(items)),
                            schema_version="poi-generic-v1",
                            invalid_error_code=(
                                "NO_RELEVANT_RESULTS"
                                if parsed_items and not items
                                else "PROVIDER_SCHEMA_MISMATCH"
                            ),
                            partial_summary=_poi_quality_summary(
                                provider_count=provider_count,
                                normalized_item_count=len(items),
                                parse_failure_count=parse_failure_count,
                                relevance_filtered_count=relevance_filtered_count,
                            ),
                            invalid_summary=_poi_quality_summary(
                                provider_count=provider_count,
                                normalized_item_count=len(items),
                                parse_failure_count=parse_failure_count,
                                relevance_filtered_count=relevance_filtered_count,
                            ),
                        )
                    )
                elif stage == "collecting_weather":
                    weather_results.append(item)
                    provider_count = _payload_item_count(outcome.result.data)
                    quality_events.append(
                        _quality_event(
                            outcome,
                            provider_item_count=provider_count,
                            normalized_item_count=int(provider_count > 0),
                            rejected_item_count=0,
                            schema_version="weather-generic-v1",
                        )
                    )

        normalized_transport = _unique_models(transport_results)[: self._max_transport_options]
        normalized_hotels = _unique_models(hotel_results)[: self._max_hotel_options]
        normalized_pois = _unique_pois(poi_results)[: self._max_poi_candidates]
        usable_counts = {
            "collecting_transport": len(normalized_transport),
            "collecting_hotels": len(normalized_hotels),
            "collecting_pois": len(normalized_pois),
            "collecting_weather": len(weather_results),
        }
        for stage, pairs in stage_results.items():
            if pairs:
                required_items = 1
                covered_items = int(usable_counts.get(stage, 0) > 0)
                if stage == "collecting_transport":
                    required_directions = {
                        (
                            str(call.get("args", {}).get("origin") or "").casefold(),
                            str(call.get("args", {}).get("destination") or "").casefold(),
                        )
                        for call, _ in pairs
                    }
                    covered_directions = {
                        (
                            item.departure_city.casefold(),
                            item.arrival_city.casefold(),
                        )
                        for item in normalized_transport
                    }
                    required_items = len(required_directions)
                    covered_items = len(required_directions & covered_directions)
                diagnostics[stage] = _collection_diagnostic(
                    pairs,
                    usable_items=usable_counts.get(stage, 0),
                    required_items=required_items,
                    covered_items=covered_items,
                )

        return {
            "transport_results": normalized_transport,
            "hotel_results": normalized_hotels,
            "poi_results": normalized_pois,
            "weather_results": weather_results,
            "route_results": [],
            "collection_diagnostics": diagnostics,
            "tool_evidence": evidence,
            "tool_failures": failures,
            "quality_events": quality_events,
        }

    async def _geocode_hotels(
        self,
        hotels: Sequence[HotelOption],
        request: TripRequest,
        *,
        execution_context: ToolExecutionContext,
        writer: EventWriter,
    ) -> dict[str, Any]:
        destination = request.destinations[0] if request.destinations else ""
        candidates = [
            (index, hotel)
            for index, hotel in enumerate(hotels)
            if not hotel.coordinates
        ][: self._max_hotel_geocodes]
        if (
            not candidates
            or not destination
            or "amap_search_places" not in self._executor.tool_names
        ):
            return {"hotels": list(hotels), "evidence": [], "failures": []}

        calls = [
            _tool_call(
                "amap_search_places",
                {
                    "keywords": " ".join(
                        value for value in (hotel.name, hotel.address or "") if value
                    ),
                    "city": destination,
                    "limit": 5,
                },
            )
            for _, hotel in candidates
        ]
        prepared = self._executor.prepare_calls(calls, round_index=1)
        for call in prepared:
            writer(call.event)
        outcomes = await self._executor.execute_many(prepared, context=execution_context)

        resolved = list(hotels)
        evidence: list[dict[str, Any]] = []
        failures: list[str] = []
        for (hotel_index, hotel), call, outcome in zip(
            candidates, calls, outcomes, strict=True
        ):
            writer(outcome.event)
            if not outcome.result.success:
                if outcome.result.error:
                    failures.append(
                        f"酒店坐标匹配 {hotel.name}: {outcome.result.error.message}"
                    )
                continue
            provider_count = _payload_item_count(outcome.result.data)
            places = _poi_items(
                outcome.result.data,
                query=hotel.name,
                source_tool="amap_search_places",
            )
            match = _match_hotel_place(hotel, places, destination)
            matched_count = int(match is not None)
            parse_failure_count = max(0, provider_count - len(places))
            candidate_unmatched_count = max(0, len(places) - matched_count)
            geocode_summary = _hotel_geocode_quality_summary(
                provider_count=provider_count,
                matched_count=matched_count,
                parse_failure_count=parse_failure_count,
                candidate_unmatched_count=candidate_unmatched_count,
            )
            quality_event = _quality_event(
                outcome,
                provider_item_count=provider_count,
                normalized_item_count=matched_count,
                rejected_item_count=max(0, provider_count - matched_count),
                schema_version="hotel-geocode-v1",
                invalid_error_code="HOTEL_COORDINATE_MATCH_REJECTED",
                partial_summary=geocode_summary,
                invalid_summary=geocode_summary,
            )
            writer(quality_event)
            await self._executor.record_data_quality(quality_event, execution_context)
            if match is None:
                continue
            place, confidence = match
            location = place["location"]
            resolved[hotel_index] = hotel.model_copy(
                update={
                    "poi_id": hotel.poi_id or place.get("poi_id"),
                    "coordinates": json.dumps(location, ensure_ascii=False),
                    "coordinate_source": "amap_search_places",
                    "coordinate_source_reference": (
                        f"amap_search_places:{place.get('poi_id') or place.get('name')}"
                    ),
                    "coordinate_queried_at": outcome.result.metadata.queried_at,
                    "coordinate_match_confidence": confidence,
                }
            )
            evidence.append(_evidence(call, outcome, self._result_max_length))
        return {"hotels": resolved, "evidence": evidence, "failures": failures}

    async def collect_itinerary_routes(
        self,
        plan: ItineraryPlan,
        request: TripRequest,
        *,
        execution_context: ToolExecutionContext,
        writer: EventWriter,
    ) -> dict[str, Any]:
        required_pairs = _itinerary_route_pairs(plan)
        calls: list[dict[str, Any]] = []
        if "amap_plan_route" in self._executor.tool_names:
            for pair in required_pairs:
                if pair["origin"] is None or pair["destination"] is None:
                    continue
                route_call = _tool_call(
                    "amap_plan_route",
                    {
                        "origin": pair["origin"],
                        "destination": pair["destination"],
                        "mode": "driving",
                        "city": request.destinations[0],
                    },
                )
                route_call["route_pair"] = {
                    "origin_id": pair["origin_id"],
                    "destination_id": pair["destination_id"],
                }
                calls.append(route_call)

        required_items = len(required_pairs)
        if required_items == 0:
            _stage(writer, "calculating_routes", "skipped")
            return {
                "plan": _apply_route_durations(plan, []),
                "evidence": [],
                "failures": [],
                "diagnostic": {
                    "status": "skipped",
                    "call_count": 0,
                    "successful_calls": 0,
                    "usable_items": 0,
                    "required_items": 0,
                    "detail": "行程中没有需要计算的相邻活动路段。",
                },
            }
        if not calls:
            _stage(
                writer,
                "calculating_routes",
                "failed",
                detail=f"{required_items} 个相邻活动路段缺少坐标或路线工具不可用。",
            )
            return {
                "plan": _apply_route_durations(plan, []),
                "evidence": [],
                "failures": [],
                "diagnostic": {
                    "status": "failed",
                    "call_count": 0,
                    "successful_calls": 0,
                    "usable_items": 0,
                    "required_items": required_items,
                    "detail": f"{required_items} 个相邻活动路段缺少坐标或路线工具不可用。",
                },
            }

        _stage(writer, "calculating_routes", "running")
        prepared = self._executor.prepare_calls(calls, round_index=2)
        for call in prepared:
            writer(call.event)
        outcomes = await self._executor.execute_many(prepared, context=execution_context)
        evidence: list[dict[str, Any]] = []
        failures: list[str] = []
        all_legs: list[dict[str, Any]] = []
        for call, outcome in zip(calls, outcomes, strict=True):
            writer(outcome.event)
            if outcome.result.success:
                legs = _route_legs(call, outcome)
                provider_count = _payload_item_count(outcome.result.data)
                quality_event = _quality_event(
                    outcome,
                    provider_item_count=provider_count,
                    normalized_item_count=len(legs),
                    rejected_item_count=max(0, provider_count - len(legs)),
                    schema_version="route-generic-v1",
                )
                writer(quality_event)
                await self._executor.record_data_quality(quality_event, execution_context)
                if legs:
                    item = _evidence(call, outcome, self._result_max_length)
                    item["route_legs"] = legs
                    evidence.append(item)
                    all_legs.extend(legs)
            elif outcome.result.error:
                failures.append(f"{call['name']}: {outcome.result.error.message}")
        unique_legs = _unique_route_legs(all_legs)
        successful_calls = sum(outcome.result.success for outcome in outcomes)
        if not unique_legs:
            status = "failed"
        elif len(unique_legs) < required_items or successful_calls < len(calls):
            status = "partial"
        else:
            status = "success"
        detail = (
            f"{successful_calls}/{len(calls)} 个路线调用成功，"
            f"取得 {len(unique_legs)}/{required_items} 个最低所需路段。"
        )
        _stage(writer, "calculating_routes", status, detail=detail)
        return {
            "plan": _apply_route_durations(plan, unique_legs),
            "evidence": evidence,
            "failures": failures,
            "diagnostic": {
                "status": status,
                "call_count": len(calls),
                "successful_calls": successful_calls,
                "usable_items": len(unique_legs),
                "required_items": required_items,
                "detail": detail,
            },
        }


def _tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"id": f"planner-{uuid.uuid4().hex}", "name": name, "args": arguments}


def _stage(
    writer: EventWriter,
    stage: str,
    status: str,
    *,
    detail: str | None = None,
) -> None:
    writer(
        PlanningStageEvent(
            stage=stage,
            display_name=_STAGE_LABELS[stage],
            status=status,
            detail=detail,
        )
    )


def _quality_event(
    outcome: ToolExecutionOutcome,
    *,
    provider_item_count: int,
    normalized_item_count: int,
    rejected_item_count: int,
    schema_version: str,
    invalid_error_code: str = "PROVIDER_SCHEMA_MISMATCH",
    partial_summary: str | None = None,
    invalid_summary: str | None = None,
) -> ToolResultEvent:
    if normalized_item_count > 0:
        data_status = "partial" if rejected_item_count > 0 else "usable"
        success = True
        error_code = None
        if data_status == "partial":
            summary = partial_summary or (
                f"供应商返回 {provider_item_count} 条记录，成功解析 "
                f"{normalized_item_count} 条，解析失败 {rejected_item_count} 条。"
            )
        else:
            summary = f"已取得 {normalized_item_count} 条可用数据。"
    elif provider_item_count <= 0:
        data_status = "empty"
        success = False
        error_code = "NO_RESULTS"
        summary = "查询已完成，但没有匹配结果。"
    else:
        data_status = "invalid"
        success = False
        error_code = invalid_error_code
        summary = invalid_summary or (
            f"供应商返回 {provider_item_count} 条记录，但均解析失败，当前版本无法使用。"
        )
    return ToolResultEvent(
        tool_call_id=outcome.event.tool_call_id,
        tool_name=outcome.event.tool_name,
        success=success,
        summary=summary,
        duration_ms=outcome.event.duration_ms,
        error_code=error_code,
        data_status=data_status,
        provider_item_count=max(0, provider_item_count),
        normalized_item_count=max(0, normalized_item_count),
        rejected_item_count=max(0, rejected_item_count),
        schema_version=schema_version,
    )


def _poi_quality_summary(
    *,
    provider_count: int,
    normalized_item_count: int,
    parse_failure_count: int,
    relevance_filtered_count: int,
) -> str:
    if normalized_item_count > 0:
        parts = [f"供应商返回 {provider_count} 条地点"]
        if parse_failure_count:
            parts.append(f"解析失败 {parse_failure_count} 条")
        if relevance_filtered_count:
            parts.append(f"相关性过滤 {relevance_filtered_count} 条")
        parts.append(f"保留 {normalized_item_count} 条旅游候选")
        return "，".join(parts) + "。"
    if relevance_filtered_count:
        prefix = (
            f"供应商返回 {provider_count} 条地点，解析失败 {parse_failure_count} 条，"
            if parse_failure_count
            else f"供应商返回 {provider_count} 条地点，"
        )
        return f"{prefix}相关性过滤 {relevance_filtered_count} 条，没有保留旅游候选。"
    return f"供应商返回 {provider_count} 条记录，但 {parse_failure_count} 条均解析失败。"


def _hotel_geocode_quality_summary(
    *,
    provider_count: int,
    matched_count: int,
    parse_failure_count: int,
    candidate_unmatched_count: int,
) -> str:
    parts = [f"供应商返回 {provider_count} 个地点候选"]
    if parse_failure_count:
        parts.append(f"解析失败 {parse_failure_count} 个")
    if candidate_unmatched_count:
        parts.append(f"候选未匹配 {candidate_unmatched_count} 个")
    if matched_count:
        parts.append(f"匹配酒店坐标 {matched_count} 个")
    else:
        parts.append("未找到可确认的酒店坐标")
    return "，".join(parts) + "。"


def _payload_item_count(data: Any, *, _depth: int = 0) -> int:
    """Estimate provider records without retaining or exposing the raw payload."""

    if isinstance(data, list):
        return len(data)
    if not isinstance(data, Mapping):
        return int(data is not None)
    if not data:
        return 0
    for key in (
        "itemList",
        "items",
        "results",
        "hotels",
        "pois",
        "lives",
        "forecasts",
        "routes",
        "paths",
    ):
        value = data.get(key)
        if isinstance(value, list):
            return len(value)
    if "data" in data and data.get("data") is None:
        return 0
    if _depth < 3:
        nested = data.get("data")
        if isinstance(nested, (Mapping, list)):
            return _payload_item_count(nested, _depth=_depth + 1)
    return 1


def _collection_diagnostic(
    pairs: Sequence[tuple[Any, ToolExecutionOutcome]],
    *,
    usable_items: int,
    required_items: int,
    covered_items: int,
) -> dict[str, Any]:
    successful_calls = sum(outcome.result.success for _, outcome in pairs)
    call_count = len(pairs)
    if usable_items <= 0 or covered_items <= 0:
        status = "failed"
    elif successful_calls < call_count or covered_items < required_items:
        status = "partial"
    else:
        status = "success"
    detail = (
        f"{successful_calls}/{call_count} 个工具调用成功，"
        f"标准化得到 {usable_items} 条可用记录，"
        f"覆盖 {covered_items}/{required_items} 个必要查询方向。"
    )
    return {
        "status": status,
        "call_count": call_count,
        "successful_calls": successful_calls,
        "usable_items": usable_items,
        "required_items": required_items,
        "covered_items": covered_items,
        "detail": detail,
    }


def _evidence(
    call: Mapping[str, Any],
    outcome: ToolExecutionOutcome,
    max_length: int,
) -> dict[str, Any]:
    serialized = json.dumps(outcome.result.data, ensure_ascii=False, default=str)
    if len(serialized) > max_length:
        serialized = serialized[:max_length] + "…[truncated]"
    return {
        "tool_name": outcome.result.tool_name,
        "provider": outcome.result.metadata.provider,
        "queried_at": outcome.result.metadata.queried_at.isoformat(),
        "arguments": call.get("args", {}),
        "data": serialized,
    }


def _route_legs(
    call: Mapping[str, Any],
    outcome: ToolExecutionOutcome,
) -> list[dict[str, Any]]:
    tool_name = str(call.get("name") or "")
    legs: list[dict[str, Any]] = []
    if tool_name == "amap_travel_time_matrix":
        for mapping in _walk_mappings(outcome.result.data):
            origin_id = _text(mapping, "origin_id", "originId")
            destination_id = _text(mapping, "destination_id", "destinationId")
            duration_seconds = _number(_first(mapping, "duration_seconds", "duration"))
            if (
                not origin_id
                or not destination_id
                or mapping.get("success") is not True
                or duration_seconds is None
            ):
                continue
            legs.append(
                {
                    "origin_id": origin_id,
                    "destination_id": destination_id,
                    "duration_minutes": math.ceil(duration_seconds / 60),
                    "distance_meters": _number(
                        _first(mapping, "distance_meters", "distance")
                    ),
                    "source_tool": tool_name,
                }
            )
    elif tool_name == "amap_plan_route":
        route_pair = call.get("route_pair")
        if isinstance(route_pair, Mapping):
            for mapping in _walk_mappings(outcome.result.data):
                duration_seconds = _number(
                    _first(mapping, "duration_seconds", "duration")
                )
                if duration_seconds is None:
                    continue
                origin_id = _text(route_pair, "origin_id")
                destination_id = _text(route_pair, "destination_id")
                if origin_id and destination_id:
                    legs.append(
                        {
                            "origin_id": origin_id,
                            "destination_id": destination_id,
                            "duration_minutes": math.ceil(duration_seconds / 60),
                            "distance_meters": _number(
                                _first(mapping, "distance_meters", "distance")
                            ),
                            "source_tool": tool_name,
                        }
                    )
                break
    return _unique_route_legs(legs)


def _itinerary_route_pairs(plan: ItineraryPlan) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for day in plan.days:
        for left, right in zip(day.activities, day.activities[1:], strict=False):
            origin_id = left.poi_id or ""
            destination_id = right.poi_id or ""
            key = (origin_id, destination_id)
            if origin_id and destination_id and key in seen:
                continue
            if origin_id and destination_id:
                seen.add(key)
            pairs.append(
                {
                    "origin_id": origin_id,
                    "destination_id": destination_id,
                    "origin": _activity_route_coordinate(left),
                    "destination": _activity_route_coordinate(right),
                }
            )
    return pairs


def _activity_route_coordinate(activity: Activity) -> dict[str, Any] | None:
    if not activity.poi_id or not activity.coordinates:
        return None
    try:
        raw = json.loads(activity.coordinates)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, Mapping):
        return None
    longitude = _number(raw.get("longitude"))
    latitude = _number(raw.get("latitude"))
    if longitude is None or latitude is None:
        return None
    return {
        "longitude": longitude,
        "latitude": latitude,
        "coordinate_system": "GCJ02",
    }


def _apply_route_durations(
    plan: ItineraryPlan,
    legs: Sequence[Mapping[str, Any]],
) -> ItineraryPlan:
    durations = {
        (str(leg.get("origin_id") or ""), str(leg.get("destination_id") or "")): leg.get(
            "duration_minutes"
        )
        for leg in legs
        if isinstance(leg.get("duration_minutes"), int)
    }
    updated = plan.model_copy(deep=True)
    for day in updated.days:
        if len(day.activities) <= 1:
            day.estimated_transport_time_minutes = 0
            continue
        day_durations: list[int] = []
        for left, right in zip(day.activities, day.activities[1:], strict=False):
            key = (left.poi_id or "", right.poi_id or "")
            duration = durations.get(key)
            if not key[0] or not key[1] or not isinstance(duration, int):
                day_durations = []
                break
            day_durations.append(duration)
        day.estimated_transport_time_minutes = sum(day_durations) if day_durations else None
    return updated


def _unique_route_legs(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (str(item.get("origin_id") or ""), str(item.get("destination_id") or ""))
        if all(key) and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _transport_options(
    call: Mapping[str, Any],
    outcome: ToolExecutionOutcome,
    request: TripRequest,
    *,
    timezone: str,
) -> list[TransportOption]:
    return _transport_normalization(
        call,
        outcome,
        request,
        timezone=timezone,
    ).options


def _transport_normalization(
    call: Mapping[str, Any],
    outcome: ToolExecutionOutcome,
    request: TripRequest,
    *,
    timezone: str,
) -> FlyAITransportNormalization:
    del request
    arguments = call.get("args") if isinstance(call.get("args"), Mapping) else {}
    tool_name = str(call.get("name"))
    transport_type = "flight" if tool_name == "search_flight" else "train"
    flyai = normalize_flyai_transport(
        outcome.result.data,
        transport_type=transport_type,
        source_tool=tool_name,
        provider=outcome.result.metadata.provider,
        queried_at=outcome.result.metadata.queried_at,
        arguments=arguments,
        timezone=timezone,
    )
    if flyai.recognized:
        return flyai

    options: list[TransportOption] = []
    for mapping in _walk_mappings(outcome.result.data):
        flight_number = _text(
            mapping,
            "flight_number",
            "flightNo",
            "flight_no",
            "flightCode",
            "flight_code",
            "transportNo",
            "transport_no",
            "航班号",
            "航班",
        )
        train_number = _text(
            mapping,
            "train_number",
            "trainNo",
            "train_no",
            "trainCode",
            "train_code",
            "transportNo",
            "transport_no",
            "车次号",
            "车次",
        )
        if transport_type == "flight" and not flight_number:
            continue
        if transport_type == "train" and not train_number:
            continue
        price = _number(_first(mapping, "price", "ticket_price", "lowestPrice", "票价", "价格"))
        reference = flight_number or train_number or _reference(mapping)
        try:
            options.append(
                TransportOption(
                    transport_type=transport_type,
                    provider=outcome.result.metadata.provider,
                    timezone=timezone,
                    departure_city=_text(mapping, "departure_city", "origin", "from", "出发城市")
                    or str(arguments.get("origin") or "未知"),
                    arrival_city=_text(mapping, "arrival_city", "destination", "to", "到达城市")
                    or str(arguments.get("destination") or "未知"),
                    departure_time=_datetime(
                        _first(
                            mapping,
                            "departure_time",
                            "departureTime",
                            "depart_time",
                            "departTime",
                            "出发时间",
                        ),
                        arguments.get("departure_date"),
                        timezone=timezone,
                    ),
                    arrival_time=_datetime(
                        _first(
                            mapping,
                            "arrival_time",
                            "arrivalTime",
                            "arrive_time",
                            "arriveTime",
                            "到达时间",
                        ),
                        arguments.get("departure_date"),
                        timezone=timezone,
                    ),
                    flight_number=flight_number,
                    train_number=train_number,
                    origin_station=_text(
                        mapping, "origin_station", "departure_station", "departureAirport", "出发站"
                    ),
                    destination_station=_text(
                        mapping,
                        "destination_station",
                        "arrival_station",
                        "arrivalAirport",
                        "到达站",
                    ),
                    price=price,
                    seat_or_cabin=_text(mapping, "seat", "cabin", "seat_class", "席别", "舱位"),
                    duration_minutes=_duration_minutes(mapping),
                    source_tool=tool_name,
                    source_reference=f"{tool_name}:{reference}",
                    queried_at=outcome.result.metadata.queried_at,
                )
            )
        except ValueError:
            continue
    provider_count = _payload_item_count(outcome.result.data)
    return FlyAITransportNormalization(
        recognized=False,
        options=options,
        provider_item_count=provider_count,
        journey_count=len(options),
        rejected_count=max(0, provider_count - len(options)),
        schema_version="transport-generic-v1",
    )


def _hotel_options(
    call: Mapping[str, Any],
    outcome: ToolExecutionOutcome,
    request: TripRequest,
) -> list[HotelOption]:
    arguments = call.get("args") if isinstance(call.get("args"), Mapping) else {}
    check_in = _date(arguments.get("check_in_date")) or request.start_date
    check_out = _date(arguments.get("check_out_date")) or request.end_date
    if check_in is None or check_out is None:
        return []
    options: list[HotelOption] = []
    for mapping in _walk_mappings(outcome.result.data):
        name = _text(mapping, "hotel_name", "hotelName", "name", "酒店名称")
        if not name:
            continue
        price = _number(
            _first(mapping, "nightly_price", "price", "lowestPrice", "每晚价格", "价格")
        )
        poi_id = _text(mapping, "poi_id", "poiId", "id")
        location = _first(mapping, "coordinates", "location", "经纬度")
        try:
            options.append(
                HotelOption(
                    name=name,
                    address=_text(mapping, "address", "formatted_address", "地址"),
                    poi_id=poi_id,
                    coordinates=json.dumps(location, ensure_ascii=False)
                    if isinstance(location, Mapping)
                    else (str(location) if location else None),
                    star_level=_text(mapping, "star_level", "star", "rating", "星级"),
                    room_type=_text(mapping, "room_type", "roomType", "房型"),
                    bed_type=_text(mapping, "bed_type", "bedType", "床型"),
                    nightly_price=price,
                    total_price=_number(_first(mapping, "total_price", "totalPrice", "总价")),
                    check_in_date=check_in,
                    check_out_date=check_out,
                    source_tool=str(call.get("name")),
                    source_reference=f"search_hotel:{poi_id or _reference(mapping)}",
                    queried_at=outcome.result.metadata.queried_at,
                )
            )
        except ValueError:
            continue
    return options


def _poi_items(
    data: Any,
    *,
    query: str | None = None,
    source_tool: str | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for mapping in _walk_mappings(data):
        poi_id = _text(mapping, "poi_id", "poiId", "id")
        name = _text(mapping, "name", "poi_name", "title", "景点名称")
        raw_location = _first(mapping, "location", "coordinates", "coordinate")
        location: dict[str, Any] | None = None
        if isinstance(raw_location, Mapping):
            longitude = _number(_first(raw_location, "longitude", "lng", "lon"))
            latitude = _number(_first(raw_location, "latitude", "lat"))
            if longitude is not None and latitude is not None:
                location = {
                    "longitude": longitude,
                    "latitude": latitude,
                    "coordinate_system": raw_location.get("coordinate_system", "GCJ02"),
                }
        elif isinstance(raw_location, str) and "," in raw_location:
            left, right = raw_location.split(",", 1)
            longitude, latitude = _number(left), _number(right)
            if longitude is not None and latitude is not None:
                location = {
                    "longitude": longitude,
                    "latitude": latitude,
                    "coordinate_system": "GCJ02",
                }
        if not name or (not poi_id and location is None):
            continue
        items.append(
            {
                "poi_id": poi_id,
                "name": name,
                "address": _text(mapping, "address", "formatted_address", "地址"),
                "poi_type": _text(mapping, "poi_type", "category", "type", "类型"),
                "type_code": _text(mapping, "typecode", "type_code", "category_code"),
                "city": _text(
                    mapping,
                    "city",
                    "cityname",
                    "cityName",
                    "city_name",
                    "城市",
                ),
                "district": _text(
                    mapping,
                    "adname",
                    "district",
                    "districtName",
                    "行政区",
                ),
                "adcode": _text(mapping, "adcode", "ad_code"),
                "location": location,
                "query": query,
                "source_tool": source_tool,
                "provider_rank": len(items),
            }
        )
    return items


def _walk_mappings(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for nested in value.values():
            yield from _walk_mappings(nested)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for nested in value:
            yield from _walk_mappings(nested)


def _first(values: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in values and values[key] not in (None, ""):
            return values[key]
    return None


def _text(values: Mapping[str, Any], *keys: str) -> str | None:
    value = _first(values, *keys)
    if value is None or isinstance(value, (Mapping, list)):
        return None
    normalized = str(value).strip()
    return normalized or None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group()) if match else None


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _datetime(value: Any, fallback_date: Any, *, timezone: str) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("/", "-")
    try:
        parsed = datetime.fromisoformat(normalized)
        return parsed.replace(tzinfo=parsed.tzinfo or ZoneInfo(timezone))
    except ValueError:
        pass
    base_date = _date(fallback_date)
    if base_date and re.fullmatch(r"\d{1,2}:\d{2}", normalized):
        hour, minute = normalized.split(":")
        return datetime(
            base_date.year,
            base_date.month,
            base_date.day,
            int(hour),
            int(minute),
            tzinfo=ZoneInfo(timezone),
        )
    return None


def _duration_minutes(values: Mapping[str, Any]) -> int | None:
    direct = _number(_first(values, "duration_minutes", "durationMinutes", "耗时分钟"))
    if direct is not None:
        return max(0, round(direct))
    hours = _number(_first(values, "duration_hours", "duration", "耗时"))
    return max(0, round(hours * 60)) if hours is not None else None


def _reference(values: Mapping[str, Any]) -> str:
    serialized = json.dumps(values, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def _unique_models[T: TransportOption | HotelOption](items: Iterable[T]) -> list[T]:
    result: list[T] = []
    seen: set[str] = set()
    for item in items:
        key = item.source_reference or item.model_dump_json()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _unique_pois(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        key = str(item.get("poi_id") or item.get("name", "")).casefold()
        if key and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _relevant_pois(
    items: Sequence[dict[str, Any]],
    request: TripRequest,
) -> list[dict[str, Any]]:
    """Keep travel-relevant, destination-consistent POIs with explainable scores."""

    destination = request.destinations[0] if request.destinations else ""
    must_visit = [value.casefold() for value in request.must_visit]
    shopping_requested = any(
        marker in value.casefold()
        for value in request.interests
        for marker in ("购物", "商场", "市集", "shopping", "market")
    )
    commercial_markers = (
        "展销",
        "批发",
        "购物中心",
        "商场",
        "超市",
        "特产",
        "零售",
        "建材",
        "家居",
        "公司",
        "产业园",
        "写字楼",
        "市集",
    )
    excluded_place_markers = (
        "商务住宅",
        "住宅区",
        "住宅小区",
        "商务写字楼",
        "产业园",
        "公司企业",
        "公司",
    )
    tourism_markers = (
        "风景名胜",
        "旅游景点",
        "景区",
        "博物馆",
        "纪念馆",
        "遗址",
        "古迹",
        "古城",
        "古镇",
        "城墙",
        "寺",
        "塔",
        "故居",
        "公园",
        "湖",
        "山",
        "动物园",
        "植物园",
        "文化场馆",
        "museum",
        "historic",
        "attraction",
    )
    intent_markers = {
        "历史": ("历史", "博物馆", "遗址", "古迹", "古城", "城墙", "故居"),
        "文化": ("文化", "博物馆", "纪念馆", "寺", "古城", "故居"),
        "自然": ("自然", "风景", "景区", "公园", "湖", "山", "植物园"),
        "亲子": ("亲子", "动物园", "植物园", "乐园", "科技馆"),
        "美食": ("美食", "餐饮", "小吃", "老字号"),
    }
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for index, item in enumerate(items):
        city = str(item.get("city") or "")
        if city and destination and not _same_city(city, destination):
            continue
        name = str(item.get("name") or "")
        query = str(item.get("query") or "")
        text = " ".join(
            str(item.get(key) or "")
            for key in ("name", "poi_type", "type_code", "address", "query")
        ).casefold()
        classification_text = " ".join(
            str(item.get(key) or "") for key in ("name", "poi_type", "type_code")
        ).casefold()
        name_text = name.casefold()
        required = any(value in name_text or name_text in value for value in must_visit)
        excluded_place = any(marker in classification_text for marker in excluded_place_markers)
        if excluded_place and not required:
            continue
        commercial = any(marker in text for marker in commercial_markers)
        if commercial and not required and not shopping_requested:
            continue

        score = 0
        reasons: list[str] = []
        if city and destination:
            score += 10
            reasons.append("destination_city")
        if required:
            score += 100
            reasons.append("must_visit")
        query_name_match = bool(query and query.casefold() in name_text)
        if query_name_match:
            score += 30
            reasons.append("query_name_match")
        matched_intent = any(
            intent in query and any(marker in text for marker in markers)
            for intent, markers in intent_markers.items()
        )
        if matched_intent:
            score += 20
            reasons.append("query_type_match")
        tourism_signal = any(marker in text for marker in tourism_markers) or str(
            item.get("type_code") or ""
        ).startswith("11")
        if tourism_signal:
            score += 15
            reasons.append("tourism_type")
        if item.get("location") is not None:
            score += 5
            reasons.append("has_coordinates")
        if commercial:
            score -= 40
            reasons.append("commercial_penalty")
        if (
            not tourism_signal
            and not matched_intent
            and not required
            and not query_name_match
            and not (shopping_requested and commercial)
        ):
            continue
        if score < 10 and not required:
            continue
        candidate = dict(item)
        candidate["relevance_score"] = score
        candidate["relevance_reasons"] = reasons
        candidate["is_must_visit"] = required
        scored.append((score, index, candidate))
    return [item for _, _, item in sorted(scored, key=lambda value: (-value[0], value[1]))]


def _same_city(left: str, right: str) -> bool:
    def normalize(value: str) -> str:
        compact = re.sub(r"[\s·,，]", "", value).casefold()
        suffix = r"(?:特别行政区|壮族自治区|回族自治区|维吾尔自治区|自治区|省|市)$"
        return re.sub(suffix, "", compact)

    normalized_left = normalize(left)
    normalized_right = normalize(right)
    return bool(
        normalized_left
        and normalized_right
        and (
            normalized_left == normalized_right
            or normalized_left in normalized_right
            or normalized_right in normalized_left
        )
    )


def _match_hotel_place(
    hotel: HotelOption,
    places: Sequence[dict[str, Any]],
    destination: str,
) -> tuple[dict[str, Any], float] | None:
    """Return only high-confidence city/name matches suitable for route facts."""

    hotel_name = _normalize_place_text(hotel.name)
    matches: list[tuple[float, dict[str, Any]]] = []
    for place in places:
        location = place.get("location")
        city = str(place.get("city") or "")
        if not isinstance(location, Mapping) or not city or not _same_city(city, destination):
            continue
        poi_type = str(place.get("poi_type") or "")
        if poi_type and not any(
            marker in poi_type for marker in ("住宿服务", "宾馆酒店", "酒店", "宾馆", "旅馆")
        ):
            continue
        place_name = _normalize_place_text(str(place.get("name") or ""))
        if not hotel_name or not place_name:
            continue
        exact_name = hotel_name == place_name
        contained_name = (
            min(len(hotel_name), len(place_name)) >= 5
            and (hotel_name in place_name or place_name in hotel_name)
        )
        address_score = _address_match_score(hotel.address, str(place.get("address") or ""))
        address_matches = address_score >= 0.75
        hotel_brand = _hotel_brand_key(hotel_name)
        brand_matches = bool(hotel_brand and hotel_brand == _hotel_brand_key(place_name))
        if exact_name and (len(hotel_name) >= 8 or address_matches):
            confidence = 0.98 if address_matches else 0.95
        elif contained_name and address_matches:
            confidence = 0.9
        elif brand_matches and address_matches:
            confidence = 0.88
        else:
            continue
        matches.append((confidence, place))
    if not matches:
        return None
    confidence, place = max(matches, key=lambda value: value[0])
    return place, confidence


def _normalize_place_text(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", value.casefold())


def _address_match_score(left: str | None, right: str | None) -> float:
    left_text = _normalize_place_text(left or "")
    right_text = _normalize_place_text(right or "")
    if not left_text or not right_text:
        return 0.0
    if left_text in right_text or right_text in left_text:
        return 1.0
    street_number = re.compile(r"[\u4e00-\u9fff]{2,8}(?:路|街|巷|大道)\d+号")
    if set(street_number.findall(left_text)) & set(street_number.findall(right_text)):
        return 0.95
    longest = SequenceMatcher(None, left_text, right_text, autojunk=False).find_longest_match().size
    if longest >= 7:
        return 0.9
    if longest >= 5:
        return 0.75
    return 0.0


def _hotel_brand_key(normalized_name: str) -> str:
    for marker in ("酒店", "宾馆", "旅馆", "客栈", "公寓"):
        index = normalized_name.find(marker)
        if index >= 2:
            return normalized_name[: index + len(marker)]
    return ""


__all__ = ["TravelDataCollector"]
