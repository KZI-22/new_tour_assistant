from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import suppress
from datetime import date
from typing import Any, cast

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from app.schemas.map_planning import (
    MapDayEvidence,
    MapDayNarrative,
    MapPlaceEvidence,
    MapPlaceNarrative,
)
from app.schemas.trip_evidence import EvidenceStatus, JoinedTripEvidence
from app.schemas.trip_itinerary import (
    TripDayNarrativeDraft,
    TripNarrativeDraft,
    TripNarrativePlan,
)
from app.schemas.trip_plan_snapshot import TripPlanSnapshot
from app.schemas.trip_planning import DailyWeatherEvidence
from app.services.trip_presentation_context import build_trip_presentation_context
from app.services.weather_advice_service import build_weather_advice

TRIP_MARKDOWN_SYSTEM_PROMPT = """你是一名擅长信息设计和路线叙事的旅行攻略编辑。

请根据输入中的已编排行程事实，直接写成一份用户可以阅读的完整 Markdown 旅行攻略。
从 Markdown 标题立即开始输出，不要解释思考过程，不要输出 JSON，也不要使用 Markdown 代码块包裹正文。

成品目标：
- 它应当像一份可以直接收藏和照着走的成品攻略，而不是字段清单或数据摘要。
- 使用清晰的 Markdown 层级、适量 emoji、粗体标签、引用提示和留白建立视觉节奏。
- 语言自然、有旅行感，但克制、可信、易扫读；避免公文腔、模板腔和重复套话。
- 优先适配手机阅读：短段落、短列表，不使用 Markdown 表格，不堆砌装饰符号。

请按以下结构成文；某部分未启用或没有可用信息时自然省略：

# 🧭 行程标题
标题在信息充分时包含“出发地 → 目的地”、天数和日期范围。

> 用 2～4 行完成行程速览。使用粗体标签分别概括出行日期、天气、城际交通和住宿查询范围；
> 只写输入已提供的信息，不要推断住宿晚数、具体车站或机场。

## 🚄 / ✈️ 城际交通参考
- 根据实际 modes 选择合适标题；多种方式并存时使用“城际交通参考”。
- 先用一句话说明这是供用户自行比较的查询结果，再按输入 options 顺序逐项编号展示。
- 每个选项独占一项，保留其中的班次、起讫点、日期、时间、时长、舱座和价格等已有信息。
- 如果输入能够明确区分去程与返程，可以使用三级标题分组；否则不要自行判断方向。

## 🏨 住宿参考
- 先写入住、退房、目的地或 nearby_poi 等已有查询条件，再按输入 options 顺序逐项编号展示。
- 酒店是候选信息，不使用“首选”“最推荐”“替你选好”等替用户决策的措辞。

## 🗓️ 每日详细行程
每一天都使用以下有层次的版式：

### Day N｜日期（星期）｜当日路线
> **今日路线：** 按 places 原顺序用“→”连接全部景点。
> **天气：** 自然整合已有天气和 advice。
> **行程概览：** 展示已有的总游览时长、总交通时长；没有的数据不要补。

然后逐个景点书写：

#### 序号. 景点名称
- 用紧凑的粗体标签展示已有的地址、类型、建议游玩时长和偏好匹配。
- 另起一小段写 **安排说明：** 用 1～2 句说明它在当天既定路线中的位置，
  并自然整理 selection_reasons、matched_preferences 等已有依据。
- 在相邻景点之间，根据对应 route_legs 写一行简短的 **下一程：**，
  保留出发点、到达点、方式、距离、耗时、换乘数和降级情况中实际存在的信息。
- 当天存在 warnings 时，在相关位置使用引用块突出显示，不要把普通事实渲染成警告。
- 每天结束时可用一句简短的“当日收尾”串联当天节奏，但不得增加活动或事实。

不同天之间使用 `---` 分隔，让长攻略更容易浏览。

## 💡 重要出行贴士
- 只整理 weather.advice、route_legs、warnings 和已给出的行程事实。
- 合并重复提醒，优先给出对整趟行程真正有帮助的 3～6 条信息。

事实边界：
1. 日期、地点、景点顺序、路线、天气、交通和酒店候选均已由后端确定；
   不得重新排序、删除或新增景点，也不得改变已有事实。
2. 交通和酒店 options 必须作为供用户自行选择的参考信息完整展示；
   不得替用户选择，也不得声称已经预订、支付、出票、锁价、占座或确认库存。
3. 每天必须严格按照 places 顺序介绍全部景点，并保留输入提供的日期、天气、地址、
   建议游玩时长、路段距离和路段耗时。可以增加自然的过渡语，但不得改变这些事实。
4. 输入没有提供精确到访时刻、开放时间、门票、预约规则、历史背景、具体餐厅、美食、
   地铁线路或营业状态时，禁止补写；不得为了让攻略“丰富”而使用常识补全。
5. 可以使用“第一站、随后、下一站、最后一站”等路线衔接词；
   没有精确时刻时，不得编写“09:00”“上午”“午后”等时间安排。
6. 不要向用户展示 reference_id、origin_ref、destination_ref、error_code、status 等内部字段名。
7. 不要推断输入未给出的住宿晚数、交通目的地、车站归属或景点属性；
   只启用了火车时不得提及机场，只给出温度时不得升级为“极端天气”，不得推荐药品。
8. 逐个景点使用“安排说明”而不是“简介”；安排说明只能整理名称、地址、类型、
   建议时长、偏好匹配、筛选理由和其在既定顺序中的位置，不得补充常识性介绍。
9. 字段缺失时直接省略对应标签，不写“暂无”“未知”，也不暴露后端数据结构。
10. 好看来自信息层级、路线叙事和排版，不来自编造内容；信息完整优先，不要省略候选项或景点。"""


