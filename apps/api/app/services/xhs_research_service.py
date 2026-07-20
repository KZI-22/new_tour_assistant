from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
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
_DETAIL_BATCH_SIZE = 2
_COUNT_PATTERN = re.compile(r"^(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>[千萬万]?)\+?$")
_COUNT_MULTIPLIERS = {"": 1, "千": 1_000, "万": 10_000, "萬": 10_000}


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
        min_post_content_chars: int = 200,
        detail_candidate_limit: int = 5,
    ) -> None:
        if evidence_max_chars <= 0:
            raise ValueError("evidence_max_chars must be positive")
        if min_post_content_chars <= 0:
            raise ValueError("min_post_content_chars must be positive")
        if detail_candidate_limit <= 0:
            raise ValueError("detail_candidate_limit must be positive")
        self._client = client
        self._evidence_max_chars = evidence_max_chars
        self._min_post_content_chars = min_post_content_chars
        self._detail_candidate_limit = detail_candidate_limit

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

        candidates = _select_candidates(search.items, self._detail_candidate_limit)
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
            batch_size = min(_DETAIL_BATCH_SIZE, _MAX_POSTS - len(collected))
            batch = candidates[cursor : cursor + batch_size]
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
                content = result.detail.description.strip()
                if len(content) < self._min_post_content_chars:
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
                liked_count=(
                    result.detail.interactions.liked_count
                    or item.interactions.liked_count
                    or None
                ),
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


def normalize_xhs_count(value: str | None) -> int | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().replace(",", "")
    if not normalized:
        return None
    match = _COUNT_PATTERN.fullmatch(normalized)
    if match is None:
        return None
    unit = match.group("unit")
    number_text = match.group("number")
    if not unit and "." in number_text:
        return None
    try:
        return int(Decimal(number_text) * _COUNT_MULTIPLIERS[unit])
    except (InvalidOperation, OverflowError):
        return None


def _select_candidates(
    items: list[XhsSearchItem],
    limit: int,
) -> list[XhsSearchItem]:
    readable = [
        item
        for item in items
        if item.detail_available and item.note_id and item.xsec_token
    ]
    ranked = sorted(
        readable,
        key=lambda item: _candidate_sort_key(item),
    )
    unique: list[XhsSearchItem] = []
    seen_note_ids: set[str] = set()
    for item in ranked:
        if item.note_id in seen_note_ids:
            continue
        seen_note_ids.add(item.note_id)
        unique.append(item)
        if len(unique) == limit:
            break
    return unique


def _candidate_sort_key(item: XhsSearchItem) -> tuple[bool, int, int]:
    liked_count = normalize_xhs_count(item.interactions.liked_count)
    return liked_count is None, -(liked_count or 0), item.index


__all__ = [
    "XhsReadClient",
    "XhsResearchError",
    "XhsResearchService",
    "normalize_xhs_count",
]
