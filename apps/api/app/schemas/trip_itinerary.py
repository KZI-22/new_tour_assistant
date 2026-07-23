from __future__ import annotations

from pydantic import Field

from app.schemas.map_planning import MapNarrativePlan


class TripNarrativePlan(MapNarrativePlan):
    transport_options: list[str] = Field(default_factory=list)
    hotel_options: list[str] = Field(default_factory=list)


__all__ = ["TripNarrativePlan"]
