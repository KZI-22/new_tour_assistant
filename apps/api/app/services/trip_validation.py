from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, time, timedelta

from app.schemas.itinerary import (
    HotelOption,
    ItineraryPlan,
    TransportOption,
    TripRequest,
    ValidationIssue,
)

_BAD_WEATHER = ("暴雨", "大雨", "雷暴", "台风", "暴雪", "大雪", "冰雹", "沙尘")
_PACE_LIMITS = {"relaxed": 3, "moderate": 4, "packed": 5}


def validate_itinerary(
    plan: ItineraryPlan,
    request: TripRequest,
    *,
    transport_options: Iterable[TransportOption] = (),
    hotel_options: Iterable[HotelOption] = (),
    known_poi_ids: Iterable[str] = (),
    max_daily_activities: int = 5,
    route_data_available: bool = True,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    _validate_dates(plan, issues)
    _validate_arrival_and_return(plan, issues)
    _validate_days(plan, request, issues, max_daily_activities=max_daily_activities)
    _validate_hotel(plan, issues)
    _validate_budget(plan, request, issues)
    _validate_sources(
        plan,
        issues,
        transport_options=list(transport_options),
        hotel_options=list(hotel_options),
        known_poi_ids=set(known_poi_ids),
    )
    if not route_data_available and any(len(day.activities) > 1 for day in plan.days):
        issues.append(
            ValidationIssue(
                code="ROUTE_DATA_MISSING",
                severity="warning",
                message="部分地点间的实时路线或耗时未能取得，日程衔接时间需要出行前复核。",
                suggested_action="取得路线数据后重新核对每日交通时间。",
            )
        )
    return issues


def _validate_dates(plan: ItineraryPlan, issues: list[ValidationIssue]) -> None:
    expected = _date_range(plan.start_date, plan.end_date)
    actual = [day.date for day in plan.days]
    if actual != expected:
        issues.append(
            ValidationIssue(
                code="DAY_DATES_NOT_CONTIGUOUS",
                severity="error",
                message="每日行程日期不连续或没有完整覆盖旅行日期。",
                suggested_action="按旅行起止日期重新生成连续的每日计划。",
            )
        )
    for expected_index, day in enumerate(plan.days, start=1):
        if day.day_index != expected_index:
            issues.append(
                ValidationIssue(
                    code="DAY_INDEX_INVALID",
                    severity="error",
                    message=f"第 {expected_index} 天的 day_index 不正确。",
                    day_index=expected_index,
                )
            )


def _validate_arrival_and_return(
    plan: ItineraryPlan,
    issues: list[ValidationIssue],
) -> None:
    if plan.days and plan.outbound_transport and plan.outbound_transport.arrival_time:
        arrival = plan.outbound_transport.arrival_time
        first = plan.days[0]
        for index, activity in enumerate(first.activities):
            if _activity_before(activity.start_time, first.date, arrival):
                issues.append(
                    ValidationIssue(
                        code="ACTIVITY_BEFORE_ARRIVAL",
                        severity="error",
                        message=f"第一天的“{activity.place_name}”安排在抵达之前。",
                        day_index=first.day_index,
                        activity_index=index,
                        suggested_action="将活动移到抵达并预留进城时间之后。",
                    )
                )
    if plan.days and plan.return_transport and plan.return_transport.departure_time:
        departure = plan.return_transport.departure_time
        last = plan.days[-1]
        for index, activity in enumerate(last.activities):
            activity_time = activity.end_time or activity.start_time
            if _activity_after(activity_time, last.date, departure):
                issues.append(
                    ValidationIssue(
                        code="ACTIVITY_AFTER_DEPARTURE",
                        severity="error",
                        message=f"最后一天的“{activity.place_name}”晚于返程出发时间。",
                        day_index=last.day_index,
                        activity_index=index,
                        suggested_action="提前结束活动并预留前往车站或机场的时间。",
                    )
                )


def _validate_days(
    plan: ItineraryPlan,
    request: TripRequest,
    issues: list[ValidationIssue],
    *,
    max_daily_activities: int,
) -> None:
    seen_names: dict[str, tuple[int, int]] = {}
    seen_pois: dict[str, tuple[int, int]] = {}
    pace_limit = min(_PACE_LIMITS.get(request.pace or "moderate", 4), max_daily_activities)

    for day in plan.days:
        if len(day.activities) > pace_limit:
            issues.append(
                ValidationIssue(
                    code="TOO_MANY_DAILY_ACTIVITIES",
                    severity="error",
                    message=(
                        f"第 {day.day_index} 天有 {len(day.activities)} 个主要活动，"
                        f"超过当前节奏上限 {pace_limit}。"
                    ),
                    day_index=day.day_index,
                    suggested_action="减少活动或拆分到其他日期。",
                )
            )

        timed = [
            (index, activity)
            for index, activity in enumerate(day.activities)
            if activity.start_time is not None and activity.end_time is not None
        ]
        timed.sort(key=lambda item: item[1].start_time or time.min)
        for (_left_index, left), (right_index, right) in zip(timed, timed[1:], strict=False):
            if left.end_time and right.start_time and left.end_time > right.start_time:
                issues.append(
                    ValidationIssue(
                        code="ACTIVITY_TIME_OVERLAP",
                        severity="error",
                        message=(
                            f"第 {day.day_index} 天的“{left.place_name}”"
                            f"与“{right.place_name}”时间重叠。"
                        ),
                        day_index=day.day_index,
                        activity_index=right_index,
                        suggested_action="调整开始时间或减少一个活动。",
                    )
                )

        activity_minutes = sum(
            activity.estimated_duration_minutes
            or _clock_duration(activity.start_time, activity.end_time)
            for activity in day.activities
        )
        if activity_minutes + day.estimated_transport_time_minutes > 12 * 60:
            issues.append(
                ValidationIssue(
                    code="DAILY_SCHEDULE_TOO_LONG",
                    severity="error",
                    message=f"第 {day.day_index} 天活动与交通合计超过 12 小时。",
                    day_index=day.day_index,
                    suggested_action="压缩活动或增加休息时间。",
                )
            )
        if len(day.activities) > 1 and day.estimated_transport_time_minutes <= 0:
            issues.append(
                ValidationIssue(
                    code="TRANSPORT_TIME_NOT_INCLUDED",
                    severity="error",
                    message=f"第 {day.day_index} 天有多个地点，但没有计入地点间交通时间。",
                    day_index=day.day_index,
                    suggested_action="使用路线结果补充市内交通耗时并调整活动时间。",
                )
            )
        if any(activity.poi_id and not activity.coordinates for activity in day.activities):
            issues.append(
                ValidationIssue(
                    code="ACTIVITY_COORDINATES_MISSING",
                    severity="warning",
                    message=f"第 {day.day_index} 天部分 POI 缺少可核验坐标。",
                    day_index=day.day_index,
                    suggested_action="出发前确认准确入口与导航坐标。",
                )
            )

        bad_weather = day.weather_summary and any(
            marker in day.weather_summary for marker in _BAD_WEATHER
        )
        if bad_weather and any(activity.indoor is False for activity in day.activities):
            issues.append(
                ValidationIssue(
                    code="OUTDOOR_ACTIVITY_BAD_WEATHER",
                    severity="warning",
                    message=f"第 {day.day_index} 天存在恶劣天气预报，但仍安排了室外活动。",
                    day_index=day.day_index,
                    suggested_action="准备室内备选方案并在出发前复核天气。",
                )
            )

        for activity_index, activity in enumerate(day.activities):
            normalized_name = activity.place_name.casefold()
            if normalized_name in seen_names:
                issues.append(
                    ValidationIssue(
                        code="DUPLICATE_ACTIVITY",
                        severity="error",
                        message=f"景点“{activity.place_name}”在行程中重复出现。",
                        day_index=day.day_index,
                        activity_index=activity_index,
                        suggested_action="保留一次并替换重复活动。",
                    )
                )
            else:
                seen_names[normalized_name] = (day.day_index, activity_index)
            if activity.poi_id:
                if activity.poi_id in seen_pois:
                    issues.append(
                        ValidationIssue(
                            code="DUPLICATE_POI",
                            severity="error",
                            message=f"POI“{activity.place_name}”在行程中重复出现。",
                            day_index=day.day_index,
                            activity_index=activity_index,
                        )
                    )
                else:
                    seen_pois[activity.poi_id] = (day.day_index, activity_index)


def _validate_hotel(plan: ItineraryPlan, issues: list[ValidationIssue]) -> None:
    if plan.hotel is None:
        return
    if plan.hotel.check_in_date > plan.start_date or plan.hotel.check_out_date < plan.end_date:
        issues.append(
            ValidationIssue(
                code="HOTEL_DATES_INCOMPLETE",
                severity="error",
                message="酒店入住与退房日期没有覆盖全部住宿日期。",
                suggested_action="重新查询覆盖旅行日期的酒店。",
            )
        )


def _validate_budget(
    plan: ItineraryPlan,
    request: TripRequest,
    issues: list[ValidationIssue],
) -> None:
    if request.total_budget is None or plan.budget is None:
        return
    total = plan.budget.total_estimated_cost
    if total is not None and total > request.total_budget:
        issues.append(
            ValidationIssue(
                code="OVER_BUDGET",
                severity="error",
                message=f"预计总费用 {total:.0f} 元超过用户预算 {request.total_budget:.0f} 元。",
                suggested_action="优先调整交通、酒店或收费活动。",
            )
        )


def _validate_sources(
    plan: ItineraryPlan,
    issues: list[ValidationIssue],
    *,
    transport_options: list[TransportOption],
    hotel_options: list[HotelOption],
    known_poi_ids: set[str],
) -> None:
    for field_name, selected in (
        ("outbound", plan.outbound_transport),
        ("return", plan.return_transport),
    ):
        if selected is None:
            continue
        if not any(_same_transport(selected, option) for option in transport_options):
            issues.append(
                ValidationIssue(
                    code="UNVERIFIED_TRANSPORT_FACT",
                    severity="error",
                    message=f"{field_name} 交通方案包含工具结果中无法核验的班次或价格。",
                    suggested_action="只选择工具返回的交通方案；没有结果时保留为空。",
                )
            )
    if plan.hotel is not None and not any(
        _same_hotel(plan.hotel, option) for option in hotel_options
    ):
        issues.append(
            ValidationIssue(
                code="UNVERIFIED_HOTEL_FACT",
                severity="error",
                message="住宿方案包含工具结果中无法核验的酒店或价格。",
                suggested_action="只选择工具返回的酒店；没有实时结果时保留为空。",
            )
        )
    if known_poi_ids:
        for day in plan.days:
            for index, activity in enumerate(day.activities):
                if activity.poi_id and activity.poi_id not in known_poi_ids:
                    issues.append(
                        ValidationIssue(
                            code="UNVERIFIED_POI",
                            severity="error",
                            message=f"“{activity.place_name}”的 POI ID 不在工具结果中。",
                            day_index=day.day_index,
                            activity_index=index,
                            suggested_action="改用已查询到的 POI。",
                        )
                    )


def _same_transport(left: TransportOption, right: TransportOption) -> bool:
    identifiers = (
        left.flight_number and left.flight_number == right.flight_number,
        left.train_number and left.train_number == right.train_number,
        left.source_reference and left.source_reference == right.source_reference,
    )
    return left.source_tool == right.source_tool and any(identifiers)


def _same_hotel(left: HotelOption, right: HotelOption) -> bool:
    return (
        left.source_tool == right.source_tool
        and (
            (left.poi_id is not None and left.poi_id == right.poi_id)
            or left.name.casefold() == right.name.casefold()
        )
        and (left.nightly_price is None or left.nightly_price == right.nightly_price)
    )


def _date_range(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _activity_before(value: time | None, day: date, boundary: datetime) -> bool:
    return value is not None and datetime.combine(day, value, boundary.tzinfo) < boundary


def _activity_after(value: time | None, day: date, boundary: datetime) -> bool:
    return value is not None and datetime.combine(day, value, boundary.tzinfo) > boundary


def _clock_duration(start: time | None, end: time | None) -> int:
    if start is None or end is None:
        return 0
    return max(
        0,
        round(
            (datetime.combine(date.min, end) - datetime.combine(date.min, start)).total_seconds()
            / 60
        ),
    )


__all__ = ["validate_itinerary"]
