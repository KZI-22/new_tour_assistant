from __future__ import annotations

from datetime import date, timedelta

import pytest
from app.schemas.travel import (
    FlightSearchInput,
    FlyAIResult,
    HotelSearchInput,
    PoiSearchInput,
    TrainSearchInput,
)
from app.tools import build_travel_tools


class FakeFlyAIClient:
    def __init__(self) -> None:
        self.queries: list[object] = []

    async def search_flight(self, query: FlightSearchInput) -> FlyAIResult:
        return self._result(query, "search-flight")

    async def search_train(self, query: TrainSearchInput) -> FlyAIResult:
        return self._result(query, "search-train")

    async def search_hotel(self, query: HotelSearchInput) -> FlyAIResult:
        return self._result(query, "search-hotel")

    async def search_poi(self, query: PoiSearchInput) -> FlyAIResult:
        return self._result(query, "search-poi")

    async def ai_search(self, query: str) -> FlyAIResult:
        return self._result(query, "ai-search")

    async def keyword_search(self, query: str) -> FlyAIResult:
        return self._result(query, "keyword-search")

    def _result(self, query: object, command: str) -> FlyAIResult:
        self.queries.append(query)
        return FlyAIResult(
            success=True,
            command=["flyai", command],
            data={"kind": command},
            duration_ms=1,
        )


@pytest.mark.asyncio
async def test_travel_tools_validate_structured_fields_and_delegate_to_client() -> None:
    client = FakeFlyAIClient()
    tools = build_travel_tools(client)  # type: ignore[arg-type]
    by_name = {tool.name: tool for tool in tools}
    tomorrow = date.today() + timedelta(days=1)

    assert set(by_name) == {
        "ai_search",
        "search_poi",
        "keyword_search",
        "search_flight",
        "search_train",
        "search_hotel",
    }
    assert "command" not in by_name["search_flight"].args_schema.model_json_schema()["properties"]

    results = [
        await by_name["search_flight"].ainvoke(
            {"origin": "上海", "destination": "北京", "departure_date": tomorrow.isoformat()}
        ),
        await by_name["search_train"].ainvoke(
            {"origin": "上海", "destination": "杭州", "departure_date": tomorrow.isoformat()}
        ),
        await by_name["search_hotel"].ainvoke(
            {
                "destination": "杭州",
                "check_in_date": tomorrow.isoformat(),
                "check_out_date": (tomorrow + timedelta(days=1)).isoformat(),
            }
        ),
        await by_name["ai_search"].ainvoke({"query": "杭州两日游"}),
        await by_name["search_poi"].ainvoke({"city": "杭州", "keyword": "西湖"}),
        await by_name["keyword_search"].ainvoke({"query": "西湖门票"}),
    ]

    assert all(result["success"] is True for result in results)
    assert [type(query) for query in client.queries] == [
        FlightSearchInput,
        TrainSearchInput,
        HotelSearchInput,
        str,
        PoiSearchInput,
        str,
    ]
