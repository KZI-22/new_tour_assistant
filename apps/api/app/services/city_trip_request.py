from __future__ import annotations

import re
from datetime import date, timedelta

from app.core.request_context import get_request_context
from app.schemas.trip_planning import CityTripRequest

_SMALL_NUMBER_PATTERN = r"(?:\d{1,2}|[零〇一二两三四五六七八九十]{1,3})"
_ISO_DATE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")
_FULL_CHINESE_DATE = re.compile(
    rf"(?<!\d)(\d{{4}})\s*年\s*({_SMALL_NUMBER_PATTERN})\s*月\s*"
    rf"({_SMALL_NUMBER_PATTERN})\s*[日号]?"
)
_YEARLESS_CHINESE_DATE = re.compile(
    rf"(?<!\d)({_SMALL_NUMBER_PATTERN})\s*月\s*({_SMALL_NUMBER_PATTERN})\s*[日号]"
)
_WEEKDAY_DATE = re.compile(r"(下下周|下周|本周|这周|周)([一二三四五六日天])")
_WEEKEND_DATE = re.compile(r"(下下周|下周|本周|这周|这个周)末")
_ORDINAL_DAY = re.compile(rf"第\s*{_SMALL_NUMBER_PATTERN}\s*[天日]")
_DURATION_PATTERNS = (
    re.compile(rf"(?<!第)(?:游玩|行程|玩|游)?\s*({_SMALL_NUMBER_PATTERN})\s*天"),
    re.compile(rf"(?<!第)(?:游玩|行程|玩|游)\s*({_SMALL_NUMBER_PATTERN})\s*日"),
    re.compile(
        rf"(?<!第)({_SMALL_NUMBER_PATTERN})\s*日\s*"
        r"(?:游|行程|(?:旅游|旅行)(?:规划|攻略|计划)?|规划|攻略|计划)"
    ),
)
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


def validate_city_trip_request(
    request: CityTripRequest | None,
    *,
    maximum_days: int,
) -> tuple[list[str], list[str]]:
    missing: list[str] = []
    errors: list[str] = []
    if request is None or not request.destination_city:
        missing.append("destination_city")
    if request is None or request.duration_days is None:
        missing.append("duration_days")
    elif request.duration_days <= 0:
        errors.append("游玩天数至少需要 1 天。")
    elif request.duration_days > maximum_days:
        errors.append(f"目前最多支持 {maximum_days} 天的城市攻略。")
    if request is None or request.start_date is None:
        missing.append("start_date")
    elif request.start_date < _current_date():
        errors.append("出行开始日期不能早于当前日期。")
    return missing, errors


def explicit_dates(text: str, *, today: date | None = None) -> list[date]:
    return [item[2] for item in _explicit_date_mentions(text, today=today)]


def explicit_duration_candidates(text: str) -> list[int]:
    sanitized = text
    for pattern in (_ISO_DATE, _FULL_CHINESE_DATE, _YEARLESS_CHINESE_DATE, _ORDINAL_DAY):
        sanitized = pattern.sub(lambda match: " " * len(match.group(0)), sanitized)
    matches: list[tuple[int, int, int]] = []
    for pattern in _DURATION_PATTERNS:
        for match in pattern.finditer(sanitized):
            value = _small_chinese_number(match.group(1))
            if value is not None:
                matches.append((match.start(), match.end(), value))
    values: list[int] = []
    occupied: list[tuple[int, int]] = []
    for start, end, value in sorted(matches):
        if any(start < used_end and end > used_start for used_start, used_end in occupied):
            continue
        occupied.append((start, end))
        values.append(value)
    return values


def _explicit_date_mentions(
    text: str,
    *,
    today: date | None = None,
) -> list[tuple[int, int, date]]:
    current_date = today or _current_date()
    mentions: list[tuple[int, int, date]] = []

    for match in _ISO_DATE.finditer(text):
        try:
            parsed = date.fromisoformat(match.group(1))
        except ValueError:
            continue
        mentions.append((match.start(), match.end(), parsed))

    for match in _FULL_CHINESE_DATE.finditer(text):
        month = _small_chinese_number(match.group(2))
        day = _small_chinese_number(match.group(3))
        if month is None or day is None:
            continue
        try:
            parsed = date(int(match.group(1)), month, day)
        except ValueError:
            continue
        mentions.append((match.start(), match.end(), parsed))

    occupied = [(start, end) for start, end, _ in mentions]
    for match in _YEARLESS_CHINESE_DATE.finditer(text):
        if any(match.start() < end and match.end() > start for start, end in occupied):
            continue
        month = _small_chinese_number(match.group(1))
        day = _small_chinese_number(match.group(2))
        if month is None or day is None:
            continue
        try:
            parsed = date(current_date.year, month, day)
        except ValueError:
            continue
        mentions.append((match.start(), match.end(), parsed))

    relative_markers = (("后天", 2), ("明天", 1), ("今天", 0))
    for marker, offset in relative_markers:
        start = text.find(marker)
        if start >= 0:
            mentions.append((start, start + len(marker), current_date + timedelta(days=offset)))

    weekday_numbers = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
    week_offsets = {"本周": 0, "这周": 0, "周": 0, "下周": 1, "下下周": 2}
    for match in _WEEKDAY_DATE.finditer(text):
        week_start = current_date - timedelta(days=current_date.weekday())
        parsed = week_start + timedelta(
            weeks=week_offsets[match.group(1)],
            days=weekday_numbers[match.group(2)],
        )
        mentions.append((match.start(), match.end(), parsed))
    for match in _WEEKEND_DATE.finditer(text):
        week_start = current_date - timedelta(days=current_date.weekday())
        parsed = week_start + timedelta(weeks=week_offsets[match.group(1)], days=5)
        mentions.append((match.start(), match.end(), parsed))

    ordered: list[tuple[int, int, date]] = []
    for mention in sorted(mentions, key=lambda item: (item[0], -(item[1] - item[0]))):
        start, end, _ = mention
        if any(start < used_end and end > used_start for used_start, used_end, _ in ordered):
            continue
        ordered.append(mention)
    return ordered


def _small_chinese_number(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    if value == "十":
        return 10
    if "十" in value:
        left, _, right = value.partition("十")
        tens = _CHINESE_DIGITS.get(left, 1) if left else 1
        ones = _CHINESE_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(value) == 1:
        return _CHINESE_DIGITS.get(value)
    return None


def _current_date() -> date:
    context = get_request_context()
    if context is not None:
        return context.time.current_date
    return date.today()


__all__ = [
    "explicit_dates",
    "explicit_duration_candidates",
    "validate_city_trip_request",
]
