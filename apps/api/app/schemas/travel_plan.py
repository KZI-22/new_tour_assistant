from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.trip_itinerary import TripNarrativePlan
from app.schemas.trip_plan_snapshot import TripPlanSnapshotAny


class TravelPlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TravelPlanReference(TravelPlanModel):
    plan_id: UUID
    version_id: UUID
    version: int = Field(ge=1)


class TravelPlanDetailResponse(TravelPlanReference):
    title: str
    status: Literal["draft", "active", "archived"]
    current_version: int = Field(ge=1)
    change_summary: str | None = None
    created_at: datetime
    snapshot: TripPlanSnapshotAny
    narrative: TripNarrativePlan | None = None
    rendered_markdown: str | None = None


__all__ = ["TravelPlanDetailResponse", "TravelPlanReference"]
