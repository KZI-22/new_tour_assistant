from __future__ import annotations

import datetime as dt

from app.schemas.map_planning import (
    MapNarrativePlan,
    MapPlaceEvidence,
    MapTripEvidence,
    RouteLegEvidence,
)
from app.schemas.trip_planning import DailyWeatherEvidence, TripWeatherEvidence

_BEIJING_TIMEZONE = dt.timezone(dt.timedelta(hours=8))


def render_map_itinerary(
    evidence: MapTripEvidence,
    weather: TripWeatherEvidence,
    narrative: MapNarrativePlan,
) -> str:
    narratives = {day.day_index: day for day in narrative.days}
    weather_days = {day.date: day for day in weather.days}
    lines = [
        f"# {narrative.title}",
        "",
        narrative.summary,
        "",
        "> 本方案的地点、坐标、距离和路线只来自本次高德地图查询；景点筛选、分天与顺序由"
        "确定性规则完成，推荐理由和天气建议由模型整理。"
        "本次未查询机票、火车、酒店、价格、库存、营业状态或预订信息。",
    ]

    for day in evidence.days:
        day_narrative = narratives[day.day_index]
        day_weather = weather_days[day.date]
        reasons = {item.reference_id: item.recommendation_reason for item in day_narrative.places}
        theme = f"｜{day_narrative.theme}" if day_narrative.theme else ""
        lines.extend(["", f"## 第 {day.day_index} 天｜{day.date.isoformat()}{theme}"])
        lines.extend(["", _render_weather(day_weather)])
        if day_narrative.weather_advice:
            lines.extend(["", "**天气建议**", ""])
            lines.extend(f"- {item}" for item in day_narrative.weather_advice)

        for stop_index, place in enumerate(day.attractions, start=1):
            lines.extend(["", f"### 第 {stop_index} 站｜{place.name}"])
            lines.extend(
                [
                    "",
                    reasons[place.reference_id],
                    "",
                    (
                        f"- 地址：{place.address or '高德未提供'}\n"
                        f"- 高德 POI ID：`{place.poi_id}`\n"
                        f"- POI 类型：{place.poi_type or '高德未提供'}\n"
                        f"- 召回依据：关键词“{place.search_query}”第 {place.search_rank} 条\n"
                        f"- 入选依据：{'；'.join(place.selection_reasons)}\n"
                        f"- 规划时长预算：约 {place.estimated_visit_minutes} 分钟"
                        "（按 POI 类型估算，不是景点官方建议）"
                    ),
                ]
            )

        if not day.attractions:
            lines.extend(["", "暂无有效高德景点，未使用模型猜测地点补足。"])
        lines.extend(
            [
                "",
                "**用餐与休息预留**",
                "",
                "- 当天已在总时长预算中预留午餐、晚餐与机动休息时间；未搜索或推荐具体餐厅。",
                (
                    f"- 景点预算约 {day.estimated_visit_minutes} 分钟；"
                    f"相邻交通约 {day.estimated_transport_minutes} 分钟。"
                ),
            ]
        )

        if day.route_legs:
            by_ref = {place.reference_id: place for place in day.ordered_places()}
            lines.extend(["", "**当日路段**", ""])
            lines.extend(_render_leg(leg, by_ref) for leg in day.route_legs)
        tips = [*day_narrative.tips, *day.warnings]
        if tips:
            lines.extend(["", "**当天提醒**", ""])
            lines.extend(f"- {item}" for item in dict.fromkeys(tips))

    if narrative.practical_tips:
        lines.extend(["", "## 实用提醒", ""])
        lines.extend(f"- {item}" for item in narrative.practical_tips)

    warnings = list(dict.fromkeys([*narrative.warnings, *evidence.warnings, *weather.warnings]))
    if warnings:
        lines.extend(["", "## 使用说明", ""])
        lines.extend(f"- {item}" for item in warnings)

    lines.extend(
        [
            "",
            "## 数据来源",
            "",
            f"- 地点与路线：高德地图；查询时间：{_format_query_time(evidence.queried_at)}",
            (
                f"- 天气：高德地图；adcode：{weather.adcode or '未返回'}；"
                f"报告时间：{weather.report_time or '供应商未提供'}；"
                f"查询时间：{_format_query_time(weather.queried_at)}"
            ),
        ]
    )
    return "\n".join(lines)


def _render_weather(weather: DailyWeatherEvidence) -> str:
    if weather.coverage == "unavailable":
        return f"**天气**：暂无对应日期预报。{weather.unavailable_reason or ''}".rstrip()
    return (
        f"**天气**：白天 {weather.day_weather or '未提供'} "
        f"{weather.day_temperature or '—'}℃；夜间 {weather.night_weather or '未提供'} "
        f"{weather.night_temperature or '—'}℃。"
    )


def _render_leg(
    leg: RouteLegEvidence,
    places: dict[str, MapPlaceEvidence],
) -> str:
    origin = places[leg.origin_ref].name
    destination = places[leg.destination_ref].name
    modes = {
        "walking": "步行",
        "transit": "公交",
        "driving": "打车",
        "estimated": "直线距离估算",
        "unverified": "未验证",
    }
    facts: list[str] = [modes[leg.mode]]
    if leg.distance_meters is not None:
        facts.append(_format_distance(leg.distance_meters))
    if leg.duration_seconds is not None:
        facts.append(_format_duration(leg.duration_seconds))
    if leg.transfer_count is not None:
        facts.append(f"换乘 {leg.transfer_count} 次")
    summary = f"；{leg.route_summary}" if leg.route_summary else ""
    return f"- {origin} → {destination}：{'，'.join(facts)}{summary}"


def _format_distance(value: int) -> str:
    if value >= 1_000:
        return f"{value / 1_000:.1f} 公里"
    return f"{value} 米"


def _format_duration(value: int) -> str:
    minutes = max(1, round(value / 60))
    if minutes >= 60:
        return f"约 {minutes // 60} 小时 {minutes % 60} 分钟"
    return f"约 {minutes} 分钟"


def _format_query_time(value: dt.datetime) -> str:
    local_time = value.astimezone(_BEIJING_TIMEZONE)
    return f"{local_time:%Y-%m-%d %H:%M:%S}（北京时间）"


__all__ = ["render_map_itinerary"]
