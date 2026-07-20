from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from app.clients import xhs_mcp_client as xhs_client_module
from app.clients.xhs_mcp_client import (
    XhsMcpClient,
    XhsMcpClientError,
    XhsNoteDetail,
    XhsNoteDetailResult,
    XhsSearchItem,
    XhsSearchResult,
)
from app.services.xhs_research_service import XhsResearchError, XhsResearchService
from mcp.types import CallToolResult


class StubMcpClient(XhsMcpClient):
    def __init__(self, responses: dict[str, Any]) -> None:
        super().__init__("http://127.0.0.1:8765/mcp")
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        return SimpleNamespace(structured_content=self.responses[name])


@pytest.mark.asyncio
async def test_streamable_http_client_reuses_stateful_session_across_login_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {
        "http_client_count": 0,
        "http_exit_count": 0,
        "stream_exit_count": 0,
        "session_count": 0,
        "session_exit_count": 0,
        "initialize_count": 0,
        "calls": [],
    }

    class FakeHttpClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["http_client_count"] += 1
            captured["http_kwargs"] = kwargs

        async def __aenter__(self) -> FakeHttpClient:
            return self

        async def __aexit__(self, *_: Any) -> None:
            captured["http_exit_count"] += 1
            return None

    @asynccontextmanager
    async def fake_streamable_http_client(url: str, **kwargs: Any):
        captured["url"] = url
        captured["stream_kwargs"] = kwargs
        try:
            yield object(), object(), lambda: None
        finally:
            captured["stream_exit_count"] += 1

    class FakeSession:
        def __init__(self, *_: Any, **__: Any) -> None:
            captured["session_count"] += 1

        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *_: Any) -> None:
            captured["session_exit_count"] += 1
            return None

        async def initialize(self) -> None:
            captured["initialize_count"] += 1

        async def call_tool(self, name: str, **kwargs: Any) -> CallToolResult:
            captured["calls"].append((name, kwargs))
            responses = {
                "xhs_check_login": {
                    "is_logged_in": False,
                    "checked_at": "2026-07-20T16:00:00+08:00",
                },
                "xhs_start_login": {
                    "login_id": "fixture-login",
                    "status": "pending",
                    "created_at": "2026-07-20T16:00:00+08:00",
                    "expires_at": "2026-07-20T16:04:00+08:00",
                    "is_logged_in": False,
                    "message": "请在 Chrome 中完成登录。",
                },
                "xhs_get_login_status": {
                    "login_id": "fixture-login",
                    "status": "succeeded",
                    "created_at": "2026-07-20T16:00:00+08:00",
                    "expires_at": "2026-07-20T16:04:00+08:00",
                    "is_logged_in": True,
                    "message": "登录成功。",
                },
                "xhs_search_notes": {"keyword": "成都攻略", "items": []},
            }
            return CallToolResult(
                content=[],
                structuredContent=responses[name],
                isError=False,
            )

    monkeypatch.setattr(xhs_client_module.httpx, "AsyncClient", FakeHttpClient)
    monkeypatch.setattr(
        xhs_client_module,
        "streamable_http_client",
        fake_streamable_http_client,
    )
    monkeypatch.setattr(xhs_client_module, "ClientSession", FakeSession)

    client = XhsMcpClient(
        "http://xhs.internal:8765/mcp",
        auth_token="private-token",
    )
    checked = await client.check_login()
    started = await client.start_login()
    completed = await client.get_login_status(started.login_id)
    result = await client.search_notes("成都攻略")

    assert checked.is_logged_in is False
    assert completed.status == "succeeded"
    assert result.keyword == "成都攻略"
    assert captured["http_kwargs"]["headers"] == {"Authorization": "Bearer private-token"}
    assert captured["url"] == "http://xhs.internal:8765/mcp"
    assert captured["stream_kwargs"]["terminate_on_close"] is True
    assert captured["http_client_count"] == 1
    assert captured["session_count"] == 1
    assert captured["initialize_count"] == 1
    assert [name for name, _ in captured["calls"]] == [
        "xhs_check_login",
        "xhs_start_login",
        "xhs_get_login_status",
        "xhs_search_notes",
    ]

    await client.aclose()

    assert captured["session_exit_count"] == 1
    assert captured["stream_exit_count"] == 1
    assert captured["http_exit_count"] == 1


