from __future__ import annotations

from typing import Any

from app.graphs.trip_planning_state import TripPlanningState
from app.services.hotel_search_service import HotelSearchService
from app.services.intercity_transport_service import IntercityTransportService
from app.services.map_weather_collection_service import MapWeatherCollectionService
from app.services.trip_evidence_joiner import join_trip_evidence
from app.services.trip_itinerary_generator import TripItineraryGenerator
from app.services.trip_itinerary_renderer import render_trip_itinerary


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


class EvidenceJoinNode:
    async def __call__(self, state: TripPlanningState) -> dict[str, Any]:
        evidence = join_trip_evidence(
            state["request"],
            state["capability_plan"],
            state["map_weather_evidence"],
            state["transport_evidence"],
            state["hotel_evidence"],
        )
        return {
            "joined_evidence": evidence,
            "current_stage": "joining_evidence",
        }


class GenerateItineraryNode:
    def __init__(self, generator: TripItineraryGenerator) -> None:
        self._generator = generator

    async def __call__(self, state: TripPlanningState) -> dict[str, Any]:
        narrative = await self._generator.generate(
            state["joined_evidence"],
            validation_issues=state.get("validation_issues"),
        )
        return {
            "narrative": narrative,
            "current_stage": "generating_itinerary",
        }


class RenderResponseNode:
    async def __call__(self, state: TripPlanningState) -> dict[str, Any]:
        answer = render_trip_itinerary(
            state["joined_evidence"],
            state["narrative"],
        )
        return {
            "final_answer": answer,
            "current_stage": "completed",
        }


__all__ = [
    "EvidenceJoinNode",
    "GenerateItineraryNode",
    "HotelNode",
    "MapWeatherNode",
    "RenderResponseNode",
    "TransportNode",
]
