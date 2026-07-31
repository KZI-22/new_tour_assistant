from __future__ import annotations

import pytest
from app.schemas.amap import SearchPlacesInput
from app.schemas.travel import FlyAIErrorCode, FlyAIResult, PoiSearchInput
from app.services.flyai_poi_search_service import (
    FlyAIPoiSearchError,
    FlyAIPoiSearchService,
)


class FakeFlyAIPoiClient:
    def __init__(self, result: FlyAIResult) -> None:
        self.result = result
        self.queries: list[PoiSearchInput] = []

    async def search_poi(self, query: PoiSearchInput) -> FlyAIResult:
        self.queries.append(query)
        return self.result


def _result(data: object) -> FlyAIResult:
    return FlyAIResult(
        success=True,
        command=["flyai", "search-poi"],
        data=data,
        duration_ms=10,
    )


@pytest.mark.asyncio
async def test_search_places_normalizes_flyai_items_and_skips_invalid_coordinates() -> None:
    client = FakeFlyAIPoiClient(
        _result(
            {
                "data": {
                    "itemList": [
                        {
                            "id": "1001",
                            "name": "成都博物馆",
                            "address": "青羊区小河街1号",
                            "category": "museum",
                            "longitude": "104.072259",
                            "latitude": "30.663375",
                        },
                        {
                            "id": "1002",
                            "name": "无分类博物馆",
                            "category": None,
                            "longitude": "104.08",
                            "latitude": "30.67",
                        },
                        {
                            "id": "invalid",
                            "name": "坐标缺失景点",
                            "longitude": None,
                            "latitude": "30.6",
                        },
                    ]
                },
                "message": "success",
                "status": 0,
            }
        )
    )

    result = await FlyAIPoiSearchService(client).search_places(
        SearchPlacesInput(city="成都", keywords="博物馆", limit=5)
    )

    assert client.queries == [PoiSearchInput(city="成都", keyword="博物馆")]
    assert len(result.pois) == 2
    place = result.pois[0]
    assert place.poi_id == "flyai:1001"
    assert place.name == "成都博物馆"
    assert place.city == "成都"
    assert place.poi_type == "museum"
    assert place.location.longitude == pytest.approx(104.072259)
    assert place.location.latitude == pytest.approx(30.663375)
    assert place.location.source == "flyai"
    assert result.pois[1].poi_type == "博物馆"


@pytest.mark.asyncio
async def test_search_places_treats_successful_empty_provider_data_as_no_results() -> None:
    client = FakeFlyAIPoiClient(_result({"data": None, "message": "empty", "status": 0}))

    result = await FlyAIPoiSearchService(client).search_places(
        SearchPlacesInput(city="成都", keywords="城市地标")
    )

    assert result.pois == []


@pytest.mark.asyncio
async def test_search_places_preserves_flyai_failure_code() -> None:
    client = FakeFlyAIPoiClient(
        FlyAIResult(
            success=False,
            command=["flyai", "search-poi"],
            error_code=FlyAIErrorCode.CLI_TIMEOUT,
            error_message="timed out",
            duration_ms=10,
        )
    )

    with pytest.raises(FlyAIPoiSearchError, match="CLI_TIMEOUT") as exc_info:
        await FlyAIPoiSearchService(client).search_places(
            SearchPlacesInput(city="成都", keywords="博物馆")
        )

    assert exc_info.value.error_code == "CLI_TIMEOUT"
