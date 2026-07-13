from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool

from app.clients.flyai_client import FlyAIClient
from app.schemas.travel import FlightSearchInput
from app.tools._flyai import result_payload


def build_flight_tool(client: FlyAIClient) -> StructuredTool:
    async def search_flight(**values: Any) -> dict[str, Any]:
        query = FlightSearchInput.model_validate(values)
        return result_payload(await client.search_flight(query))

    return StructuredTool.from_function(
        coroutine=search_flight,
        name="search_flight",
        description=(
            "Search current FlyAI flight data with structured route, date, cabin, time, "
            "price, and sorting constraints. Prices and availability are provider facts "
            "only when the returned success field is true."
        ),
        args_schema=FlightSearchInput,
    )
