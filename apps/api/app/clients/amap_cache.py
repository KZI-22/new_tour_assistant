from __future__ import annotations

import asyncio
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from time import monotonic
from typing import Any, Protocol


class AmapCache(Protocol):
    """Async boundary that a later Redis-backed cache can implement."""

    async def get(self, key: str) -> dict[str, Any] | None: ...

    async def set(self, key: str, value: dict[str, Any]) -> None: ...


@dataclass(slots=True)
class _CacheEntry:
    expires_at: float
    value: dict[str, Any]


class InMemoryAmapCache:
    """Small process-local TTL cache used only for non-sensitive provider responses."""

    def __init__(self, *, ttl_seconds: float = 300, max_entries: int = 512) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._ttl_seconds = ttl_seconds
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

    async def set(self, key: str, value: dict[str, Any]) -> None:
        async with self._lock:
            self._entries[key] = _CacheEntry(
                expires_at=monotonic() + self._ttl_seconds,
                value=deepcopy(value),
            )
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)


__all__ = ["AmapCache", "InMemoryAmapCache"]
