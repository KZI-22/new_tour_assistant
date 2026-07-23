from __future__ import annotations

from typing import Literal

from app.schemas.trip_planning import TripPlanningModel


class ValidationIssue(TripPlanningModel):
    code: str
    path: str
    message: str
    severity: Literal["error", "warning"] = "error"
    expected_summary: str | None = None
    actual_summary: str | None = None
    reference_id: str | None = None


__all__ = ["ValidationIssue"]
