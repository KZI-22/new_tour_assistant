from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TripPlanningModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CityTripRequest(TripPlanningModel):
    destination_city: str | None = Field(default=None, max_length=80)
    duration_days: int | None = Field(default=None, ge=1)
    start_date: date | None = None
    interests: list[str] = Field(default_factory=list, max_length=10)
    food_preferences: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("destination_city")
    @classmethod
    def normalize_destination_city(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("interests", "food_preferences")
    @classmethod
    def normalize_preferences(cls, value: list[str]) -> list[str]:
        normalized = (item.strip() for item in value)
        return list(dict.fromkeys(item for item in normalized if item))


class CityTripRequestExtraction(TripPlanningModel):
    request: CityTripRequest


class DailyWeatherEvidence(TripPlanningModel):
    date: date
    coverage: Literal["available", "unavailable"]
    day_weather: str | None = None
    night_weather: str | None = None
    day_temperature: str | None = None
    night_temperature: str | None = None
    day_wind_direction: str | None = None
    night_wind_direction: str | None = None
    day_wind_power: str | None = None
    night_wind_power: str | None = None
    unavailable_reason: str | None = None


class TripWeatherEvidence(TripPlanningModel):
    provider: Literal["amap"] = "amap"
    city: str
    adcode: str | None = None
    report_time: str | None = None
    queried_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    days: list[DailyWeatherEvidence]
    warnings: list[str] = Field(default_factory=list)


PlanningSource = Literal["standard", "xhs"]


__all__ = [
    "CityTripRequest",
    "CityTripRequestExtraction",
    "DailyWeatherEvidence",
    "PlanningSource",
    "TripWeatherEvidence",
]