def _search_item(index: int, *, detail_available: bool = True) -> XhsSearchItem:
    return XhsSearchItem(
        note_id=f"note-{index}",
        xsec_token=f"secret-{index}",
        detail_available=detail_available,
        index=index,
        title=f"搜索标题 {index}",
        author={"nickname": f"作者 {index}"},
        interactions={"liked_count": str(100 - index)},
    )


def _detail(index: int, description: str | None = None) -> XhsNoteDetailResult:
    return XhsNoteDetailResult(
        note_id=f"note-{index}",
        detail=XhsNoteDetail(
            note_id=f"note-{index}",
            title=f"详情标题 {index}",
            description=description or f"第 {index} 篇攻略正文。" * 40,
            published_at="2026-07-01T12:00:00+08:00",
            author={"nickname": f"详情作者 {index}"},
            interactions={"liked_count": "100", "collected_count": "50"},
        ),
    )


@pytest.mark.asyncio
async def test_mcp_client_uses_search_pair_for_detail_without_comments() -> None:
    client = StubMcpClient(
        {
            "xhs_search_notes": {
                "keyword": "成都 3天 旅游攻略",
                "items": [
                    {
                        "note_id": "note-1",
                        "xsec_token": "private-token",
                        "detail_available": True,
                        "index": 0,
                    }
                ],
            },
            "xhs_get_note_detail": {
                "note_id": "note-1",
                "detail": {
                    "note_id": "note-1",
                    "title": "成都攻略",
                    "description": "正文",
                },
            },
        }
    )

    search = await client.search_notes("成都 3天 旅游攻略")
    await client.get_note_detail(search.items[0].note_id, search.items[0].xsec_token)

    assert client.calls == [
        (
            "xhs_search_notes",
            {
                "keyword": "成都 3天 旅游攻略",
                "sort_by": "most_liked",
            },
        ),
        (
            "xhs_get_note_detail",
            {
                "note_id": "note-1",
                "xsec_token": "private-token",
                "comment_mode": "none",
            },
        ),
    ]


def test_mcp_client_distinguishes_structure_and_note_errors() -> None:
    assert xhs_client_module._safe_error_message("PAGE_STRUCTURE_CHANGED") == (
        "小红书搜索或详情页面结构已变化，当前读取规则需要更新。"
    )
    assert xhs_client_module._safe_error_message("NOTE_UNAVAILABLE") == (
        "暂时无法读取所选小红书笔记，请稍后重试。"
    )


@pytest.mark.asyncio
async def test_mcp_client_supports_login_check_start_status_and_cancel() -> None:
    pending = {
        "login_id": "fixture-login",
        "status": "pending",
        "created_at": "2026-07-19T10:00:00+08:00",
        "expires_at": "2026-07-19T10:05:00+08:00",
        "is_logged_in": False,
        "message": "请在已打开的 Google Chrome 中完成验证码。",
    }
    client = StubMcpClient(
        {
            "xhs_check_login": {
                "is_logged_in": False,
                "checked_at": "2026-07-19T10:00:00+08:00",
            },
            "xhs_start_login": pending,
            "xhs_get_login_status": {**pending, "status": "succeeded", "is_logged_in": True},
            "xhs_cancel_login": {**pending, "status": "cancelled"},
        }
    )

    checked = await client.check_login()
    started = await client.start_login()
    succeeded = await client.get_login_status("fixture-login")
    cancelled = await client.cancel_login("fixture-login")

    assert checked.is_logged_in is False
    assert started.status == "pending"
    assert "Google Chrome" in started.message
    assert succeeded.status == "succeeded"
    assert succeeded.is_logged_in is True
    assert cancelled.status == "cancelled"
    assert client.calls == [
        ("xhs_check_login", {}),
        ("xhs_start_login", {"force_restart": False}),
        ("xhs_get_login_status", {"login_id": "fixture-login"}),
        ("xhs_cancel_login", {"login_id": "fixture-login"}),
    ]


@pytest.mark.asyncio
async def test_mcp_client_accepts_already_succeeded_login() -> None:
    client = StubMcpClient(
        {
            "xhs_start_login": {
                "login_id": "fixture-login",
                "status": "succeeded",
                "created_at": "2026-07-19T10:00:00+08:00",
                "expires_at": "2026-07-19T10:05:00+08:00",
                "is_logged_in": True,
                "message": "已登录。",
            }
        }
    )

    started = await client.start_login()

    assert started.status == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "succeeded", "expired", "cancelled", "failed"])
