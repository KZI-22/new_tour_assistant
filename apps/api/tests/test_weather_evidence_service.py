from __future__ import annotations

from datetime import date

import pytest
from app.clients.amap_errors import AmapRequestError
from app.schemas.amap import CurrentWeather, WeatherForecast, WeatherResult
from app.services.weather_evidence_service import WeatherEvidenceService


class FakeWeatherClient:
    def __init__(self, result: WeatherResult | Exception) -> None:
        self.result = result
        self.calls = 0

    async def get_weather(self, query: object) -> WeatherResult:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def weather_result(*forecast_dates: date) -> WeatherResult:
    return WeatherResult(
        city="成都",
        adcode="510100",
        province="四川",
        current=CurrentWeather(
            weather="晴",
            temperature="31",
            humidity="60",
            wind_direction="南",
            wind_power="3",
            report_time="2026-07-20 10:00:00",
        ),
        forecast=[
            WeatherForecast(
                date=item,
                day_weather="晴",
                night_weather="多云",
                day_temperature="32",
                night_temperature="23",
                day_wind_direction="南",
                night_wind_direction="南",
                day_wind_power="3",
                night_wind_power="2",
            )
            for item in forecast_dates
        ],
    )


@pytest.mark.asyncio
async def test_weather_evidence_maps_forecasts_by_exact_date() -> None:
    client = FakeWeatherClient(
        weather_result(date(2026, 7, 25), date(2026, 7, 27), date(2026, 7, 26))
    )

    evidence = await WeatherEvidenceService(client).collect(
        city="成都",
        start_date=date(2026, 7, 25),
        duration_days=3,
    )

    assert client.calls == 1
    assert [day.date for day in evidence.days] == [
        date(2026, 7, 25),
        date(2026, 7, 26),
        date(2026, 7, 27),
    ]
    assert [day.coverage for day in evidence.days] == ["available"] * 3
    assert evidence.adcode == "510100"
    assert evidence.report_time == "2026-07-20 10:00:00"


@pytest.mark.asyncio
async def test_weather_evidence_marks_partial_and_missing_dates_unavailable() -> None:
    client = FakeWeatherClient(weather_result(date(2026, 7, 26)))

    evidence = await WeatherEvidenceService(client).collect(
        city="成都",
        start_date=date(2026, 7, 25),
        duration_days=3,
    )

    assert [day.coverage for day in evidence.days] == [
        "unavailable",
        "available",
        "unavailable",
    ]
    assert evidence.days[0].day_weather is None
    assert evidence.days[0].unavailable_reason
    assert len(evidence.warnings) == 1


@pytest.mark.asyncio
async def test_weather_evidence_does_not_substitute_current_weather() -> None:
    client = FakeWeatherClient(weather_result())

    evidence = await WeatherEvidenceService(client).collect(
        city="成都",
        start_date=date(2026, 8, 25),
        duration_days=2,
    )

    assert all(day.coverage == "unavailable" for day in evidence.days)
    assert all(day.day_weather is None for day in evidence.days)
    assert "尚未覆盖" in evidence.warnings[0]


@pytest.mark.asyncio
async def test_weather_evidence_sanitizes_provider_failure() -> None:
    client = FakeWeatherClient(AmapRequestError("secret URL and key must not leak"))

    evidence = await WeatherEvidenceService(client).collect(
        city="成都",
        start_date=date(2026, 7, 25),
        duration_days=2,
    )

    dumped = evidence.model_dump_json()
    assert all(day.coverage == "unavailable" for day in evidence.days)
    assert "secret" not in dumped
    assert "key" not in dumped
    assert client.calls == 1


@pytest.mark.asyncio
async def test_weather_evidence_handles_unconfigured_client_without_query() -> None:
    evidence = await WeatherEvidenceService(None).collect(
        city="成都",
        start_date=date(2026, 7, 25),
        duration_days=1,
    )

    assert evidence.days[0].coverage == "unavailable"
    assert "未配置" in (evidence.days[0].unavailable_reason or "")
