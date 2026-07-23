from __future__ import annotations

from typing import cast

from app.schemas.map_planning import MapNarrativePlan
from app.schemas.trip_planning import DailyWeatherEvidence, TripWeatherEvidence

UNAVAILABLE_WEATHER_ADVICE = (
    "该日期暂无对应天气预报，请在临近出发时复查天气后再确认户外安排。"
)


def build_weather_advice(weather: DailyWeatherEvidence) -> list[str]:
    if weather.coverage == "unavailable":
        return [UNAVAILABLE_WEATHER_ADVICE]

    advice: list[str] = []
    phenomena = "".join(
        value for value in (weather.day_weather, weather.night_weather) if value
    )
    if "雷" in phenomena:
        advice.append("预报含雷电，请留意临近预警并减少开阔地带的户外活动。")
    if "雪" in phenomena:
        advice.append("预报含降雪，请注意防滑并为市内交通预留调整时间。")
    if "雨" in phenomena:
        advice.append("预报含降雨，建议携带雨具并为户外行程预留调整空间。")
    if any(marker in phenomena for marker in ("雾", "霾")):
        advice.append("预报含雾或霾，请关注能见度与空气质量并适当调整户外安排。")
    if "晴" in phenomena:
        advice.append("预报含晴天，户外活动请注意防晒。")

    day_temperature = _temperature_value(weather.day_temperature)
    night_temperature = _temperature_value(weather.night_temperature)
    temperatures = [
        value for value in (day_temperature, night_temperature) if value is not None
    ]
    if temperatures and max(temperatures) >= 30:
        advice.append("气温可能偏高，请避免长时间暴晒并及时补水。")
    if temperatures and min(temperatures) <= 10:
        advice.append("气温可能偏低，请准备保暖衣物。")
    if (
        day_temperature is not None
        and night_temperature is not None
        and abs(day_temperature - night_temperature) >= 8
    ):
        advice.append("昼夜温差较明显，建议分层穿着。")

    if not advice:
        advice.append("请结合白天与夜间预报安排穿着，并在出发前复查最新天气。")
    return advice


def normalize_weather_advice[PlanT: MapNarrativePlan](
    plan: PlanT,
    weather: TripWeatherEvidence,
) -> PlanT:
    weather_by_date = {day.date: day for day in weather.days}
    normalized_days = []
    for day in plan.days:
        weather_day = weather_by_date.get(day.date)
        advice = build_weather_advice(weather_day) if weather_day is not None else []
        normalized_days.append(day.model_copy(update={"weather_advice": advice}))
    return cast(PlanT, plan.model_copy(update={"days": normalized_days}))


def _temperature_value(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.strip().removesuffix("℃"))
    except ValueError:
        return None


__all__ = [
    "UNAVAILABLE_WEATHER_ADVICE",
    "build_weather_advice",
    "normalize_weather_advice",
]
