from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import suppress
from typing import Any, cast

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from app.schemas.map_planning import MapDayNarrative, MapPlaceNarrative
from app.schemas.trip_itinerary import TripNarrativePlan
from app.schemas.trip_plan_snapshot import TripPlanSnapshotV2

STRUCTURED_TRIP_SYSTEM_PROMPT = """你是一名严谨、克制、擅长信息设计的旅行攻略编辑。

请仅根据输入 JSON 中已经验证的事实，写成一份适合手机阅读的 Markdown 旅行攻略。
直接从一级标题开始，不要解释过程，不要输出 JSON 或 Markdown 代码块。

固定结构：
1. 一级标题与 2～4 行行程速览。
2. “每日详细行程”，按 days 原顺序完整展示每天及其全部 places。
3. 每个景点保留名称、地址、类型、建议游玩时长、安排依据和相邻路段事实。
4. 全部日期结束后再写“城市餐饮推荐”，只展示 restaurant_recommendations 中已有的店，
   不得把餐厅编入任何一天，也不得增加路线、时间或菜品。
5. 最后写简短的出行提醒，只整理天气 advice、路线降级和 warnings。

事实边界：
- 不得新增、删除、重排景点或餐厅。
- 不得虚构开放时间、门票、预约、人均消费、菜品、营业状态、历史背景或交通线路。
- 不得展示内部 ID、检索排名、schema_version 等实现字段。
- 餐厅不足三家时按实际数量输出，不得补足。
- 不得提及酒店、航班、火车票或住宿安排。
"""


class StructuredItineraryGenerationError(RuntimeError):
    pass


class StructuredItineraryGenerator:
    def __init__(
        self,
        model: BaseChatModel,
        *,
        timeout_seconds: float,
        idle_timeout_seconds: float = 20,
    ) -> None:
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._idle_timeout_seconds = min(idle_timeout_seconds, timeout_seconds)

    async def stream_markdown(self, snapshot: TripPlanSnapshotV2) -> AsyncIterator[str]:
        messages = [
            SystemMessage(content=STRUCTURED_TRIP_SYSTEM_PROMPT),
            HumanMessage(
                content=json.dumps(
                    snapshot.model_dump(mode="json"),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            ),
        ]
        stream = self._model.astream(messages, **_stream_options(self._model))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout_seconds
        emitted = False
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise StructuredItineraryGenerationError("旅行攻略生成超时。")
                try:
                    chunk = await asyncio.wait_for(
                        anext(stream),
                        timeout=min(self._idle_timeout_seconds, remaining),
                    )
                except StopAsyncIteration:
                    break
                except TimeoutError as exc:
                    raise StructuredItineraryGenerationError("旅行攻略生成等待超时。") from exc
                text = _message_text(chunk)
                if text:
                    emitted = True
                    yield text
        except asyncio.CancelledError:
            raise
        except StructuredItineraryGenerationError:
            raise
        except Exception as exc:
            raise StructuredItineraryGenerationError("旅行攻略生成失败。") from exc
        finally:
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                with suppress(Exception):
                    await aclose()
        if not emitted:
            raise StructuredItineraryGenerationError("模型未返回旅行攻略。")


def build_structured_narrative(snapshot: TripPlanSnapshotV2) -> TripNarrativePlan:
    city = snapshot.request.destination_city
    interests = "、".join(item.value for item in snapshot.request.interests)
    summary = f"围绕{city}的景点、天气与市内路线形成的 {snapshot.request.duration_days} 日方案。"
    if interests:
        summary = f"围绕{interests}偏好整理的{city} {snapshot.request.duration_days} 日方案。"
    return TripNarrativePlan(
        title=f"{city}{snapshot.request.duration_days}日旅行方案",
        summary=summary,
        days=[
            MapDayNarrative(
                day_index=day.day_index,
                date=day.date,
                theme=f"{city}第 {day.day_index} 天",
                places=[
                    MapPlaceNarrative(
                        reference_id=place.reference_id,
                        recommendation_reason=(
                            "；".join(place.selection_reasons)
                            if place.selection_reasons
                            else "来自本次高德景点检索与路线编排结果。"
                        ),
                    )
                    for place in day.places
                ],
                weather_advice=list(day.weather.advice),
                tips=list(day.warnings[:3]),
            )
            for day in snapshot.days
        ],
        practical_tips=list(snapshot.warnings[:5]),
        warnings=[],
    )


def render_structured_itinerary(
    snapshot: TripPlanSnapshotV2,
    narrative: TripNarrativePlan,
) -> str:
    lines = [f"# {narrative.title}", "", narrative.summary]
    narrative_by_day = {day.day_index: day for day in narrative.days}
    for day in snapshot.days:
        day_narrative = narrative_by_day[day.day_index]
        lines.extend(
            ["", f"## 第 {day.day_index} 天｜{day.date.isoformat()}｜{day_narrative.theme}"]
        )
        if day.weather.coverage == "available":
            lines.extend(
                [
                    "",
                    (
                        f"**天气：** 白天 {day.weather.day_weather or '—'} "
                        f"{day.weather.day_temperature or '—'}℃；夜间 "
                        f"{day.weather.night_weather or '—'} "
                        f"{day.weather.night_temperature or '—'}℃。"
                    ),
                ]
            )
        reasons = {item.reference_id: item.recommendation_reason for item in day_narrative.places}
        for index, place in enumerate(day.places, start=1):
            lines.extend(
                [
                    "",
                    f"### {index}. {place.name}",
                    "",
                    f"- 地址：{place.address or '高德未提供'}",
                    f"- 类型：{place.poi_type or '高德未提供'}",
                    f"- 建议游玩：约 {place.estimated_visit_minutes} 分钟",
                    f"- 安排说明：{reasons[place.reference_id]}",
                ]
            )
    if snapshot.restaurant_recommendations:
        lines.extend(["", "## 城市餐饮推荐"])
        for restaurant in snapshot.restaurant_recommendations:
            facts = [restaurant.poi_type, restaurant.business_area]
            if restaurant.rating is not None:
                facts.append(f"高德评分 {restaurant.rating:g}")
            lines.extend(
                [
                    "",
                    f"### {restaurant.name}",
                    "",
                    f"- 地址：{restaurant.address or '高德未提供'}",
                    f"- 信息：{'｜'.join(item for item in facts if item)}",
                    f"- 推荐依据：{restaurant.recommendation_reason}",
                ]
            )
    if snapshot.warnings:
        lines.extend(["", "## 使用说明", ""])
        lines.extend(f"- {item}" for item in snapshot.warnings)
    return "\n".join(lines)


def _message_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, Mapping) and isinstance(block.get("text"), str):
            parts.append(cast(str, block["text"]))
    return "".join(parts)


def _stream_options(model: BaseChatModel) -> dict[str, Any]:
    model_name = str(getattr(model, "model_name", None) or getattr(model, "model", "")).casefold()
    options: dict[str, Any] = {"temperature": 0}
    if model_name.startswith("qwen"):
        options["extra_body"] = {"enable_thinking": False}
    return options


__all__ = [
    "STRUCTURED_TRIP_SYSTEM_PROMPT",
    "StructuredItineraryGenerationError",
    "StructuredItineraryGenerator",
    "build_structured_narrative",
    "render_structured_itinerary",
]
