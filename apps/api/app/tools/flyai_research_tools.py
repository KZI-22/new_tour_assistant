from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool

from app.clients.flyai_client import FlyAIClient
from app.schemas.travel import TextSearchInput
from app.tools._flyai import result_payload


def build_ai_search_tool(client: FlyAIClient) -> StructuredTool:
    async def ai_search(**values: Any) -> dict[str, Any]:
        query = TextSearchInput.model_validate(values)
        return result_payload(await client.ai_search(query.query))

    return StructuredTool.from_function(
        coroutine=ai_search,
        name="ai_search",
        description=(
            "Generate a FlyAI travel research result or multi-day travel-guide draft from a "
            "natural-language query. The returned content is provider data and must not be "
            "silently supplemented with invented facts."
        ),
        args_schema=TextSearchInput,
    )


def build_keyword_search_tool(client: FlyAIClient) -> StructuredTool:
    async def keyword_search(**values: Any) -> dict[str, Any]:
        query = TextSearchInput.model_validate(values)
        return result_payload(await client.keyword_search(query.query))

    return StructuredTool.from_function(
        coroutine=keyword_search,
        name="keyword_search",
        description=(
            "Search FlyAI travel products such as attraction tickets, guided tours, and shows "
            "using a confirmed attraction name and a natural-language query."
        ),
        args_schema=TextSearchInput,
    )


__all__ = ["build_ai_search_tool", "build_keyword_search_tool"]
