from __future__ import annotations

from typing import Any

from app.graphs.trip_planning_state import TripPlanningState
from app.services.map_weather_collection_service import MapWeatherCollectionService


class MapWeatherNode:
    def __init__(self, collection_service: MapWeatherCollectionService) -> None:
        self._collection_service = collection_service

    async def __call__(self, state: TripPlanningState) -> dict[str, Any]:
        request = state["request"]
        evidence = await self._collection_service.collect(request.core)
        return {
            "map_weather_evidence": evidence,
            "current_stage": "collecting_map_weather",
        }


__all__ = ["MapWeatherNode"]
