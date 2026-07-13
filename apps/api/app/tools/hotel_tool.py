from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool

from app.clients.flyai_client import FlyAIClient
from app.schemas.travel import HotelSearchInput
from app.tools._flyai import result_payload


def build_hotel_tool(client: FlyAIClient) -> StructuredTool:
    async def search_hotel(**values: Any) -> dict[str, Any]:
        query = HotelSearchInput.model_validate(values)
        return result_payload(await client.search_hotel(query))

    return StructuredTool.from_function(
        coroutine=search_hotel,
        name="search_hotel",
        description=(
            "Search current FlyAI hotel data using destination, stay dates, nearby POI, "
            "type, star, bed, price, and sorting constraints. Treat prices and availability "
            "as verified only when the returned success field is true."
        ),
        args_schema=HotelSearchInput,
    )
