from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

TripRoute = Literal["general_agent", "xhs_trip_planner"]


class TripRouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: TripRoute


class ResolvedTripRoute(TripRouteDecision):
    source: Literal["llm_router", "fallback"]


__all__ = [
    "ResolvedTripRoute",
    "TripRoute",
    "TripRouteDecision",
]
