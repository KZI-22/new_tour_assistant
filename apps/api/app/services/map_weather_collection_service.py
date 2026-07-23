from __future__ import annotations

import asyncio
from datetime import date, timedelta

from app.schemas.trip_evidence import MapWeatherEvidenceBundle
from app.schemas.trip_planning import CityTripRequest, TripWeatherEvidence
from app.services.attraction_planning_service import match_weather_to_days
from app.services.map_trip_collection_service import (
    MapTripCollectionError,
    MapTripCollectionService,
)
from app.services.weather_evidence_service import WeatherEvidenceService


class MapWeatherCollectionService:
    def __init__(
        self,
        map_service: MapTripCollectionService,
        weather_service: WeatherEvidenceService,
        *,
        weather_timeout_seconds: float,
    ) -> None:
        if weather_timeout_seconds <= 0:
            raise ValueError("weather_timeout_seconds must be positive")
        self._map_service = map_service
        self._weather_service = weather_service
        self._weather_timeout_seconds = weather_timeout_seconds

    async def collect(self, request: CityTripRequest) -> MapWeatherEvidenceBundle:
        if (
            request.destination_city is None
            or request.start_date is None
            or request.duration_days is None
        ):
            return MapWeatherEvidenceBundle(
                status="failed",
                warnings=["地图与天气查询缺少必要的城市、日期或天数。"],
                error_code="MAP_REQUIREMENTS_INCOMPLETE",
            )

        map_task = asyncio.create_task(self._map_service.collect(request))
        weather_task = asyncio.create_task(
            self._collect_weather_with_timeout(
                city=request.destination_city,
                start_date=request.start_date,
                duration_days=request.duration_days,
            )
        )
        try:
            map_evidence, weather = await asyncio.gather(map_task, weather_task)
        except asyncio.CancelledError:
            await _cancel_tasks(map_task, weather_task)
            raise
        except MapTripCollectionError as exc:
            await _cancel_tasks(map_task, weather_task)
            return MapWeatherEvidenceBundle(
                status="failed",
                warnings=[exc.user_message],
                error_code=exc.code,
            )
        except Exception:
            await _cancel_tasks(map_task, weather_task)
            raise

        map_evidence = match_weather_to_days(map_evidence, weather)
        warnings = list(dict.fromkeys([*map_evidence.warnings, *weather.warnings]))
        weather_complete = len(weather.days) == request.duration_days and all(
            day.coverage == "available" for day in weather.days
        )
        return MapWeatherEvidenceBundle(
            status="usable" if not warnings and weather_complete else "partial",
            map=map_evidence,
            weather=weather,
            warnings=warnings,
        )

    async def _collect_weather_with_timeout(
        self,
        *,
        city: str,
        start_date: date,
        duration_days: int,
    ) -> TripWeatherEvidence:
        try:
            return await asyncio.wait_for(
                self._weather_service.collect(
                    city=city,
                    start_date=start_date,
                    duration_days=duration_days,
                ),
                timeout=self._weather_timeout_seconds,
            )
        except TimeoutError:
            reason = "天气查询达到数据阶段超时，请在临近出发时重新确认。"
            return TripWeatherEvidence(
                city=city,
                days=[
                    {
                        "date": start_date + timedelta(days=offset),
                        "coverage": "unavailable",
                        "unavailable_reason": reason,
                    }
                    for offset in range(duration_days)
                ],
                warnings=[reason],
            )


async def _cancel_tasks(*tasks: asyncio.Task[object]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


__all__ = ["MapWeatherCollectionService"]
