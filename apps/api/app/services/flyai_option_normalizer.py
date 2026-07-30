from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from app.schemas.trip_options import (
    HotelOptionSnapshot,
    TransportOptionSnapshot,
    TripOptionSnapshot,
)

_MAX_TRANSPORT_OPTIONS_PER_QUERY = 5
_MAX_HOTEL_OPTIONS = 10


@dataclass(frozen=True, slots=True)
class OptionNormalization:
    options: tuple[str, ...]
    provider_item_count: int
    rejected_item_count: int
    schema_valid: bool
    normalized_options: tuple[TripOptionSnapshot, ...] = ()

    @property
    def usable(self) -> bool:
        return bool(self.options)

    @property
    def empty(self) -> bool:
        return self.schema_valid and self.provider_item_count == 0


def normalize_transport_options(
    payload: object,
    *,
    mode: Literal["flight", "train"],
    direction: str,
) -> OptionNormalization:
    items = _provider_items(payload)
    if items is None:
        return _invalid_normalization()

    options: list[TransportOptionSnapshot] = []
    rejected = 0
    for item in items:
        if not isinstance(item, Mapping):
            rejected += 1
            continue
        option = _normalize_transport_item(item, mode=mode, direction=direction)
        if option is None:
            rejected += 1
            continue
        if len(options) < _MAX_TRANSPORT_OPTIONS_PER_QUERY:
            options.append(option)
    return OptionNormalization(
        options=tuple(item.display_text for item in options),
        provider_item_count=len(items),
        rejected_item_count=rejected,
        schema_valid=True,
        normalized_options=tuple(options),
    )


def normalize_hotel_options(payload: object) -> OptionNormalization:
    items = _provider_items(payload)
    if items is None:
        return _invalid_normalization()

    options: list[HotelOptionSnapshot] = []
    rejected = 0
    for item in items:
        if not isinstance(item, Mapping):
            rejected += 1
            continue
        option = _normalize_hotel_item(item)
        if option is None:
            rejected += 1
            continue
        if len(options) < _MAX_HOTEL_OPTIONS:
            options.append(option)
    return OptionNormalization(
        options=tuple(item.display_text for item in options),
        provider_item_count=len(items),
        rejected_item_count=rejected,
        schema_valid=True,
        normalized_options=tuple(options),
    )


def _provider_items(payload: object) -> list[object] | None:
    if not isinstance(payload, Mapping):
        return None
    status = payload.get("status")
    if status not in (None, 0, "0"):
        return None
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return None
    raw_items = data.get("itemList")
    if not _is_sequence(raw_items):
        return None
    return list(raw_items)


def _normalize_transport_item(
    item: Mapping[str, object],
    *,
    mode: Literal["flight", "train"],
    direction: str,
) -> TransportOptionSnapshot | None:
    journeys = item.get("journeys")
    if not _is_sequence(journeys):
        return None
    journey = next((value for value in journeys if isinstance(value, Mapping)), None)
    if journey is None:
        return None
    raw_segments = journey.get("segments")
    if not _is_sequence(raw_segments):
        return None
    segments = [value for value in raw_segments if isinstance(value, Mapping)]
    if not segments:
        return None

    first_segment = segments[0]
    last_segment = segments[-1]
    departure_station = _station_label(first_segment, prefix="dep")
    arrival_station = _station_label(last_segment, prefix="arr")
    departure_time = _format_datetime(_text(first_segment.get("depDateTime")))
    arrival_time = _format_datetime(_text(last_segment.get("arrDateTime")))
    transport_numbers = _unique_texts(
        _text(segment.get("marketingTransportNo")) for segment in segments
    )
    if not all(
        (
            departure_station,
            arrival_station,
            departure_time,
            arrival_time,
            transport_numbers,
        )
    ):
        return None

    direction_label = "返程" if direction == "return" else "去程"
    mode_label = "航班" if mode == "flight" else "火车"
    transport_names = _unique_texts(
        _text(segment.get("marketingTransportName")) for segment in segments
    )
    journey_type = _text(journey.get("journeyType")) or ("直达" if len(segments) == 1 else "中转")
    descriptor_values = [*transport_names, journey_type]
    descriptor = f"（{'，'.join(descriptor_values)}）" if descriptor_values else ""
    line = (
        f"{direction_label}{mode_label} {' → '.join(transport_numbers)}{descriptor}"
        f"｜{departure_station} {departure_time} → {arrival_station} {arrival_time}"
    )

    duration_value = item.get("totalDuration") or journey.get("totalDuration")
    duration_minutes = _duration_minutes(duration_value)
    if duration := _duration_text(duration_value):
        line += f"｜{duration}"
    seat_classes = _unique_texts(_text(segment.get("seatClassName")) for segment in segments)
    if seat_classes:
        line += f"｜{' / '.join(seat_classes)}"
    price = item.get("ticketPrice") if mode == "flight" else item.get("price")
    if price_text := _price_text(price):
        line += f"｜参考价 {price_text}"
    detail_url = _safe_url(item.get("jumpUrl"))
    if detail_url:
        line += f"｜[查看详情]({detail_url})"
    price_amount = _price_amount(price)
    return TransportOptionSnapshot(
        option_id=_option_id("transport", item, mode, direction),
        mode=mode,
        direction="return" if direction == "return" else "outbound",
        journey_type=journey_type,
        transport_names=transport_names,
        transport_numbers=transport_numbers,
        departure_station=departure_station,
        departure_at=departure_time,
        arrival_station=arrival_station,
        arrival_at=arrival_time,
        duration_minutes=duration_minutes,
        seat_classes=seat_classes,
        price_amount=price_amount,
        currency="CNY" if price_amount is not None else None,
        detail_url=detail_url or None,
        display_text=line,
    )


