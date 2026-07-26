from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from app.clients.amap_cache import (
    DEFAULT_CACHE_TTL_SECONDS,
    InMemoryAmapCache,
    RedisAmapCache,
    ttl_for_namespace,
)
from redis.exceptions import RedisError

DAY_SECONDS = 24 * 60 * 60


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.set_calls: list[tuple[str, int]] = []

    async def get(self, name: str) -> str | None:
        return self.store.get(name)

    async def set(self, name: str, value: str, *, ex: int) -> None:
        self.set_calls.append((name, ex))
        self.store[name] = value


class BrokenRedis:
    async def get(self, name: str) -> str | None:
        del name
        raise RedisError("connection refused")

    async def set(self, name: str, value: str, *, ex: int) -> None:
        del name, value, ex
        raise RedisError("connection refused")


def test_stable_endpoints_outlive_the_default_ttl() -> None:
    assert ttl_for_namespace("geocode") == 7 * DAY_SECONDS
    assert ttl_for_namespace("coordinate_conversion") == 7 * DAY_SECONDS
    assert ttl_for_namespace("place_search_v2") == DAY_SECONDS
    assert ttl_for_namespace("travel_time_matrix") == 12 * 60 * 60


def test_volatile_endpoints_keep_a_short_ttl() -> None:
    assert ttl_for_namespace("route_plan") == 60 * 60
    assert ttl_for_namespace("weather_forecast") == 30 * 60
    assert ttl_for_namespace("weather_current") == 300


def test_unknown_namespace_falls_back_to_the_default_ttl() -> None:
    assert ttl_for_namespace("something_new") == DEFAULT_CACHE_TTL_SECONDS


def test_overrides_replace_only_the_named_namespace() -> None:
    overrides = {"geocode": 60.0}

    assert ttl_for_namespace("geocode", overrides) == 60
    assert ttl_for_namespace("place_search_v2", overrides) == DAY_SECONDS


@pytest.mark.asyncio
async def test_in_memory_entries_expire_by_their_own_ttl() -> None:
    cache = InMemoryAmapCache()

    await cache.set("short", {"value": 1}, ttl_seconds=0.01)
    await cache.set("long", {"value": 2}, ttl_seconds=60)
    await asyncio.sleep(0.05)

    assert await cache.get("short") is None
    assert await cache.get("long") == {"value": 2}


@pytest.mark.asyncio
async def test_redis_round_trips_a_payload_with_the_requested_ttl() -> None:
    client = FakeRedis()
    cache = RedisAmapCache(client)  # type: ignore[arg-type]
    payload: dict[str, Any] = {"status": "1", "pois": [{"id": "B001"}]}

    await cache.set("place_search_v2:abc", payload, ttl_seconds=DAY_SECONDS)

    assert client.set_calls == [("amap:cache:place_search_v2:abc", DAY_SECONDS)]
    assert json.loads(client.store["amap:cache:place_search_v2:abc"]) == payload
    assert await cache.get("place_search_v2:abc") == payload


@pytest.mark.asyncio
async def test_redis_failure_degrades_to_a_miss_instead_of_raising() -> None:
    cache = RedisAmapCache(BrokenRedis())  # type: ignore[arg-type]

    await cache.set("place_search_v2:abc", {"status": "1"}, ttl_seconds=60)

    assert await cache.get("place_search_v2:abc") is None


@pytest.mark.asyncio
async def test_corrupted_payload_is_treated_as_a_miss() -> None:
    client = FakeRedis()
    client.store["amap:cache:geocode:abc"] = "not-json"

    assert await RedisAmapCache(client).get("geocode:abc") is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_non_object_payload_is_treated_as_a_miss() -> None:
    client = FakeRedis()
    client.store["amap:cache:geocode:abc"] = json.dumps([1, 2, 3])

    assert await RedisAmapCache(client).get("geocode:abc") is None  # type: ignore[arg-type]
