from __future__ import annotations

from datetime import datetime

from app.core.request_context import (
    build_time_context,
    extract_client_ip,
    is_public_ipv4,
    parse_trusted_proxy_networks,
)
from fastapi import Request


def make_request(peer: str, forwarded_for: str | None = None) -> Request:
    headers = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode("ascii")))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "client": (peer, 12345),
            "server": ("testserver", 80),
            "scheme": "http",
            "query_string": b"",
        }
    )


def test_untrusted_peer_cannot_spoof_forwarded_ip() -> None:
    request = make_request("203.0.113.10", "8.8.8.8")

    assert extract_client_ip(request, parse_trusted_proxy_networks(())) == "203.0.113.10"


def test_trusted_proxy_chain_selects_the_first_untrusted_hop_from_the_right() -> None:
    request = make_request("10.0.0.2", "8.8.8.8, 10.0.0.3")
    networks = parse_trusted_proxy_networks(("10.0.0.0/8",))

    assert extract_client_ip(request, networks) == "8.8.8.8"


def test_public_ipv4_check_rejects_loopback_private_and_ipv6() -> None:
    assert is_public_ipv4("8.8.8.8") is True
    assert is_public_ipv4("127.0.0.1") is False
    assert is_public_ipv4("192.168.1.1") is False
    assert is_public_ipv4("2001:4860:4860::8888") is False


def test_time_context_uses_explicit_timezone_and_fixed_clock() -> None:
    context = build_time_context(
        "Asia/Shanghai",
        now=datetime.fromisoformat("2026-07-13T02:00:00+00:00"),
    )

    assert context.current_datetime.isoformat() == "2026-07-13T10:00:00+08:00"
    assert context.current_date.isoformat() == "2026-07-13"
    assert context.timezone == "Asia/Shanghai"
    assert context.weekday == "Monday"
