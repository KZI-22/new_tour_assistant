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


__all__ = [
    "CurrentTimeContext",
    "TravelRequestContext",
]
