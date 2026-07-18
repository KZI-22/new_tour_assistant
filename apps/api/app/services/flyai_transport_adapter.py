from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from app.schemas.itinerary import TransportOption

TransportType = Literal["flight", "train"]

FLYAI_TRANSPORT_SCHEMA_VERSION = "flyai-transport-v1"


@dataclass(frozen=True, slots=True)
class FlyAITransportNormalization:
    recognized: bool
    options: list[TransportOption]
    provider_item_count: int
    journey_count: int
    rejected_count: int
    schema_version: str = FLYAI_TRANSPORT_SCHEMA_VERSION


def normalize_flyai_transport(
    data: Any,
    *,
    transport_type: TransportType,
    source_tool: str,
    provider: str,
    queried_at: datetime,
    arguments: Mapping[str, Any],
    timezone: str,
) -> FlyAITransportNormalization:
    """Convert FlyAI's item/journey/segment hierarchy into canonical options."""

    raw_items = _item_list(data)
    if raw_items is None:
        return FlyAITransportNormalization(
            recognized=False,
            options=[],
            provider_item_count=0,
            journey_count=0,
            rejected_count=0,
        )

    options: list[TransportOption] = []
    journey_count = 0
    rejected_count = 0
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            rejected_count += 1
            continue
        journeys = _mapping_sequence(raw_item.get("journeys"))
        if not journeys:
            rejected_count += 1
            continue
        for journey in journeys:
            journey_count += 1
            option = _journey_option(
                raw_item,
                journey,
                transport_type=transport_type,
                source_tool=source_tool,
                provider=provider,
                queried_at=queried_at,
                arguments=arguments,
                timezone=timezone,
            )
            if option is None:
                rejected_count += 1
            else:
                options.append(option)

    return FlyAITransportNormalization(
        recognized=True,
        options=options,
        provider_item_count=len(raw_items),
        journey_count=journey_count,
        rejected_count=rejected_count,
    )


def _item_list(data: Any) -> list[Any] | None:
    current = data
    for _ in range(3):
        if not isinstance(current, Mapping):
            return None
        items = current.get("itemList")
        if isinstance(items, list):
            return items
        nested = current.get("data")
        if nested is current:
            return None
        current = nested
    return None


def _journey_option(
    item: Mapping[str, Any],
    journey: Mapping[str, Any],
    *,
    transport_type: TransportType,
    source_tool: str,
    provider: str,
    queried_at: datetime,
    arguments: Mapping[str, Any],
    timezone: str,
) -> TransportOption | None:
    segments = _mapping_sequence(journey.get("segments"))
    if not segments:
        return None

    first_segment = segments[0]
    last_segment = segments[-1]
    numbers = _unique_texts(_text(segment.get("marketingTransportNo")) for segment in segments)
    if not numbers:
        return None

    departure_time = _datetime(first_segment.get("depDateTime"), timezone=timezone)
    arrival_time = _datetime(last_segment.get("arrDateTime"), timezone=timezone)
    number = " → ".join(numbers)
    seats = _unique_texts(_text(segment.get("seatClassName")) for segment in segments)
    price_key = "ticketPrice" if transport_type == "flight" else "price"
    price = _number(item.get(price_key))
    if price is None:
        price = _number(item.get("price")) or _number(item.get("ticketPrice"))
    duration = _minutes(journey.get("totalDuration"))
    if duration is None:
        duration = _minutes(item.get("totalDuration"))
    if duration is None:
        segment_minutes = [_minutes(segment.get("duration")) for segment in segments]
        if all(value is not None for value in segment_minutes):
            duration = sum(value for value in segment_minutes if value is not None)

    reference_time = departure_time.isoformat() if departure_time else "unknown-time"
    values: dict[str, Any] = {
        "transport_type": transport_type,
        "provider": provider,
        "timezone": timezone,
        "departure_city": _text(first_segment.get("depCityName"))
        or str(arguments.get("origin") or "未知"),
        "arrival_city": _text(last_segment.get("arrCityName"))
        or str(arguments.get("destination") or "未知"),
        "departure_time": departure_time,
        "arrival_time": arrival_time,
        "origin_station": _text(first_segment.get("depStationName")),
        "destination_station": _text(last_segment.get("arrStationName")),
        "price": price,
        "seat_or_cabin": " / ".join(seats) or None,
        "duration_minutes": duration,
        "source_tool": source_tool,
        "source_reference": f"{source_tool}:{number}:{reference_time}",
        "queried_at": queried_at,
    }
    values["flight_number" if transport_type == "flight" else "train_number"] = number
    try:
        return TransportOption.model_validate(values)
    except ValueError:
        return None


def _mapping_sequence(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _unique_texts(values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, (Mapping, list)):
        return None
    normalized = str(value).strip()
    return normalized or None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group()) if match else None


def _minutes(value: Any) -> int | None:
    parsed = _number(value)
    return max(0, round(parsed)) if parsed is not None else None


def _datetime(value: Any, *, timezone: str) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or ZoneInfo(timezone))
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("/", "-"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or ZoneInfo(timezone))


__all__ = [
    "FLYAI_TRANSPORT_SCHEMA_VERSION",
    "FlyAITransportNormalization",
    "normalize_flyai_transport",
]
