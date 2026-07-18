from __future__ import annotations

from app.schemas.itinerary import ItineraryPlan


def render_itinerary(plan: ItineraryPlan, *, change_summary: str | None = None) -> str:
    lines = [f"# {plan.title}", "", "## 行程概览"]
    lines.append(f"- 目的地：{plan.destination}")
    lines.append(f"- 日期：{plan.start_date.isoformat()} 至 {plan.end_date.isoformat()}")
    if plan.origin:
        lines.append(f"- 出发地：{plan.origin}")
    if plan.readiness == "blocked":
        lines.extend(
            [
                "",
                "> ⚠️ 当前仅为未完成草案：关键交通、路线或预算校验未通过，不能直接作为可执行行程。",
            ]
        )
    elif plan.readiness == "partial":
        lines.extend(["", "> 当前方案使用了降级或不完整数据，请结合注意事项复核后再执行。"])
    if change_summary:
        lines.extend(["", f"> 本次修改：{change_summary}"])

    lines.extend(["", "## 交通建议"])
    lines.extend(_transport_lines("去程", plan.outbound_transport))
    lines.extend(_transport_lines("返程", plan.return_transport))

    lines.extend(["", "## 住宿建议"])
    if plan.hotel:
        hotel = plan.hotel
        lines.append(f"- {hotel.name}（{hotel.check_in_date} 入住，{hotel.check_out_date} 退房）")
        if hotel.address:
            lines.append(f"  - 地址：{hotel.address}")
        if hotel.nightly_price is not None:
            lines.append(f"  - 查询时每晚参考价格：约 ¥{hotel.nightly_price:.0f}")
            if hotel.queried_at:
                lines.append(f"  - 查询时间：{hotel.queried_at.isoformat()}")
        else:
            lines.append("  - 实时价格：未取得，请预订前重新查询")
    else:
        lines.append("- 暂未取得可核验的实时住宿方案，住宿费用未按真实报价计入。")

    lines.extend(["", "## 每日行程"])
    for day in plan.days:
        heading = f"### 第 {day.day_index} 天 · {day.date.isoformat()}"
        if day.theme:
            heading += f" · {day.theme}"
        lines.extend([heading, ""])
        if not day.activities:
            lines.append("- 预留休息或自由活动时间。")
        for activity in day.activities:
            period = ""
            if activity.start_time:
                period = activity.start_time.strftime("%H:%M")
                if activity.end_time:
                    period += f"–{activity.end_time.strftime('%H:%M')}"
                period += " "
            lines.append(f"- {period}{activity.place_name}：{activity.activity_type}")
            if activity.notes:
                lines.append(f"  - {activity.notes}")
        if day.estimated_transport_time_minutes is None:
            lines.append("- 市内交通时间：未取得覆盖全部相邻地点的可靠路线数据")
        else:
            lines.append(
                f"- 已核验路段汇总交通时间：约 {day.estimated_transport_time_minutes} 分钟"
            )
        if day.weather_summary:
            lines.append(f"- 天气预报：{day.weather_summary}")
        for warning in day.warnings:
            lines.append(f"- 注意：{warning}")
        lines.append("")

    lines.append("## 预算估算")
    if plan.budget:
        budget = plan.budget
        _append_money(lines, "工具查询交通费用", budget.transport_cost)
        _append_money(lines, "工具查询住宿费用", budget.hotel_cost)
        _append_money(lines, "活动费用估算", budget.activity_cost)
        _append_money(lines, "市内交通经验估算", budget.local_transport_cost)
        _append_money(lines, "餐饮经验估算", budget.food_estimate)
        _append_money(lines, "预计合计", budget.total_estimated_cost)
        if budget.user_budget is not None:
            lines.append(f"- 用户预算：¥{budget.user_budget:.0f}")
        for assumption in budget.assumptions:
            lines.append(f"- 估算假设：{assumption}")
    else:
        lines.append("- 暂无足够数据生成可靠的预算汇总。")

    lines.extend(["", "## 天气提醒"])
    weather_lines = [
        f"- 第 {day.day_index} 天：{day.weather_summary}"
        for day in plan.days
        if day.weather_summary
    ]
    lines.extend(weather_lines or ["- 当前没有覆盖旅行日期的准确天气预报，请临近出发时复核。"])

    lines.extend(["", "## 方案假设"])
    lines.extend([f"- {item}" for item in plan.assumptions] or ["- 未记录额外默认假设。"])
    degraded_diagnostics = [
        item for item in plan.diagnostics if item.severity in {"warning", "error"}
    ]
    if degraded_diagnostics:
        lines.extend(["", "## 数据质量"])
        lines.extend(
            f"- [{item.code}] {item.message}"
            for item in degraded_diagnostics
        )
    lines.extend(["", "## 注意事项"])
    lines.extend(
        [f"- {item}" for item in plan.warnings]
        or ["- 价格、余票、天气和路线耗时会变化，请在出行前再次确认。"]
    )
    return "\n".join(lines).rstrip() + "\n"


def _transport_lines(label: str, option: object) -> list[str]:
    if option is None:
        return [f"- {label}：暂未取得可核验的实时交通方案。"]
    transport = option
    number = transport.flight_number or transport.train_number or "班次未提供"
    line = f"- {label}：{transport.transport_type} {number}"
    if transport.departure_time and transport.arrival_time:
        line += f"，{transport.departure_time:%m-%d %H:%M}–{transport.arrival_time:%H:%M}"
    if transport.price is not None:
        line += f"，工具查询价格约 ¥{transport.price:.0f}"
    return [line]


def _append_money(lines: list[str], label: str, value: float | None) -> None:
    lines.append(f"- {label}：{'未取得' if value is None else f'¥{value:.0f}'}")


__all__ = ["render_itinerary"]
