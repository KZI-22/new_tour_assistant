from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

_MAX_TRANSPORT_OPTIONS_PER_QUERY = 5
_MAX_HOTEL_OPTIONS = 10


@dataclass(frozen=True, slots=True)
class OptionNormalization:
    options: tuple[str, ...]
    provider_item_count: int
    rejected_item_count: int
    schema_valid: bool

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

    options: list[str] = []
    rejected = 0
    for item in items:
        if not isinstance(item, Mapping):
            rejected += 1
            continue
        option = _format_transport_item(item, mode=mode, direction=direction)
        if option is None:
            rejected += 1
            continue
        if len(options) < _MAX_TRANSPORT_OPTIONS_PER_QUERY:
            options.append(option)
    return OptionNormalization(
        options=tuple(options),
        provider_item_count=len(items),
        rejected_item_count=rejected,
        schema_valid=True,
    )


def normalize_hotel_options(payload: object) -> OptionNormalization:
    items = _provider_items(payload)
    if items is None:
        return _invalid_normalization()

    options: list[str] = []
    rejected = 0
    for item in items:
        if not isinstance(item, Mapping):
            rejected += 1
            continue
        option = _format_hotel_item(item)
        if option is None:
            rejected += 1
            continue
        if len(options) < _MAX_HOTEL_OPTIONS:
            options.append(option)
    return OptionNormalization(
        options=tuple(options),
        provider_item_count=len(items),
        rejected_item_count=rejected,
        schema_valid=True,
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


def _format_transport_item(
    item: Mapping[str, object],
    *,
    mode: Literal["flight", "train"],
    direction: str,
) -> str | None:
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

    if duration := _duration_text(item.get("totalDuration") or journey.get("totalDuration")):
        line += f"｜{duration}"
    seat_classes = _unique_texts(_text(segment.get("seatClassName")) for segment in segments)
    if seat_classes:
        line += f"｜{' / '.join(seat_classes)}"
    price = item.get("ticketPrice") if mode == "flight" else item.get("price")
    if price_text := _price_text(price):
        line += f"｜参考价 {price_text}"
    if detail_url := _safe_url(item.get("jumpUrl")):
        line += f"｜[查看详情]({detail_url})"
    return line


def _format_hotel_item(item: Mapping[str, object]) -> str | None:
    name = _text(item.get("name"))
    if not name:
        return None

    parts = [name]
    if star := _text(item.get("star")):
        parts.append(star)
    if price := _price_text(item.get("price")):
        parts.append(f"参考价 {price}")
    if nearby := _text(item.get("interestsPoi")):
        parts.append(nearby)
    if address := _text(item.get("address")):
        parts.append(f"地址：{address}")
    if detail_url := _safe_url(item.get("detailUrl")):
        parts.append(f"[查看详情]({detail_url})")
    return "｜".join(parts)


def _station_label(segment: Mapping[str, object], *, prefix: Literal["dep", "arr"]) -> str:
    station = _text(segment.get(f"{prefix}StationName"))
    city = _text(segment.get(f"{prefix}CityName"))
    label = station or city
    terminal = _text(segment.get(f"{prefix}Term"))
    return f"{label} {terminal}".strip() if terminal else label


def _duration_text(value: object) -> str:
    raw = _text(value)
    if not raw:
        return ""
    try:
        minutes = int(float(raw))
    except ValueError:
        return ""
    if minutes <= 0:
        return ""
    hours, remaining = divmod(minutes, 60)
    if hours and remaining:
        return f"{hours}小时{remaining}分"
    if hours:
        return f"{hours}小时"
    return f"{remaining}分"


def _price_text(value: object) -> str:
    raw = _text(value)
    if not raw:
        return ""
    return raw if raw.startswith(("¥", "￥")) else f"¥{raw}"


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
