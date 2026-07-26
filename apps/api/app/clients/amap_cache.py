from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from time import monotonic
from typing import Any, Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

_HOUR_SECONDS = 60 * 60
_DAY_SECONDS = 24 * _HOUR_SECONDS

DEFAULT_CACHE_TTL_SECONDS = 300.0

NAMESPACE_CACHE_TTL_SECONDS: Mapping[str, float] = {
    # Geographic identity barely changes, so re-resolving it only burns quota.
    "geocode": 7 * _DAY_SECONDS,
    "reverse_geocode": 7 * _DAY_SECONDS,
    "coordinate_conversion": 7 * _DAY_SECONDS,
    # POI catalogues move on a weekly cadence at most.
    "place_search_v2": _DAY_SECONDS,
    # Distance between two fixed points does not depend on when it is asked.
    "travel_time_matrix": 12 * _HOUR_SECONDS,
    # Transit lines are stable, but a route still carries some live routing.
    "route_plan": _HOUR_SECONDS,
    # Weather must stay close to the provider's own refresh rate.
    "weather_forecast": 30 * 60,
    "weather_current": 300,
}


def ttl_for_namespace(
    namespace: str,
    overrides: Mapping[str, float] | None = None,
) -> float:
    if overrides and namespace in overrides:
        return overrides[namespace]
    return NAMESPACE_CACHE_TTL_SECONDS.get(namespace, DEFAULT_CACHE_TTL_SECONDS)


class AmapCache(Protocol):
    """Storage boundary for non-sensitive provider responses."""

    async def get(self, key: str) -> dict[str, Any] | None: ...

    async def set(self, key: str, value: dict[str, Any], *, ttl_seconds: float) -> None: ...


@dataclass(slots=True)
class _CacheEntry:
    expires_at: float
    value: dict[str, Any]


class InMemoryAmapCache:
    """Process-local fallback used when no shared cache is configured."""

    def __init__(self, *, max_entries: int = 512) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._max_entries = max_entries
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> dict[str, Any] | None:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= monotonic():
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            return deepcopy(entry.value)

    async def set(self, key: str, value: dict[str, Any], *, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        async with self._lock:
            self._entries[key] = _CacheEntry(
                expires_at=monotonic() + ttl_seconds,
                value=deepcopy(value),
            )
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)


class RedisAmapCache:
    """Shared cache that survives restarts and is visible to every worker.

    Failure semantics deliberately differ from the OTP store: that store guards
    authentication and must fail loudly, while this one is a pure optimisation.
    An unavailable or corrupted cache degrades to a miss and never fails the
    surrounding request.
    """

    def __init__(self, client: Redis, *, key_prefix: str = "amap:cache") -> None:
        if not key_prefix or any(character.isspace() for character in key_prefix):
            raise ValueError("Amap cache key prefix must be non-empty and contain no whitespace.")
        self._client = client
        self._key_prefix = key_prefix.rstrip(":")

    def _key(self, key: str) -> str:
        return f"{self._key_prefix}:{key}"

    async def get(self, key: str) -> dict[str, Any] | None:
        try:
            raw = await self._client.get(self._key(key))
        except RedisError:
            logger.warning("Amap cache read failed; continuing as a cache miss.")
            return None
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            logger.warning("Discarding an unreadable Amap cache entry.")
            return None
        if not isinstance(payload, dict):
            logger.warning("Discarding an Amap cache entry with an unexpected structure.")
            return None
        return payload

    async def set(self, key: str, value: dict[str, Any], *, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        try:
            await self._client.set(
                self._key(key),
                json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                ex=max(1, int(ttl_seconds)),
            )
        except (RedisError, TypeError, ValueError):
            logger.warning("Amap cache write failed; the response stays uncached.")


__all__ = [
    "AmapCache",
    "DEFAULT_CACHE_TTL_SECONDS",
    "InMemoryAmapCache",
    "NAMESPACE_CACHE_TTL_SECONDS",
    "RedisAmapCache",
    "ttl_for_namespace",
]
