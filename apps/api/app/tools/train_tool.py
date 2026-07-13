from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool

from app.clients.flyai_client import FlyAIClient
from app.schemas.travel import TrainSearchInput
from app.tools._flyai import result_payload


def build_train_tool(client: FlyAIClient) -> StructuredTool:
    async def search_train(**values: Any) -> dict[str, Any]:
        query = TrainSearchInput.model_validate(values)
        return result_payload(await client.search_train(query))

    return StructuredTool.from_function(
        coroutine=search_train,
        name="search_train",
        description=(
            "Search current FlyAI train data with structured route, date, seat, time, "
            "price, and sorting constraints. Treat schedules and availability as verified "
            "only when the returned success field is true."
        ),
        args_schema=TrainSearchInput,
    )
