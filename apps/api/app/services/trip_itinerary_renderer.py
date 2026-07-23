from __future__ import annotations

from app.schemas.trip_evidence import EvidenceStatus, JoinedTripEvidence, RawCapabilityEvidence
from app.schemas.trip_itinerary import TripNarrativePlan
from app.services.map_itinerary_renderer import (
    format_query_time,
    render_map_itinerary,
)

_UNIFIED_MAP_SCOPE_NOTE = (
    "> 本方案的地点、坐标、距离和路线只来自本次高德地图查询；景点筛选、分天与顺序由"
    "确定性规则完成，推荐理由、天气建议和已启用的可选结果由模型整理。"
)


def render_trip_itinerary(
    evidence: JoinedTripEvidence,
    narrative: TripNarrativePlan,
) -> str:
    map_evidence = evidence.map_weather.map
    weather = evidence.map_weather.weather
    if map_evidence is None or weather is None:
        raise ValueError("usable map and weather evidence are required for rendering")

    optional_enabled = (
        evidence.capabilities.transport.enabled or evidence.capabilities.hotel.enabled
    )
    if optional_enabled:
        markdown = render_map_itinerary(
            map_evidence,
            weather,
            narrative,
            scope_note=_UNIFIED_MAP_SCOPE_NOTE,
        )
    else:
        markdown = render_map_itinerary(map_evidence, weather, narrative)
    optional_sections = [
        *_render_optional_section(
            title="城际交通结果",
            label="交通",
            enabled=evidence.capabilities.transport.enabled,
            evidence=evidence.transport,
            options=narrative.transport_options,
        ),
        *_render_optional_section(
            title="酒店结果",
            label="酒店",
            enabled=evidence.capabilities.hotel.enabled,
            evidence=evidence.hotel,
            options=narrative.hotel_options,
        ),
    ]
    if not optional_sections:
        return markdown

    first_day_marker = "\n## 第 "
    before_days, marker, after_days = markdown.partition(first_day_marker)
    if not marker:
        return "\n".join([markdown, *optional_sections])
    return "\n".join(
        [
            before_days,
            *optional_sections,
            f"## 第 {after_days}",
        ]
    )


def _render_optional_section(
    *,
    title: str,
    label: str,
    enabled: bool,
    evidence: RawCapabilityEvidence,
    options: list[str],
) -> list[str]:
    if not enabled:
        return []
    lines = ["", f"## {title}", ""]
    if evidence.status is EvidenceStatus.USABLE:
        lines.extend(f"- {item}" for item in options)
        if not options:
            lines.append(f"- 已获取{label}查询结果，但暂时无法整理为可展示选项。")
    elif evidence.status is EvidenceStatus.EMPTY:
        lines.append(f"- 未查询到符合当前条件的{label}结果。")
    elif evidence.status is EvidenceStatus.FAILED:
        suffix = f"（{evidence.error_code}）" if evidence.error_code else ""
        lines.append(f"- {label}查询暂时失败{suffix}，地图与天气行程仍可继续使用。")
    else:
        lines.append(f"- 本次未执行{label}实时查询。")
    if evidence.status is not EvidenceStatus.SKIPPED:
        lines.append(f"- 数据来源：FlyAI；查询时间：{format_query_time(evidence.queried_at)}")
    return lines


__all__ = ["render_trip_itinerary"]
