from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Request

from app.schemas.context import CurrentTimeContext, TravelRequestContext

IpAddress = IPv4Address | IPv6Address
IpNetwork = Any
_request_context: ContextVar[TravelRequestContext | None] = ContextVar(
    "travel_request_context",
    default=None,
)


def build_time_context(
    timezone: str,
    *,
    now: datetime | None = None,
) -> CurrentTimeContext:
    zone = ZoneInfo(timezone)
    current = now.astimezone(zone) if now is not None else datetime.now(zone)
    return CurrentTimeContext(
        current_datetime=current,
        current_date=current.date(),
        timezone=timezone,
        weekday=current.strftime("%A"),
    )


def parse_trusted_proxy_networks(cidrs: Sequence[str]) -> tuple[IpNetwork, ...]:
    try:
        return tuple(ip_network(item, strict=False) for item in cidrs)
    except ValueError:
        raise ValueError("TRUSTED_PROXY_CIDRS contains an invalid IP network.") from None


def extract_client_ip(
    request: Request,
    trusted_proxy_networks: Sequence[IpNetwork] = (),
) -> str | None:
    """Return the untrusted edge IP, honoring forwarding headers only from trusted peers."""

    peer = _parse_ip(request.client.host if request.client else None)
    if peer is None:
        return None
    if not _is_trusted(peer, trusted_proxy_networks):
        return str(peer)

    forwarded = request.headers.get("x-forwarded-for", "")
    chain = [_parse_ip(item.strip()) for item in forwarded.split(",") if item.strip()]
    if not chain:
        real_ip = _parse_ip(request.headers.get("x-real-ip"))
        return str(real_ip or peer)
    if any(item is None for item in chain):
        return str(peer)

    valid_chain = [item for item in chain if item is not None]
    for candidate in reversed([*valid_chain, peer]):
        if not _is_trusted(candidate, trusted_proxy_networks):
            return str(candidate)
    return str(valid_chain[0])


def is_public_ipv4(value: str | None) -> bool:
    parsed = _parse_ip(value)
    return isinstance(parsed, IPv4Address) and parsed.is_global


def get_request_context() -> TravelRequestContext | None:
    return _request_context.get()


@contextmanager
def use_request_context(context: TravelRequestContext) -> Any:
    """Set request context explicitly for independent tool calls and tests."""

    token = _request_context.set(context)
    try:
        yield context
    finally:
        _request_context.reset(token)


class RequestContextMiddleware:
    """Keep IP and clock data scoped through the full ASGI response, including streaming."""

    def __init__(
        self,
        app: Any,
        *,
        trusted_proxy_cidrs: Sequence[str] = (),
        timezone: str = "Asia/Shanghai",
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._app = app
        self._trusted_proxy_networks = parse_trusted_proxy_networks(trusted_proxy_cidrs)
        self._timezone = timezone
        ZoneInfo(timezone)
        self._now_factory = now_factory

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        request = Request(scope)
        client_ip = extract_client_ip(request, self._trusted_proxy_networks)
        time_context = build_time_context(
            self._timezone,
            now=self._now_factory() if self._now_factory is not None else None,
        )
        context = TravelRequestContext(
            client_ip=client_ip,
            client_ip_is_public_ipv4=is_public_ipv4(client_ip),
            time=time_context,
        )
        scope.setdefault("state", {})["travel_context"] = context
        token = _request_context.set(context)
        try:
            await self._app(scope, receive, send)
        finally:
            _request_context.reset(token)


def _parse_ip(value: str | None) -> IpAddress | None:
    if not value:
        return None
    normalized = value.strip().strip('"')
    if normalized.startswith("[") and "]" in normalized:
        normalized = normalized[1 : normalized.index("]")]
    elif normalized.count(":") == 1 and "." in normalized:
        normalized = normalized.split(":", 1)[0]
    try:
        return ip_address(normalized)
    except ValueError:
        return None


def _is_trusted(value: IpAddress, networks: Sequence[IpNetwork]) -> bool:
    return any(value.version == network.version and value in network for network in networks)


__all__ = [
    "RequestContextMiddleware",
    "build_time_context",
    "extract_client_ip",
    "get_request_context",
    "is_public_ipv4",
    "parse_trusted_proxy_networks",
    "use_request_context",
]
