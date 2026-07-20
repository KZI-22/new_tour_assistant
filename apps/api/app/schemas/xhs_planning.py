from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class XhsPlanningModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class XhsTripRequest(XhsPlanningModel):
    destination_city: str | None = Field(default=None, max_length=80)
    duration_days: int | None = Field(default=None, ge=1)

    @field_validator("destination_city")
    @classmethod
    def normalize_destination_city(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class XhsTripRequestExtraction(XhsPlanningModel):
    request: XhsTripRequest


class XhsPostEvidence(XhsPlanningModel):
    reference_id: Literal["source_1", "source_2"]
    role: Literal["primary", "supplementary"]
    note_id: str
    search_rank: int = Field(ge=1)
    title: str
    author_name: str
    published_at: str | None = None
    content: str = Field(min_length=1)
    liked_count_raw: str | None = None
    liked_count: int | None = Field(default=None, ge=0)
    queried_at: datetime


class XhsResearchResult(XhsPlanningModel):
    keyword: str
    posts: list[XhsPostEvidence] = Field(min_length=1, max_length=2)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_source_roles(self) -> Self:
        expected = [("source_1", "primary")]
        if len(self.posts) == 2:
            expected.append(("source_2", "supplementary"))
        actual = [(post.reference_id, post.role) for post in self.posts]
        if actual != expected:
            raise ValueError(
                "posts must contain primary source_1 then optional supplementary source_2"
            )
        return self


class XhsPlanActivity(XhsPlanningModel):
    time_of_day: Literal["morning", "afternoon", "evening", "flexible"]
    place_name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    source_refs: list[Literal["source_1", "source_2"]] = Field(min_length=1)

    @field_validator("source_refs")
    @classmethod
    def normalize_source_refs(
        cls,
        value: list[Literal["source_1", "source_2"]],
    ) -> list[Literal["source_1", "source_2"]]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))


class XhsDayPlan(XhsPlanningModel):
    day_index: int = Field(ge=1)
    theme: str = Field(min_length=1)
    activities: list[XhsPlanActivity] = Field(min_length=1)
    meal_suggestions: list[str] = Field(default_factory=list)
    tips: list[str] = Field(default_factory=list)


class XhsPlanSource(XhsPlanningModel):
    reference_id: Literal["source_1", "source_2"]
    role: Literal["primary", "supplementary"]
    note_id: str
    title: str
    author_name: str
    published_at: str | None = None
    liked_count: int | None = Field(default=None, ge=0)


class XhsItineraryPlan(XhsPlanningModel):
    title: str = Field(min_length=1)
    destination_city: str = Field(min_length=1)
    duration_days: int = Field(ge=1)
    summary: str = Field(min_length=1)
    days: list[XhsDayPlan] = Field(default_factory=list)
    practical_tips: list[str] = Field(default_factory=list)
    sources: list[XhsPlanSource] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_day_coverage(self) -> Self:
        if len(self.days) != self.duration_days:
            raise ValueError("days must contain exactly duration_days entries")
        indexes = [day.day_index for day in self.days]
        if indexes != list(range(1, self.duration_days + 1)):
            raise ValueError("day_index values must be consecutive and start at 1")
        return self


__all__ = [
    "XhsDayPlan",
    "XhsItineraryPlan",
    "XhsPlanActivity",
    "XhsPlanSource",
    "XhsPostEvidence",
    "XhsResearchResult",
    "XhsTripRequest",
    "XhsTripRequestExtraction",
]
