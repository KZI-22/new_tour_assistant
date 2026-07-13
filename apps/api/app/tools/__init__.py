from __future__ import annotations

from langchain_core.tools import BaseTool

from app.clients.flyai_client import FlyAIClient
from app.tools.flight_tool import build_flight_tool
from app.tools.hotel_tool import build_hotel_tool
from app.tools.poi_tool import build_poi_tool
from app.tools.train_tool import build_train_tool


def build_travel_tools(client: FlyAIClient) -> list[BaseTool]:
    """Build the first FlyAI tool set for later LangGraph node binding."""

    return [
        build_flight_tool(client),
        build_train_tool(client),
        build_hotel_tool(client),
        build_poi_tool(client),
    ]


__all__ = [
    "build_flight_tool",
    "build_hotel_tool",
    "build_poi_tool",
    "build_train_tool",
    "build_travel_tools",
]
