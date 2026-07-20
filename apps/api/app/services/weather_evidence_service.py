from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from app.clients.amap_errors import AmapError
from app.schemas.amap import WeatherInput, WeatherResult
from app.schemas.trip_planning import DailyWeatherEvidence, TripWeatherEvidence

logger = logging.getLogger(__name__)

_OUTSIDE_FORECAST_REASON = "该日期尚未进入高德天气预报覆盖范围。"
_QUERY_FAILED_REASON = "天气查询暂时不可用，请在出行前重新确认。"
_NOT_CONFIGURED_REASON = "天气服务未配置，请在出行前自行确认天气。"


class WeatherClient(Protocol):
    async def get_weather(self, query: WeatherInput) -> WeatherResult: ...


class WeatherEvidenceService:
    def __init__(self, client: WeatherClient | None) -> None:
        self._client = client

    async def collect(
        self,
        *,
        city: str,
        start_date: date,
        duration_days: int,
    ) -> TripWeatherEvidence:
        trip_dates = [start_date + timedelta(days=offset) for offset in range(duration_days)]
        if self._client is None:
            return _unavailable_weather(
                city=city,
                trip_dates=trip_dates,
                reason=_NOT_CONFIGURED_REASON,
                warning=_NOT_CONFIGURED_REASON,
            )

        try:
            result = await self._client.get_weather(WeatherInput(city=city, forecast=True))
        except asyncio.CancelledError:
            raise
        except AmapError as exc:
            logger.warning(
                "Amap weather evidence query failed error_code=%s",
                exc.error_code,
            )
            return _unavailable_weather(
                city=city,
                trip_dates=trip_dates,
                reason=_QUERY_FAILED_REASON,
                warning=_QUERY_FAILED_REASON,
            )
        except Exception as exc:
            logger.warning(
                "Weather evidence query failed exception_type=%s",
                type(exc).__name__,
            )
            return _unavailable_weather(
                city=city,
                trip_dates=trip_dates,
                reason=_QUERY_FAILED_REASON,
                warning=_QUERY_FAILED_REASON,
            )

        forecasts = {item.date: item for item in result.forecast}
        days: list[DailyWeatherEvidence] = []
        for trip_date in trip_dates:
            forecast = forecasts.get(trip_date)
            if forecast is None:
                days.append(
                    DailyWeatherEvidence(
                        date=trip_date,
                        coverage="unavailable",
                        unavailable_reason=_OUTSIDE_FORECAST_REASON,
                    )
                )
                continue
            days.append(
                DailyWeatherEvidence(
                    date=trip_date,
                    coverage="available",
                    day_weather=forecast.day_weather,
                    night_weather=forecast.night_weather,
                    day_temperature=forecast.day_temperature,
                    night_temperature=forecast.night_temperature,
                    day_wind_direction=forecast.day_wind_direction,
                    night_wind_direction=forecast.night_wind_direction,
                    day_wind_power=forecast.day_wind_power,
                    night_wind_power=forecast.night_wind_power,
                )
            )

        covered = sum(day.coverage == "available" for day in days)
        warnings: list[str] = []
        if covered == 0:
            warnings.append("高德天气预报尚未覆盖本次行程日期，请在临近出发时复查。")
        elif covered < len(days):
            warnings.append("高德天气预报仅覆盖本次行程的部分日期，未覆盖日期请稍后复查。")
        return TripWeatherEvidence(
            city=result.city or city,
            adcode=result.adcode or None,
            report_time=result.current.report_time or None,
            queried_at=datetime.now(UTC),
            days=days,
            warnings=warnings,
        )


def _unavailable_weather(
    *,
    city: str,
    trip_dates: list[date],
    reason: str,
    warning: str,
) -> TripWeatherEvidence:
    return TripWeatherEvidence(
        city=city,
        queried_at=datetime.now(UTC),
        days=[
            DailyWeatherEvidence(
                date=trip_date,
                coverage="unavailable",
                unavailable_reason=reason,
            )
            for trip_date in trip_dates
        ],
        warnings=[warning],
    )


__all__ = ["WeatherClient", "WeatherEvidenceService"]
