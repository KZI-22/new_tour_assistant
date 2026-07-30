from __future__ import annotations

from langchain_core.tools import BaseTool

from app.clients.amap_client import AmapClient
from app.clients.flyai_client import FlyAIClient
from app.tools.amap_tools import build_amap_tools
from app.tools.flight_tool import build_flight_tool
from app.tools.flyai_research_tools import build_ai_search_tool, build_keyword_search_tool
from app.tools.hotel_tool import build_hotel_tool
from app.tools.poi_tool import build_poi_tool
from app.tools.train_tool import build_train_tool


def build_travel_tools(
    client: FlyAIClient,
    amap_client: AmapClient | None = None,
) -> list[BaseTool]:
    """Build configured provider tools for later graph-node binding."""

    tools: list[BaseTool] = [
        build_ai_search_tool(client),
        build_poi_tool(client),
        build_keyword_search_tool(client),
        build_flight_tool(client),
        build_train_tool(client),
        build_hotel_tool(client),
    ]
    if amap_client is not None:
        tools.extend(build_amap_tools(amap_client))
    names = [tool.name for tool in tools]
    if len(names) != len(set(names)):
        raise ValueError("Travel tool names must be unique")
    return tools


__all__ = [
    "build_amap_tools",
    "build_ai_search_tool",
    "build_flight_tool",
    "build_hotel_tool",
    "build_keyword_search_tool",
    "build_poi_tool",
    "build_train_tool",
    "build_travel_tools",
]
