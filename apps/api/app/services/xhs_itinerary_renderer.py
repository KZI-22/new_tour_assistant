from __future__ import annotations

from app.schemas.xhs_planning import XhsItineraryPlan

_TIME_LABELS = {
    "morning": "上午",
    "afternoon": "下午",
    "evening": "晚上",
    "flexible": "灵活安排",
}


def render_xhs_itinerary(plan: XhsItineraryPlan) -> str:
    source_numbers = {
        source.reference_id: index for index, source in enumerate(plan.sources, start=1)
    }
    lines = [
        f"# {plan.title}",
        "",
        plan.summary,
        "",
        "> 本方案根据小红书搜索页首次加载结果中的高点赞笔记整理，不代表平台全部内容。"
        "本次未查询机票、火车票、酒店库存或实时价格。",
    ]

    for day in plan.days:
        lines.extend(["", f"## 第 {day.day_index} 天｜{day.theme}"])
        for activity in day.activities:
            refs = [
                source_numbers[reference]
                for reference in activity.source_refs
                if reference in source_numbers
            ]
            source_suffix = f"（参考来源 {', '.join(str(item) for item in refs)}）" if refs else ""
            lines.extend(
                [
                    "",
                    (
                        f"### {_TIME_LABELS[activity.time_of_day]} · "
                        f"{activity.place_name}{source_suffix}"
                    ),
                    "",
                    activity.description,
                ]
            )
        if day.meal_suggestions:
            lines.extend(["", "**餐饮建议**", ""])
            lines.extend(f"- {item}" for item in day.meal_suggestions)
        if day.tips:
            lines.extend(["", "**当天提醒**", ""])
            lines.extend(f"- {item}" for item in day.tips)

    if plan.practical_tips:
        lines.extend(["", "## 实用提醒", ""])
        lines.extend(f"- {item}" for item in plan.practical_tips)

    if plan.warnings:
        lines.extend(["", "## 使用说明", ""])
        lines.extend(f"- {item}" for item in plan.warnings)

    lines.extend(["", "## 参考的小红书笔记", ""])
    for index, source in enumerate(plan.sources, start=1):
        published = f"，发布于 {source.published_at}" if source.published_at else ""
        role = "主帖" if source.role == "primary" else "补充"
        likes = (
            f"，点赞 {_format_liked_count(source.liked_count)}"
            if source.liked_count is not None
            else "，点赞量未知"
        )
        lines.append(
            f"{index}. [{role}]《{source.title}》— {source.author_name}{likes}{published}"
        )
    lines.extend(
        [
            "",
            "小红书笔记带有作者的主观体验；营业状态、预约规则和现场情况请在出行前再次确认。",
        ]
    )
    return "\n".join(lines)


def _format_liked_count(value: int) -> str:
    if value >= 10_000:
        return f"{_compact_number(value / 10_000)} 万"
    if value >= 1_000:
        return f"{_compact_number(value / 1_000)} 千"
    return str(value)


def _compact_number(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


__all__ = ["render_xhs_itinerary"]
