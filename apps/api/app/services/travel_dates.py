from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.schemas.context import NormalizedTravelDates, TravelDateValidationResult

_ISO_DATE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")
_CHINESE_DATE = re.compile(r"(?<!\d)(\d{4})年(\d{1,2})月(\d{1,2})日?")
_NIGHTS = re.compile(r"住\s*([零〇一二两三四五六七八九十百\d]+)\s*晚")
_CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def normalize_travel_dates(
    expression: str,
    *,
    now: datetime | None = None,
    timezone: str = "Asia/Shanghai",
) -> NormalizedTravelDates:
    normalized_expression = expression.strip()
    zone = ZoneInfo(timezone)
    current = now.astimezone(zone) if now is not None else datetime.now(zone)
    parsed_dates = _extract_dates(normalized_expression, current.date())
    nights = _extract_nights(normalized_expression)

    if not parsed_dates:
        return NormalizedTravelDates(
            original_expression=expression,
            timezone=timezone,
            is_ambiguous=True,
            message="无法确定具体日期",
        )
    if nights is not None and nights < 1:
        return NormalizedTravelDates(
            original_expression=expression,
            timezone=timezone,
            is_ambiguous=True,
            message="住宿晚数必须为正整数",
            candidates=parsed_dates,
        )

    departure = parsed_dates[0]
    return_date = parsed_dates[1] if len(parsed_dates) > 1 else None
    check_out = departure + timedelta(days=nights) if nights is not None else None
    return NormalizedTravelDates(
        original_expression=expression,
        departure_date=departure,
        return_date=return_date,
        check_in_date=departure,
        check_out_date=check_out,
        nights=nights,
        timezone=timezone,
        is_ambiguous=False,
    )


def validate_travel_dates(
    departure_date: date | str | None,
    *,
    return_date: date | str | None = None,
    check_in_date: date | str | None = None,
    check_out_date: date | str | None = None,
    nights: int | None = None,
    is_ambiguous: bool = False,
    candidates: list[date] | None = None,
    now: datetime | None = None,
    timezone: str = "Asia/Shanghai",
) -> TravelDateValidationResult:
    if is_ambiguous:
        return TravelDateValidationResult(
            is_valid=False,
            is_ambiguous=True,
            message="无法确定具体日期",
            candidates=candidates or [],
        )

    zone = ZoneInfo(timezone)
    current = now.astimezone(zone) if now is not None else datetime.now(zone)
    parsed_departure = _coerce_date(departure_date)
    parsed_return = _coerce_date(return_date)
    parsed_check_in = _coerce_date(check_in_date)
    parsed_check_out = _coerce_date(check_out_date)
    supplied_values = (departure_date, return_date, check_in_date, check_out_date)
    parsed_values = (parsed_departure, parsed_return, parsed_check_in, parsed_check_out)

    if departure_date is None:
        return _invalid("缺少出发日期")
    invalid_formats = any(
        supplied is not None and parsed is None
        for supplied, parsed in zip(supplied_values, parsed_values, strict=True)
    )
    if invalid_formats:
        return _invalid("日期格式无效，请使用 YYYY-MM-DD")
    assert parsed_departure is not None
    if parsed_departure < current.date():
        return _invalid("出发日期不能早于当前日期")
    if parsed_return is not None and parsed_return < parsed_departure:
        return _invalid("返程日期不能早于出发日期")
    if (parsed_check_in is None) != (parsed_check_out is None):
        return _invalid("入住日期和退房日期必须同时提供")
    if parsed_check_in is not None and parsed_check_out is not None:
        if parsed_check_out <= parsed_check_in:
            return _invalid("退房日期必须晚于入住日期")
        actual_nights = (parsed_check_out - parsed_check_in).days
        if nights is not None and actual_nights != nights:
            return _invalid("住宿晚数与入住、退房日期不一致")
    if nights is not None and nights < 1:
        return _invalid("住宿晚数必须为正整数")
    return TravelDateValidationResult(
        is_valid=True,
        is_ambiguous=False,
        message="日期有效",
    )


def _extract_dates(expression: str, current_date: date) -> list[date]:
    explicit: list[tuple[int, date]] = []
    for match in _ISO_DATE.finditer(expression):
        parsed = _coerce_date(match.group(1))
        if parsed is not None:
            explicit.append((match.start(), parsed))
    for match in _CHINESE_DATE.finditer(expression):
        try:
            parsed = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            continue
        explicit.append((match.start(), parsed))
    if explicit:
        return [item[1] for item in sorted(explicit, key=lambda item: item[0])]

    relative_offsets = {"后天": 2, "明天": 1, "今天": 0}
    for marker, offset in relative_offsets.items():
        if marker in expression:
            return [current_date + timedelta(days=offset)]
    return []


def _extract_nights(expression: str) -> int | None:
    match = _NIGHTS.search(expression)
    if match is None:
        return None
    return _parse_chinese_number(match.group(1))


def _parse_chinese_number(value: str) -> int:
    if value.isdigit():
        return int(value)
    if value == "百":
        return 100
    if "百" in value:
        hundreds, remainder = value.split("百", 1)
        total = _CHINESE_DIGITS.get(hundreds, 1) * 100
        return total + (_parse_chinese_number(remainder) if remainder else 0)
    if "十" in value:
        tens, ones = value.split("十", 1)
        return _CHINESE_DIGITS.get(tens, 1) * 10 + _CHINESE_DIGITS.get(ones, 0)
    digits = [_CHINESE_DIGITS[item] for item in value if item in _CHINESE_DIGITS]
    if not digits:
        return 0
    return int("".join(str(item) for item in digits))


def _coerce_date(value: date | str | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _invalid(message: str) -> TravelDateValidationResult:
    return TravelDateValidationResult(
        is_valid=False,
        is_ambiguous=False,
        message=message,
    )


__all__ = ["normalize_travel_dates", "validate_travel_dates"]
