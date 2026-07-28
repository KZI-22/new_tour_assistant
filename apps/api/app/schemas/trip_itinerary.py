from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.map_planning import MapNarrativePlan

NarrativeReason = Annotated[str, Field(min_length=1, max_length=120)]
NarrativeTip = Annotated[str, Field(min_length=1, max_length=300)]


class TripDayNarrativeDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    theme: str = Field(min_length=1, max_length=200)
    recommendation_reasons: list[NarrativeReason] = Field(max_length=5)
    tips: list[NarrativeTip] = Field(default_factory=list, max_length=3)


class TripNarrativeDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=300)
    days: list[TripDayNarrativeDraft] = Field(max_length=5)
    practical_tips: list[NarrativeTip] = Field(default_factory=list, max_length=5)
    warnings: list[NarrativeTip] = Field(default_factory=list, max_length=5)


class TripNarrativePlan(MapNarrativePlan):
    transport_options: list[str] = Field(default_factory=list)
    hotel_options: list[str] = Field(default_factory=list)


__all__ = [
    "TripDayNarrativeDraft",
    "TripNarrativeDraft",
    "TripNarrativePlan",
]
