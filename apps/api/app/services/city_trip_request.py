from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import date, timedelta

from app.core.request_context import get_request_context
from app.schemas.chat import ChatMessage
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
_ORDINAL_DAY = re.compile(rf"第\s*{_SMALL_NUMBER_PATTERN}\s*[天日]")
_DURATION_PATTERNS = (
    re.compile(rf"(?<!第)(?:游玩|行程|玩|游)?\s*({_SMALL_NUMBER_PATTERN})\s*天"),
    re.compile(rf"(?<!第)(?:游玩|行程|玩|游)\s*({_SMALL_NUMBER_PATTERN})\s*日"),
    re.compile(rf"(?<!第)({_SMALL_NUMBER_PATTERN})\s*日\s*(?:游|行程)"),
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


def apply_explicit_request_overrides(
    request: CityTripRequest,
    messages: Sequence[ChatMessage],
) -> tuple[CityTripRequest, dict[str, bool]]:
    duration = latest_explicit_duration_days(messages)
    start_date = latest_explicit_start_date(messages)
    if duration is not None:
        request.duration_days = duration
    if start_date is not None:
        request.start_date = start_date
    return request, {
        "explicit_duration_override": duration is not None,
        "explicit_start_date_override": start_date is not None,
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


def clarification_question(missing: Sequence[str], errors: Sequence[str]) -> str:
    parts = list(errors)
    missing_set = set(missing)
    labels = {
        "destination_city": "目标城市",
        "duration_days": "游玩天数",
        "start_date": "出行开始日期",
    }
    missing_labels = [labels[field] for field in labels if field in missing_set]
    if missing_labels:
        if missing_labels == ["目标城市"]:
            parts.append("请告诉我想去的目标城市。")
        elif missing_labels == ["游玩天数"]:
            parts.append("请告诉我准备游玩几天。")
        elif missing_labels == ["出行开始日期"]:
            parts.append("请告诉我计划从哪一天开始游玩，例如“7 月 25 日开始”。")
        else:
            parts.append(f"请补充{'、'.join(missing_labels)}，例如“成都 3 天，7 月 25 日开始”。")
    return " ".join(parts) or "请告诉我目标城市、游玩天数和出行开始日期。"


def request_extraction_prompt(messages: Sequence[ChatMessage]) -> str:
    context = get_request_context()
    current_date = _current_date()
    timezone = context.time.timezone if context is not None else "system-local"
    return (
        "结合最近对话提取目标城市、游玩天数、出行开始日期和兴趣。"
        "只有用户明确表达或可由上下文直接继承的值才能填写，不能猜测。"
        "兴趣只能映射到 Schema 中列出的标准标签，不得自由生成标签；"
        "未提供的必填字段使用 null，未提供的偏好使用空数组，food_preferences 始终使用空数组。"
        f"当前日期是 {current_date.isoformat()}，时区是 {timezone}；"
        "无年份的月日按当前年份解释，今天、明天和后天按该日期计算。对话如下：\n"
        f"{json.dumps(conversation_payload(messages), ensure_ascii=False)}"
    )


def conversation_payload(messages: Sequence[ChatMessage]) -> list[dict[str, str]]:
    selected = [message for message in messages if message.role in {"user", "assistant"}][-8:]
    remaining = 12_000
    payload: list[dict[str, str]] = []
    for message in reversed(selected):
        content = message.content.strip()
        if not content or remaining <= 0:
            continue
        normalized = content[: min(4_000, remaining)]
        remaining -= len(normalized)
        payload.insert(0, {"role": message.role, "content": normalized})
    return payload


def latest_explicit_start_date(messages: Sequence[ChatMessage]) -> date | None:
    for message in reversed(messages):
        if message.role == "user":
            return explicit_start_date(message.content)
    return None


def explicit_start_date(text: str, *, today: date | None = None) -> date | None:
    current_date = today or _current_date()
    if match := _ISO_DATE.search(text):
        try:
            return date.fromisoformat(match.group(1))
        except ValueError:
            return None
    if match := _FULL_CHINESE_DATE.search(text):
        try:
            month = _small_chinese_number(match.group(2))
            day = _small_chinese_number(match.group(3))
            if month is None or day is None:
                return None
            return date(int(match.group(1)), month, day)
        except ValueError:
            return None
    if match := _YEARLESS_CHINESE_DATE.search(text):
        try:
            month = _small_chinese_number(match.group(1))
            day = _small_chinese_number(match.group(2))
            if month is None or day is None:
                return None
            return date(current_date.year, month, day)
        except ValueError:
            return None
    for marker, offset in (("后天", 2), ("明天", 1), ("今天", 0)):
        if marker in text:
            return current_date + timedelta(days=offset)
    return None


def latest_explicit_duration_days(messages: Sequence[ChatMessage]) -> int | None:
    for message in reversed(messages):
        if message.role == "user":
            return explicit_duration_days(message.content)
    return None


def explicit_duration_days(text: str) -> int | None:
    sanitized = text
    for pattern in (_ISO_DATE, _FULL_CHINESE_DATE, _YEARLESS_CHINESE_DATE, _ORDINAL_DAY):
        sanitized = pattern.sub(lambda match: " " * len(match.group(0)), sanitized)
    for pattern in _DURATION_PATTERNS:
        if match := pattern.search(sanitized):
            return _small_chinese_number(match.group(1))
    return None


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
    "apply_explicit_request_overrides",
    "clarification_question",
    "conversation_payload",
    "explicit_duration_days",
    "explicit_start_date",
    "latest_explicit_duration_days",
    "latest_explicit_start_date",
    "request_extraction_prompt",
    "validate_city_trip_request",
]
