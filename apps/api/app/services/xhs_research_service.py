from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from app.clients.xhs_mcp_client import (
    XhsLoginSessionResult,
    XhsLoginStatusResult,
    XhsMcpClientError,
    XhsNoteDetailResult,
    XhsSearchItem,
    XhsSearchResult,
)
from app.schemas.xhs_planning import XhsPostEvidence, XhsResearchResult

_MAX_POSTS = 2
_MAX_DETAIL_ATTEMPTS = 6


class XhsResearchError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class XhsReadClient(Protocol):
    async def check_login(self) -> XhsLoginStatusResult: ...

    async def start_login(self) -> XhsLoginSessionResult: ...

    async def get_login_status(self, login_id: str) -> XhsLoginSessionResult: ...

    async def cancel_login(self, login_id: str) -> XhsLoginSessionResult: ...

    async def search_notes(self, keyword: str) -> XhsSearchResult: ...

    async def get_note_detail(
        self,
        note_id: str,
        xsec_token: str,
    ) -> XhsNoteDetailResult: ...


class XhsResearchService:
    def __init__(
        self,
        client: XhsReadClient,
        *,
        evidence_max_chars: int = 12_000,
    ) -> None:
        if evidence_max_chars <= 0:
            raise ValueError("evidence_max_chars must be positive")
        self._client = client
        self._evidence_max_chars = evidence_max_chars

    async def check_login(self) -> XhsLoginStatusResult:
        try:
            return await self._client.check_login()
        except XhsMcpClientError as exc:
            raise _research_error(exc) from exc

    async def start_login(self) -> XhsLoginSessionResult:
        try:
            return await self._client.start_login()
        except XhsMcpClientError as exc:
            raise _research_error(exc) from exc

    async def get_login_status(self, login_id: str) -> XhsLoginSessionResult:
        try:
            return await self._client.get_login_status(login_id)
        except XhsMcpClientError as exc:
            raise _research_error(exc) from exc

    async def cancel_login(self, login_id: str) -> XhsLoginSessionResult:
        try:
            return await self._client.cancel_login(login_id)
        except XhsMcpClientError as exc:
            raise _research_error(exc) from exc

    async def collect(
        self,
        keyword: str,
        *,
        on_search_complete: Callable[[int], None] | None = None,
    ) -> XhsResearchResult:
        try:
            search = await self._client.search_notes(keyword)
        except XhsMcpClientError as exc:
            raise _research_error(exc) from exc

        candidates = sorted(
            (
                item
                for item in search.items
                if item.detail_available and item.note_id and item.xsec_token
            ),
            key=lambda item: item.index,
        )[:_MAX_DETAIL_ATTEMPTS]
        if on_search_complete is not None:
            on_search_complete(len(candidates))
        if not candidates:
            raise XhsResearchError(
                "NO_RESULTS",
                "没有找到可读取的小红书攻略笔记，请换个城市名称后重试。",
            )

        collected: list[tuple[XhsSearchItem, XhsNoteDetailResult]] = []
        failed_details = 0
        cursor = 0
        while len(collected) < _MAX_POSTS and cursor < len(candidates):
            needed = _MAX_POSTS - len(collected)
            batch = candidates[cursor : cursor + needed]
            cursor += len(batch)
            results = await asyncio.gather(
                *(self._client.get_note_detail(item.note_id, item.xsec_token) for item in batch),
                return_exceptions=True,
            )
            for item, result in zip(batch, results, strict=True):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if isinstance(result, BaseException):
                    failed_details += 1
                    continue
                if not result.detail.description.strip():
                    failed_details += 1
                    continue
                collected.append((item, result))

        if not collected:
            raise XhsResearchError(
                "NO_USABLE_POSTS",
                "搜索到了相关笔记，但暂时无法读取正文，请稍后重试。",
                retryable=True,
            )

        per_post_limit = max(1, self._evidence_max_chars // len(collected))
        queried_at = datetime.now(UTC)
        posts = [
            XhsPostEvidence(
                reference_id=f"source_{index}",
                note_id=result.detail.note_id or item.note_id,
                search_rank=item.index + 1,
                title=result.detail.title or item.title or "未命名笔记",
                author_name=result.detail.author.nickname or item.author.nickname or "未知作者",
                published_at=result.detail.published_at,
                content=result.detail.description.strip()[:per_post_limit],
                liked_count=result.detail.interactions.liked_count or None,
                collected_count=result.detail.interactions.collected_count or None,
                queried_at=queried_at,
            )
            for index, (item, result) in enumerate(collected, start=1)
        ]
        warnings: list[str] = []
        if len(posts) == 1:
            warnings.append("本次只有一篇小红书笔记正文可用，方案依据相对有限。")
        if failed_details:
            warnings.append(f"有 {failed_details} 篇候选笔记未能读取，已跳过。")
        return XhsResearchResult(
            keyword=search.keyword or keyword,
            posts=posts,
            warnings=warnings,
        )


def _research_error(exc: XhsMcpClientError) -> XhsResearchError:
    return XhsResearchError(
        exc.code,
        exc.message,
        retryable=exc.retryable,
    )


__all__ = ["XhsReadClient", "XhsResearchError", "XhsResearchService"]