class TripItineraryGenerationError(RuntimeError):
    def __init__(self, message: str, *, code: str = "MODEL_GENERATION_FAILED") -> None:
        super().__init__(message)
        self.code = code


class TripItineraryGenerator:
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

    async def stream_markdown(
        self,
        snapshot: TripPlanSnapshot,
    ) -> AsyncIterator[str]:
        prompt = build_trip_generation_prompt(snapshot)
        messages = [
            SystemMessage(content=TRIP_MARKDOWN_SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ]
        stream = self._model.astream(messages, **_stream_options(self._model))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout_seconds
        emitted_text = False
        try:
            while True:
                remaining_seconds = deadline - loop.time()
                if remaining_seconds <= 0:
                    raise TripItineraryGenerationError(
                        "模型流式生成超过整体时限。",
                        code="MODEL_STREAM_TOTAL_TIMEOUT",
                    )
                chunk_timeout = min(self._idle_timeout_seconds, remaining_seconds)
                try:
                    chunk = await asyncio.wait_for(anext(stream), timeout=chunk_timeout)
                except StopAsyncIteration:
                    break
                except TimeoutError as exc:
                    code = (
                        "MODEL_STREAM_TOTAL_TIMEOUT"
                        if remaining_seconds <= self._idle_timeout_seconds
                        else "MODEL_STREAM_IDLE_TIMEOUT"
                    )
                    raise TripItineraryGenerationError(
                        "模型流式生成等待超时。",
                        code=code,
                    ) from exc
                text = _message_text(chunk)
                if text:
                    emitted_text = True
                    yield text
        except asyncio.CancelledError:
            raise
        except TripItineraryGenerationError:
            raise
        except Exception as exc:
            raise TripItineraryGenerationError(
                "模型流式生成旅行方案失败。",
                code="MODEL_STREAM_FAILED",
            ) from exc
        finally:
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                with suppress(Exception):
                    await aclose()

        if not emitted_text:
            raise TripItineraryGenerationError(
                "模型没有生成旅行方案正文。",
                code="MODEL_STREAM_EMPTY",
            )


def build_trip_narrative_skeleton(
    evidence: JoinedTripEvidence,
) -> TripNarrativePlan:
    core = evidence.request.core
    city = core.destination_city or "目的地"
    duration_days = core.duration_days or len(
        evidence.map_weather.map.days if evidence.map_weather.map else []
    )
    draft = TripNarrativeDraft(
        title=f"{city}{duration_days}日旅行方案",
        summary=("日期、地点、顺序、路线、天气、交通与酒店信息已根据本次查询结果整理。"),
        days=[],
    )
    return compose_trip_narrative(evidence, draft)


def build_trip_generation_prompt(snapshot: TripPlanSnapshot) -> str:
    context = build_trip_presentation_context(snapshot)
    return json.dumps(
        context.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def compose_trip_narrative(
    evidence: JoinedTripEvidence,
    draft: TripNarrativeDraft,
) -> TripNarrativePlan:
    map_evidence = evidence.map_weather.map
    weather = evidence.map_weather.weather
    if map_evidence is None or weather is None:
        raise TripItineraryGenerationError("地图与天气核心证据不可用。")

    weather_by_date = {day.date: day for day in weather.days}
    narrative_days: list[MapDayNarrative] = []
    for day_offset, evidence_day in enumerate(map_evidence.days):
        draft_day = draft.days[day_offset] if day_offset < len(draft.days) else None
        narrative_days.append(
            _compose_day_narrative(
                evidence,
                evidence_day,
                weather_by_date,
                draft_day,
            )
        )

    return TripNarrativePlan(
        title=draft.title,
        summary=draft.summary,
        days=narrative_days,
        practical_tips=list(draft.practical_tips),
        warnings=list(draft.warnings),
        transport_options=_evidence_options(
            evidence.capabilities.transport.enabled,
            evidence.transport,
        ),
        hotel_options=_evidence_options(
            evidence.capabilities.hotel.enabled,
            evidence.hotel,
        ),
    )


def _compose_day_narrative(
    evidence: JoinedTripEvidence,
    evidence_day: MapDayEvidence,
    weather_by_date: dict[date, DailyWeatherEvidence],
    draft_day: TripDayNarrativeDraft | None,
) -> MapDayNarrative:
    reasons = draft_day.recommendation_reasons if draft_day is not None else []
    return MapDayNarrative(
        day_index=evidence_day.day_index,
        date=evidence_day.date,
        theme=(
            draft_day.theme
            if draft_day is not None
            else _fallback_day_theme(evidence, evidence_day.day_index)
        ),
        places=[
            MapPlaceNarrative(
                reference_id=place.reference_id,
                recommendation_reason=(
                    reasons[place_offset]
                    if place_offset < len(reasons)
                    else _fallback_recommendation_reason(place)
                ),
            )
            for place_offset, place in enumerate(evidence_day.ordered_places())
        ],
        weather_advice=(
            build_weather_advice(weather_by_date[evidence_day.date])
            if evidence_day.date in weather_by_date
            else []
        ),
        tips=list(draft_day.tips) if draft_day is not None else [],
    )


def _fallback_day_theme(
    evidence: JoinedTripEvidence,
    day_index: int,
) -> str:
    city = evidence.request.core.destination_city or "目的地"
    return f"{city}第 {day_index} 天行程"


def _fallback_recommendation_reason(place: MapPlaceEvidence) -> str:
    if place.selection_reasons:
        detail = "；".join(place.selection_reasons)
        return f"后端筛选依据：{detail}"[:500]
    if place.matched_preferences:
        preferences = "、".join(str(item) for item in place.matched_preferences)
        return f"符合本次行程的{preferences}偏好，并纳入当天既定游览顺序。"
    return f"{place.name}已由后端纳入当天的既定游览顺序。"


def _evidence_options(enabled: bool, evidence: object) -> list[str]:
    status = getattr(evidence, "status", EvidenceStatus.FAILED)
    if not enabled or status is not EvidenceStatus.USABLE:
        return []
    return list(getattr(evidence, "display_options", []))


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
    "TRIP_MARKDOWN_SYSTEM_PROMPT",
    "TripItineraryGenerationError",
    "TripItineraryGenerator",
    "build_trip_narrative_skeleton",
    "build_trip_generation_prompt",
    "compose_trip_narrative",
]