async def test_mcp_client_accepts_all_confirmed_login_session_statuses(status: str) -> None:
    client = StubMcpClient(
        {
            "xhs_get_login_status": {
                "login_id": "fixture-login",
                "status": status,
                "created_at": "2026-07-19T10:00:00+08:00",
                "expires_at": "2026-07-19T10:05:00+08:00",
                "is_logged_in": status == "succeeded",
                "message": f"fixture {status}",
            }
        }
    )

    result = await client.get_login_status("fixture-login")

    assert result.status == status


class FakeReadClient:
    def __init__(self) -> None:
        self.search = XhsSearchResult(
            keyword="成都 3天 旅游攻略",
            items=[_search_item(2), _search_item(0), _search_item(1)],
        )
        self.detail_calls: list[tuple[str, str]] = []

    async def search_notes(self, _: str) -> XhsSearchResult:
        return self.search

    async def get_note_detail(self, note_id: str, token: str) -> XhsNoteDetailResult:
        self.detail_calls.append((note_id, token))
        if note_id == "note-0":
            raise XhsMcpClientError("NOTE_UNAVAILABLE", "unavailable")
        return _detail(int(note_id.rsplit("-", 1)[1]))


@pytest.mark.asyncio
async def test_research_uses_first_two_readable_posts_and_keeps_tokens_private() -> None:
    client = FakeReadClient()
    counts: list[int] = []

    result = await XhsResearchService(
        client,
        evidence_max_chars=100,
        min_post_content_chars=1,
    ).collect(
        "成都 3天 旅游攻略",
        on_search_complete=counts.append,
    )

    assert counts == [3]
    assert [post.note_id for post in result.posts] == ["note-1", "note-2"]
    assert client.detail_calls == [
        ("note-0", "secret-0"),
        ("note-1", "secret-1"),
        ("note-2", "secret-2"),
    ]
    assert "secret-" not in result.model_dump_json()
    assert result.warnings == ["有 1 篇候选笔记未能读取，已跳过。"]


class EmptyReadClient:
    async def search_notes(self, keyword: str) -> XhsSearchResult:
        return XhsSearchResult(keyword=keyword, items=[])

    async def get_note_detail(self, note_id: str, token: str) -> XhsNoteDetailResult:
        raise AssertionError((note_id, token))


@pytest.mark.asyncio
async def test_research_reports_empty_search_without_calling_details() -> None:
    with pytest.raises(XhsResearchError, match="没有找到") as raised:
        await XhsResearchService(EmptyReadClient()).collect("不存在的城市 2天 旅游攻略")

    assert raised.value.code == "NO_RESULTS"


@pytest.mark.asyncio
async def test_research_limits_candidates_after_filtering_sorting_and_deduplication() -> None:
    class CandidateClient:
        def __init__(self) -> None:
            self.detail_calls: list[tuple[str, str]] = []

        async def search_notes(self, keyword: str) -> XhsSearchResult:
            items = [
                XhsSearchItem(
                    note_id=f"note-{index}",
                    xsec_token=f"token-{index}",
                    detail_available=True,
                    index=index,
                    interactions={"liked_count": str(index)},
                )
                for index in range(7)
            ]
            items.append(
                XhsSearchItem(
                    note_id="note-6",
                    xsec_token="duplicate-token",
                    detail_available=True,
                    index=7,
                    interactions={"liked_count": "999"},
                )
            )
            return XhsSearchResult(keyword=keyword, items=items)

        async def get_note_detail(self, note_id: str, token: str) -> XhsNoteDetailResult:
            self.detail_calls.append((note_id, token))
            raise XhsMcpClientError("NOTE_UNAVAILABLE", "fixture unavailable")

    client = CandidateClient()

    with pytest.raises(XhsResearchError) as raised:
        await XhsResearchService(client, min_post_content_chars=1).collect("成都 3日游 攻略")

    assert raised.value.code == "NO_USABLE_POSTS"
    assert client.detail_calls == [
        ("note-6", "duplicate-token"),
        ("note-5", "token-5"),
        ("note-4", "token-4"),
        ("note-3", "token-3"),
        ("note-2", "token-2"),
    ]
