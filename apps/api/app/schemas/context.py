from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class ContextModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CurrentTimeContext(ContextModel):
    current_datetime: datetime
    current_date: date
    timezone: str
    weekday: str


class TravelRequestContext(ContextModel):
    client_ip: str | None = Field(
        default=None,
        description="Server-side request context only; never expose directly to the model.",
    )
    client_ip_is_public_ipv4: bool
    time: CurrentTimeContext


class NormalizedTravelDates(ContextModel):
    original_expression: str
    departure_date: date | None = None
    return_date: date | None = None
    check_in_date: date | None = None
    check_out_date: date | None = None
    nights: int | None = Field(default=None, ge=1)
    timezone: str
    is_ambiguous: bool
    message: str | None = None
    candidates: list[date] = Field(default_factory=list)


class TravelDateValidationResult(ContextModel):
    is_valid: bool
    is_ambiguous: bool
    message: str
    candidates: list[date] = Field(default_factory=list)


__all__ = [
    "CurrentTimeContext",
    "NormalizedTravelDates",
    "TravelDateValidationResult",
    "TravelRequestContext",
]
