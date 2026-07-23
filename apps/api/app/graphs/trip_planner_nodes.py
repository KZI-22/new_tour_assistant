from __future__ import annotations

from typing import Any

from app.graphs.trip_planning_state import TripPlanningState
from app.services.hotel_search_service import HotelSearchService
from app.services.intercity_transport_service import IntercityTransportService
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


class TransportNode:
    def __init__(self, service: IntercityTransportService) -> None:
        self._service = service

    async def __call__(self, state: TripPlanningState) -> dict[str, Any]:
        evidence = await self._service.search(state["capability_plan"].transport)
        return {"transport_evidence": evidence}


class HotelNode:
    def __init__(self, service: HotelSearchService) -> None:
        self._service = service

    async def __call__(self, state: TripPlanningState) -> dict[str, Any]:
        evidence = await self._service.search(state["capability_plan"].hotel)
        return {"hotel_evidence": evidence}


__all__ = ["HotelNode", "MapWeatherNode", "TransportNode"]
