from __future__ import annotations

from langchain_core.tools import BaseTool

from app.clients.amap_client import AmapClient
from app.clients.flyai_client import FlyAIClient
from app.tools.amap_tools import build_amap_tools
from app.tools.flight_tool import build_flight_tool
from app.tools.hotel_tool import build_hotel_tool
from app.tools.train_tool import build_train_tool


def build_travel_tools(
    client: FlyAIClient,
    amap_client: AmapClient | None = None,
) -> list[BaseTool]:
    """Build configured provider tools for later graph-node binding."""

    tools: list[BaseTool] = [
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
    "build_flight_tool",
    "build_hotel_tool",
    "build_train_tool",
    "build_travel_tools",
]
