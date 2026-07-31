from __future__ import annotations

import math
from typing import Any, Protocol

from app.schemas.amap import AmapCoordinate, AmapPlace, PlaceSearchResult, SearchPlacesInput
from app.schemas.travel import FlyAIResult, PoiSearchInput


class FlyAIPoiClient(Protocol):
    async def search_poi(self, query: PoiSearchInput) -> FlyAIResult: ...


class FlyAIPoiSearchError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class FlyAIPoiSearchService:
    """Adapt FlyAI POI responses to the planner's map-place contract."""

    def __init__(self, client: FlyAIPoiClient | None) -> None:
        self._client = client

    async def search_places(self, query: SearchPlacesInput) -> PlaceSearchResult:
        if self._client is None:
            raise FlyAIPoiSearchError("FLYAI_UNAVAILABLE")
        city = _text(query.city)
        if not city:
            raise FlyAIPoiSearchError("FLYAI_CITY_REQUIRED")

        result = await self._client.search_poi(
            PoiSearchInput(
                city=city,
                keyword=str(query.keywords),
            )
        )
        if not result.success:
            error_code = (
                result.error_code.value if result.error_code is not None else "UNKNOWN_ERROR"
            )
            raise FlyAIPoiSearchError(error_code)

        items = _item_list(result.data)
        places = [
            place
            for item in items
            if (
                place := _normalize_place(
                    item,
                    city=city,
                    fallback_category=str(query.keywords),
                )
            )
            is not None
        ]
        return PlaceSearchResult(pois=places[: query.limit])


def _item_list(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise FlyAIPoiSearchError("PROVIDER_SCHEMA_INVALID")
    data = payload.get("data", payload)
    if data is None:
        return []
    if not isinstance(data, dict):
        raise FlyAIPoiSearchError("PROVIDER_SCHEMA_INVALID")
    items = data.get("itemList", [])
    if items is None:
        return []
    if not isinstance(items, list):
        raise FlyAIPoiSearchError("PROVIDER_SCHEMA_INVALID")
    return [item for item in items if isinstance(item, dict)]


def _normalize_place(
    item: dict[str, Any],
    *,
    city: str,
    fallback_category: str,
) -> AmapPlace | None:
    poi_id = _text(item.get("id"))
    name = _text(item.get("name"))
    longitude = _number(item.get("longitude"))
    latitude = _number(item.get("latitude"))
    if not poi_id or not name or longitude is None or latitude is None:
        return None
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        return None

    return AmapPlace(
        poi_id=f"flyai:{poi_id}",
        name=name,
        address=_text(item.get("address")),
        province="",
        city=city,
        district="",
        adcode="",
        poi_type=_text(item.get("category")) or fallback_category,
        location=AmapCoordinate(
            longitude=longitude,
            latitude=latitude,
            source="flyai",
        ),
    )


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


__all__ = [
    "FlyAIPoiClient",
    "FlyAIPoiSearchError",
    "FlyAIPoiSearchService",
]
