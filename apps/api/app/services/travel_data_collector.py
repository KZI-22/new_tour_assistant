from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.schemas.itinerary import AffectedSection, HotelOption, TransportOption, TripRequest
from app.schemas.tool_execution import ChatStreamEvent, PlanningStageEvent
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
        max_route_locations: int = 8,
        result_max_length: int,
        timezone: str = "Asia/Shanghai",
    ) -> None:
        self._executor = tool_executor
        self._max_poi_candidates = max_poi_candidates
        self._max_transport_options = max_transport_options
        self._max_hotel_options = max_hotel_options
        self._max_route_locations = max_route_locations
        self._result_max_length = result_max_length
        self._timezone = timezone

    async def collect(
        self,
        request: TripRequest,
        affected_sections: Sequence[AffectedSection],
        *,
        execution_context: ToolExecutionContext,
        writer: EventWriter,
        seed_pois: Sequence[dict[str, Any]] = (),
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
            if calls:
                status = (
                    "success"
                    if any(item.result.success for _, item in stage_results[stage])
                    else "failed"
                )
                _stage(writer, stage, status)
            elif stage in {
                "collecting_transport",
                "collecting_hotels",
                "collecting_pois",
                "collecting_weather",
            }:
                _stage(writer, stage, "skipped")

        normalized = self._normalize_initial(stage_results, request)
        route_candidates = normalized["poi_results"] or list(seed_pois)
        route_candidates = [*route_candidates, *_hotel_route_points(normalized["hotel_results"])]
        route_pairs = await self._collect_routes(
            route_candidates,
            request,
            execution_context=execution_context,
            writer=writer,
            enabled="routes" in sections or "activities" in sections or "hotel" in sections,
        )
        normalized["route_results"] = route_pairs["evidence"]
        normalized["tool_evidence"].extend(route_pairs["evidence"])
        normalized["tool_failures"].extend(route_pairs["failures"])
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
            queries = list(dict.fromkeys([*request.must_visit, *request.interests, "热门景点"]))[:3]
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
                    transport_results.extend(
                        _transport_options(call, outcome, request, timezone=self._timezone)[
                            :transport_per_call
                        ]
                    )
                elif stage == "collecting_hotels":
                    hotel_results.extend(
                        _hotel_options(call, outcome, request)[: self._max_hotel_options]
                    )
                elif stage == "collecting_pois":
                    poi_results.extend(_poi_items(outcome.result.data))
                elif stage == "collecting_weather":
                    weather_results.append(item)

        return {
            "transport_results": _unique_models(transport_results)[: self._max_transport_options],
            "hotel_results": _unique_models(hotel_results)[: self._max_hotel_options],
            "poi_results": _unique_pois(poi_results)[: self._max_poi_candidates],
            "weather_results": weather_results,
            "route_results": [],
            "tool_evidence": evidence,
            "tool_failures": failures,
        }

    async def _collect_routes(
        self,
        pois: Sequence[dict[str, Any]],
        request: TripRequest,
        *,
        execution_context: ToolExecutionContext,
        writer: EventWriter,
        enabled: bool,
    ) -> dict[str, list[Any]]:
        locations = []
        for index, poi in enumerate(pois):
            location = poi.get("location")
            if not isinstance(location, Mapping):
                continue
            longitude = _number(location.get("longitude"))
            latitude = _number(location.get("latitude"))
            if longitude is None or latitude is None:
                continue
            locations.append(
                {
                    "id": str(poi.get("poi_id") or f"poi-{index}"),
                    "name": str(poi.get("name") or f"地点 {index + 1}"),
                    "longitude": longitude,
                    "latitude": latitude,
                    "coordinate_system": "GCJ02",
                }
            )
            if len(locations) >= self._max_route_locations:
                break

        calls: list[dict[str, Any]] = []
        if enabled and len(locations) >= 2:
            if "amap_travel_time_matrix" in self._executor.tool_names:
                calls.append(
                    _tool_call(
                        "amap_travel_time_matrix",
                        {"locations": locations, "mode": "driving"},
                    )
                )
            if "amap_plan_route" in self._executor.tool_names:
                calls.append(
                    _tool_call(
                        "amap_plan_route",
                        {
                            "origin": {
                                "longitude": locations[0]["longitude"],
                                "latitude": locations[0]["latitude"],
                                "coordinate_system": "GCJ02",
                            },
                            "destination": {
                                "longitude": locations[1]["longitude"],
                                "latitude": locations[1]["latitude"],
                                "coordinate_system": "GCJ02",
                            },
                            "mode": "driving",
                            "city": request.destinations[0],
                        },
                    )
                )

        if not calls:
            _stage(writer, "calculating_routes", "skipped")
            return {"evidence": [], "failures": []}

        _stage(writer, "calculating_routes", "running")
        prepared = self._executor.prepare_calls(calls, round_index=1)
        for call in prepared:
            writer(call.event)
        outcomes = await self._executor.execute_many(prepared, context=execution_context)
        evidence: list[dict[str, Any]] = []
        failures: list[str] = []
        for call, outcome in zip(calls, outcomes, strict=True):
            writer(outcome.event)
            if outcome.result.success:
                evidence.append(_evidence(call, outcome, self._result_max_length))
            elif outcome.result.error:
                failures.append(f"{call['name']}: {outcome.result.error.message}")
        _stage(writer, "calculating_routes", "success" if evidence else "failed")
        return {"evidence": evidence, "failures": failures}


def _tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"id": f"planner-{uuid.uuid4().hex}", "name": name, "args": arguments}


def _stage(writer: EventWriter, stage: str, status: str) -> None:
    writer(
        PlanningStageEvent(
            stage=stage,
            display_name=_STAGE_LABELS[stage],
            status=status,
        )
    )


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


def _transport_options(
    call: Mapping[str, Any],
    outcome: ToolExecutionOutcome,
    request: TripRequest,
    *,
    timezone: str,
) -> list[TransportOption]:
    del request
    arguments = call.get("args") if isinstance(call.get("args"), Mapping) else {}
    tool_name = str(call.get("name"))
    transport_type = "flight" if tool_name == "search_flight" else "train"
    options: list[TransportOption] = []
    for mapping in _walk_mappings(outcome.result.data):
        flight_number = _text(mapping, "flight_number", "flightNo", "flight_no", "航班号")
        train_number = _text(mapping, "train_number", "trainNo", "train_no", "车次")
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
                            mapping, "departure_time", "departureTime", "depart_time", "出发时间"
                        ),
                        arguments.get("departure_date"),
                        timezone=timezone,
                    ),
                    arrival_time=_datetime(
                        _first(mapping, "arrival_time", "arrivalTime", "arrive_time", "到达时间"),
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
    return options


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


def _poi_items(data: Any) -> list[dict[str, Any]]:
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
                "location": location,
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


def _hotel_route_points(hotels: Sequence[HotelOption]) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for index, hotel in enumerate(hotels[:3]):
        if not hotel.coordinates:
            continue
        try:
            parsed = json.loads(hotel.coordinates)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, Mapping):
            continue
        longitude = _number(_first(parsed, "longitude", "lng", "lon"))
        latitude = _number(_first(parsed, "latitude", "lat"))
        if longitude is None or latitude is None:
            continue
        points.append(
            {
                "poi_id": hotel.poi_id or f"hotel-{index}",
                "name": hotel.name,
                "location": {
                    "longitude": longitude,
                    "latitude": latitude,
                    "coordinate_system": parsed.get("coordinate_system", "GCJ02"),
                },
            }
        )
    return points


__all__ = ["TravelDataCollector"]
