from __future__ import annotations

from collections.abc import Sequence

from app.schemas.chat import ChatMessage
from app.schemas.trip_capabilities import TripPlanningRequest
from app.services.city_trip_request import (
    apply_explicit_request_overrides,
    request_extraction_prompt,
)


def trip_request_extraction_prompt(messages: Sequence[ChatMessage]) -> str:
    return (
        f"{request_extraction_prompt(messages)}\n\n"
        "同时提取可选交通和酒店诉求。transport.action 与 hotel.action 只有在用户"
        "明确要求查询、查找、推荐或比较对应实时选项时才设为 enable；仅说明出发地、"
        "准备乘坐的交通方式、住宿区域或已有酒店时保持 unspecified。用户明确说不用查、"
        "已经订好或已经买好时设为 disable。enable 或 disable 时，evidence_text 必须"
        "逐字摘录最近一条用户消息中的最短相关片段；否则使用 unspecified 且"
        "evidence_text=null。不要猜测出发城市、交通方式、单双程、酒店日期或筛选条件。"
    )


def apply_trip_request_overrides(
    request: TripPlanningRequest,
    messages: Sequence[ChatMessage],
) -> tuple[TripPlanningRequest, dict[str, bool]]:
    updated = request.model_copy(deep=True)
    updated.core, overrides = apply_explicit_request_overrides(updated.core, messages)
    return updated, overrides


__all__ = [
    "apply_trip_request_overrides",
    "trip_request_extraction_prompt",
]
