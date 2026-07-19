from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from app.clients import xhs_mcp_client as xhs_client_module
from app.clients.xhs_mcp_client import (
    XhsMcpClient,
    XhsMcpClientError,
    XhsNoteDetail,
    XhsNoteDetailResult,
    XhsSearchResult,
)
from app.services import xhs_research_service as research_module
from app.services.xhs_research_service import XhsResearchError, XhsResearchService
from mcp.types import CallToolResult

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_login_tool_response_preserves_structured_content_and_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _load_fixture("xhs_login_pending.json")

    class FakeHttpClient:
        def __init__(self, **_: Any) -> None:
            pass

        async def __aenter__(self) -> FakeHttpClient:
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

    @asynccontextmanager
    async def fake_streamable_http_client(*_: Any, **__: Any):
        yield object(), object(), lambda: None

    class FakeSession:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def initialize(self) -> None:
            return None

        async def call_tool(self, *_: Any, **__: Any) -> CallToolResult:
            return CallToolResult.model_validate(fixture)

    monkeypatch.setattr(xhs_client_module.httpx, "AsyncClient", FakeHttpClient)
    monkeypatch.setattr(
        xhs_client_module,
        "streamable_http_client",
        fake_streamable_http_client,
    )
    monkeypatch.setattr(xhs_client_module, "ClientSession", FakeSession)

    response = await XhsMcpClient("http://127.0.0.1:8765/mcp")._call_tool(
        "xhs_start_login",
        {},
    )

    assert response.structured_content["status"] == "pending"
    assert len(response.images) == 1
    assert response.images[0].mime_type == "image/png"
    assert base64.b64decode(response.images[0].data_base64) == (
        b"sanitized-fixture-image"
    )


class FixtureReadClient:
    def __init__(self, fixture: Mapping[str, Any], scenario: str) -> None:
        self._items = list(fixture["items"])
        self._outcomes = fixture["scenarios"][scenario]
        self.search = XhsSearchResult.model_validate(
            {"keyword": fixture["keyword"], "items": self._items}
        )
        self.detail_calls: list[tuple[str, str]] = []

    async def search_notes(self, _: str) -> XhsSearchResult:
        return self.search

    async def get_note_detail(self, note_id: str, token: str) -> XhsNoteDetailResult:
        self.detail_calls.append((note_id, token))
        outcome = self._outcomes[note_id]
        if outcome == "error":
            raise XhsMcpClientError("NOTE_UNAVAILABLE", "fixture detail unavailable")
        description = "过短正文" if outcome == "short" else "脱敏测试正文。" * 40
        item = next(candidate for candidate in self._items if candidate["note_id"] == note_id)
        return XhsNoteDetailResult(
            note_id=note_id,
            detail=XhsNoteDetail(
                note_id=note_id,
                title=item["title"],
                description=description,
                published_at="2026-07-01T12:00:00+08:00",
                author=item["author"],
                interactions={
                    "liked_count": item["interactions"]["liked_count"],
                    "collected_count": "fixture-count",
                },
            ),
        )


@pytest.mark.xfail(
    strict=True,
    reason="Liked-count normalization has not been implemented.",
)
def test_normalize_xhs_count_contract() -> None:
    fixture = _load_fixture("xhs_post_selection.json")
    normalize = getattr(research_module, "normalize_xhs_count", None)
    assert callable(normalize)
    for case in fixture["liked_count_cases"]:
        assert normalize(case["value"]) == case["expected"]


@pytest.mark.xfail(
    strict=True,
    reason="Research still follows MCP index order instead of normalized likes.",
)
@pytest.mark.asyncio
async def test_research_selects_two_highest_liked_usable_posts() -> None:
    fixture = _load_fixture("xhs_post_selection.json")
    client = FixtureReadClient(fixture, "two_usable")

    result = await XhsResearchService(client).collect(fixture["keyword"])

    assert [post.note_id for post in result.posts] == ["note-high", "note-mid"]
    assert client.detail_calls == [
        ("note-high", "fixture-xsec-high"),
        ("note-mid", "fixture-xsec-mid"),
    ]


@pytest.mark.xfail(
    strict=True,
    reason="The minimum usable body length contract has not been implemented.",
)
@pytest.mark.asyncio
async def test_research_degrades_to_one_usable_post() -> None:
    fixture = _load_fixture("xhs_post_selection.json")
    client = FixtureReadClient(fixture, "one_usable")

    result = await XhsResearchService(client).collect(fixture["keyword"])

    assert [post.note_id for post in result.posts] == ["note-high"]
    assert result.warnings[0] == "本次只有一篇小红书笔记正文可用，方案依据相对有限。"


@pytest.mark.xfail(
    strict=True,
    reason="Short bodies are not yet rejected as unusable evidence.",
)
@pytest.mark.asyncio
async def test_research_reports_zero_usable_posts() -> None:
    fixture = _load_fixture("xhs_post_selection.json")
    client = FixtureReadClient(fixture, "zero_usable")

    with pytest.raises(XhsResearchError) as raised:
        await XhsResearchService(client).collect(fixture["keyword"])

    assert raised.value.code == "NO_USABLE_POSTS"
