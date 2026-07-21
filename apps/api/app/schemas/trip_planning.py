from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TripPlanningModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TripPreference(StrEnum):
    HISTORY_CULTURE = "历史文化"
    MUSEUM_EXHIBITION = "博物馆展览"
    NATURAL_SCENERY = "自然风光"
    CITY_LANDMARK = "城市地标"
    CHARACTERISTIC_DISTRICT = "特色街区"
    PHOTOGRAPHY = "摄影打卡"
    FAMILY = "亲子游"
    LEISURE = "休闲慢游"
    NIGHT_VIEW = "夜景体验"


_PREFERENCE_ALIASES = {
    "历史": TripPreference.HISTORY_CULTURE,
    "文化": TripPreference.HISTORY_CULTURE,
    "人文": TripPreference.HISTORY_CULTURE,
    "古迹": TripPreference.HISTORY_CULTURE,
    "园林": TripPreference.HISTORY_CULTURE,
    "博物馆": TripPreference.MUSEUM_EXHIBITION,
    "展览": TripPreference.MUSEUM_EXHIBITION,
    "美术馆": TripPreference.MUSEUM_EXHIBITION,
    "自然": TripPreference.NATURAL_SCENERY,
    "山水": TripPreference.NATURAL_SCENERY,
    "风光": TripPreference.NATURAL_SCENERY,
    "地标": TripPreference.CITY_LANDMARK,
    "街区": TripPreference.CHARACTERISTIC_DISTRICT,
    "步行街": TripPreference.CHARACTERISTIC_DISTRICT,
    "摄影": TripPreference.PHOTOGRAPHY,
    "打卡": TripPreference.PHOTOGRAPHY,
    "亲子": TripPreference.FAMILY,
    "儿童": TripPreference.FAMILY,
    "休闲": TripPreference.LEISURE,
    "慢游": TripPreference.LEISURE,
    "夜景": TripPreference.NIGHT_VIEW,
    "夜游": TripPreference.NIGHT_VIEW,
}


class CityTripRequest(TripPlanningModel):
    destination_city: str | None = Field(default=None, max_length=80)
    duration_days: int | None = Field(default=None, ge=1)
    start_date: date | None = None
    interests: list[TripPreference] = Field(default_factory=list, max_length=9)
    food_preferences: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("destination_city")
    @classmethod
    def normalize_destination_city(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("interests", mode="before")
    @classmethod
    def normalize_interests(cls, value: object) -> list[TripPreference]:
        if not isinstance(value, (list, tuple)):
            return []
        normalized: list[TripPreference] = []
        for item in value:
            text = str(item).strip()
            if not text:
                continue
            try:
                preference = TripPreference(text)
            except ValueError:
                preference = next(
                    (tag for marker, tag in _PREFERENCE_ALIASES.items() if marker in text),
                    None,
                )
            if preference is not None and preference not in normalized:
                normalized.append(preference)
        return normalized

    @field_validator("food_preferences")
    @classmethod
    def normalize_food_preferences(cls, value: list[str]) -> list[str]:
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
    "TripPreference",
]
