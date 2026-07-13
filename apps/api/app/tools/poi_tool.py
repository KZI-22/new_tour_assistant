from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool

from app.clients.flyai_client import FlyAIClient
from app.schemas.travel import PoiSearchInput
from app.tools._flyai import result_payload


def build_poi_tool(client: FlyAIClient) -> StructuredTool:
    async def search_poi(**values: Any) -> dict[str, Any]:
        query = PoiSearchInput.model_validate(values)
        return result_payload(await client.search_poi(query))

    return StructuredTool.from_function(
        coroutine=search_poi,
        name="search_poi",
        description=(
            "Search current FlyAI attraction data by city, keyword, attraction level, and "
            "a CLI-supported category. Treat the data as provider facts only when the "
            "returned success field is true."
        ),
        args_schema=PoiSearchInput,
    )