def _normalize_hotel_item(item: Mapping[str, object]) -> HotelOptionSnapshot | None:
    name = _text(item.get("name"))
    if not name:
        return None

    parts = [name]
    star = _text(item.get("star"))
    if star:
        parts.append(star)
    raw_price = item.get("price")
    if price_text := _price_text(raw_price):
        parts.append(f"参考价 {price_text}")
    nearby = _text(item.get("interestsPoi"))
    if nearby:
        parts.append(nearby)
    address = _text(item.get("address"))
    if address:
        parts.append(f"地址：{address}")
    detail_url = _safe_url(item.get("detailUrl"))
    if detail_url:
        parts.append(f"[查看详情]({detail_url})")
    price_amount = _price_amount(raw_price)
    return HotelOptionSnapshot(
        option_id=_option_id("hotel", item),
        provider_hotel_id=(
            _text(item.get("hotelId"))
            or _text(item.get("id"))
            or _text(item.get("itemId"))
            or None
        ),
        name=name,
        star=star or None,
        price_amount=price_amount,
        currency="CNY" if price_amount is not None else None,
        nearby_poi=nearby or None,
        address=address or None,
        detail_url=detail_url or None,
        display_text="｜".join(parts),
    )


def _station_label(segment: Mapping[str, object], *, prefix: Literal["dep", "arr"]) -> str:
    station = _text(segment.get(f"{prefix}StationName"))
    city = _text(segment.get(f"{prefix}CityName"))
    label = station or city
    terminal = _text(segment.get(f"{prefix}Term"))
    return f"{label} {terminal}".strip() if terminal else label


def _duration_text(value: object) -> str:
    minutes = _duration_minutes(value)
    if minutes is None:
        return ""
    hours, remaining = divmod(minutes, 60)
    if hours and remaining:
        return f"{hours}小时{remaining}分"
    if hours:
        return f"{hours}小时"
    return f"{remaining}分"


def _duration_minutes(value: object) -> int | None:
    raw = _scalar_text(value)
    if not raw:
        return None
    try:
        minutes = int(float(raw))
    except ValueError:
        return None
    return minutes if minutes > 0 else None


def _price_text(value: object) -> str:
    raw = _scalar_text(value)
    if not raw:
        return ""
    return raw if raw.startswith(("¥", "￥")) else f"¥{raw}"


def _price_amount(value: object) -> Decimal | None:
    raw = _scalar_text(value).replace(",", "").lstrip("¥￥")
    if not raw:
        return None
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        return None
    return amount if amount >= 0 else None


def _format_datetime(value: str) -> str:
    if len(value) >= 16 and value[4:5] == "-" and value[10:11] in {" ", "T"}:
        return value[:16].replace("T", " ")
    return value


def _safe_url(value: object) -> str:
    raw = _text(value)
    return raw if raw.startswith(("https://", "http://")) else ""


def _unique_texts(values: Iterable[str]) -> list[str]:
    unique: list[str] = []
    for value in values:
        if value and value not in unique:
            unique.append(value)
    return unique


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _scalar_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    return ""


def _option_id(kind: str, item: Mapping[str, object], *scope: str) -> str:
    serialized = json.dumps(
        item,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256("|".join((kind, *scope, serialized)).encode()).hexdigest()[:24]
    return f"{kind}_{digest}"


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _invalid_normalization() -> OptionNormalization:
    return OptionNormalization(
        options=(),
        provider_item_count=0,
        rejected_item_count=0,
        schema_valid=False,
    )


__all__ = [
    "OptionNormalization",
    "normalize_hotel_options",
    "normalize_transport_options",
]
