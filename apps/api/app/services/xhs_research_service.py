from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from time import perf_counter
from typing import Any, Literal, Protocol

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


@dataclass(frozen=True, slots=True)
class XhsResearchTraceUpdate:
    step: Literal["search_results", "post_detail", "evidence_selected"]
    title: str
    status: Literal["success", "partial", "failed", "skipped"]
    data: dict[str, Any]
    duration_ms: int | None = None


XhsResearchTraceCallback = Callable[[XhsResearchTraceUpdate], None]


@dataclass(frozen=True, slots=True)
class _DetailAttempt:
    item: XhsSearchItem
    result: XhsNoteDetailResult | None
    error: BaseException | None
    duration_ms: int


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
        min_post_content_chars: int = 200,
        detail_candidate_limit: int = 5,
    ) -> None:
        if min_post_content_chars <= 0:
            raise ValueError("min_post_content_chars must be positive")
        if detail_candidate_limit <= 0:
            raise ValueError("detail_candidate_limit must be positive")
        self._client = client
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
        on_trace: XhsResearchTraceCallback | None = None,
    ) -> XhsResearchResult:
        search_started = perf_counter()
        try:
            search = await self._client.search_notes(keyword)
        except XhsMcpClientError as exc:
            _emit_trace(
                on_trace,
                XhsResearchTraceUpdate(
                    step="search_results",
                    title="小红书搜索失败",
                    status="failed",
                    duration_ms=_duration_ms(search_started),
                    data={"keyword": keyword, "error_code": exc.code},
                ),
            )
            raise _research_error(exc) from exc

        candidates = _select_candidates(search.items)
        _emit_trace(
            on_trace,
            XhsResearchTraceUpdate(
                step="search_results",
                title=f"小红书返回 {len(search.items)} 条搜索结果",
                status="success" if candidates else "failed",
                duration_ms=_duration_ms(search_started),
                data={
                    "keyword": search.keyword or keyword,
                    "sort_by": "most_liked",
                    "result_scope": "initial_results_only",
                    "total_count": len(search.items),
                    "candidate_count": len(candidates),
                    "usable_pool_limit": self._detail_candidate_limit,
                    "posts": _search_trace_posts(search.items, candidates),
                },
            ),
        )
        if on_search_complete is not None:
            on_search_complete(len(candidates))
        if not candidates:
            raise XhsResearchError(
                "NO_RESULTS",
                "没有找到可读取的小红书攻略笔记，请换个城市名称后重试。",
            )

        usable_posts: list[tuple[XhsSearchItem, XhsNoteDetailResult]] = []
        failed_details = 0
        cursor = 0
        while len(usable_posts) < self._detail_candidate_limit and cursor < len(candidates):
            batch_size = min(
                _DETAIL_BATCH_SIZE,
                self._detail_candidate_limit - len(usable_posts),
            )
            batch = candidates[cursor : cursor + batch_size]
            cursor += len(batch)
            attempts = await asyncio.gather(
                *(self._read_detail(item) for item in batch),
            )
            for attempt in attempts:
                item = attempt.item
                result = attempt.result
                if attempt.error is not None:
                    failed_details += 1
                    error_code = (
                        attempt.error.code
                        if isinstance(attempt.error, XhsMcpClientError)
                        else "DETAIL_READ_FAILED"
                    )
                    _emit_trace(
                        on_trace,
                        XhsResearchTraceUpdate(
                            step="post_detail",
                            title=item.title or "未命名笔记",
                            status="failed",
                            duration_ms=attempt.duration_ms,
                            data={
                                "note_id": item.note_id,
                                "search_rank": item.index + 1,
                                "result": "detail_failed",
                                "error_code": error_code,
                            },
                        ),
                    )
                    continue
                assert result is not None
                content = result.detail.description.strip()
                if len(content) < self._min_post_content_chars:
                    failed_details += 1
                    _emit_trace(
                        on_trace,
                        XhsResearchTraceUpdate(
                            step="post_detail",
                            title=result.detail.title or item.title or "未命名笔记",
                            status="skipped",
                            duration_ms=attempt.duration_ms,
                            data={
                                "note_id": result.detail.note_id or item.note_id,
                                "search_rank": item.index + 1,
                                "result": "content_too_short",
                                "content_chars": len(content),
                                "minimum_content_chars": self._min_post_content_chars,
                            },
                        ),
                    )
                    continue
                usable_posts.append((item, result))
                _emit_trace(
                    on_trace,
                    XhsResearchTraceUpdate(
                        step="post_detail",
                        title=result.detail.title or item.title or "未命名笔记",
                        status="success",
                        duration_ms=attempt.duration_ms,
                        data={
                            "note_id": result.detail.note_id or item.note_id,
                            "search_rank": item.index + 1,
                            "result": "usable",
                            "usable_pool_position": len(usable_posts),
                            "content_chars": len(content),
                            "content_preview": content[:240],
                        },
                    ),
                )

        if not usable_posts:
            raise XhsResearchError(
                "NO_USABLE_POSTS",
                "搜索到了相关笔记，但暂时没有可用于生成攻略的完整正文，请稍后重试。",
                retryable=True,
            )

        selected_posts = sorted(usable_posts, key=_usable_post_sort_key)[:_MAX_POSTS]
        queried_at = datetime.now(UTC)
        posts = [
            XhsPostEvidence(
                reference_id=f"source_{index}",
                role="primary" if index == 1 else "supplementary",
                note_id=result.detail.note_id or item.note_id,
                search_rank=item.index + 1,
                title=result.detail.title or item.title or "未命名笔记",
                author_name=result.detail.author.nickname or item.author.nickname or "未知作者",
                published_at=result.detail.published_at,
                content=result.detail.description.strip(),
                liked_count_raw=_liked_count_raw(item, result),
                liked_count=normalize_xhs_count(_liked_count_raw(item, result)),
                queried_at=queried_at,
            )
            for index, (item, result) in enumerate(selected_posts, start=1)
        ]
        warnings: list[str] = []
        if len(posts) == 1:
            warnings.append("本次只有一篇小红书笔记正文可用，方案依据相对有限。")
        if failed_details:
            warnings.append(f"有 {failed_details} 篇候选笔记未能读取，已跳过。")
        _emit_trace(
            on_trace,
            XhsResearchTraceUpdate(
                step="evidence_selected",
                title=f"最终采用 {len(posts)} 篇小红书笔记",
                status="success" if len(posts) == _MAX_POSTS else "partial",
                data={
                    "selection_strategy": "top_liked_from_usable_pool",
                    "usable_pool_count": len(usable_posts),
                    "usable_pool_limit": self._detail_candidate_limit,
                    "posts": [
                        {
                            "reference_id": post.reference_id,
                            "role": post.role,
                            "note_id": post.note_id,
                            "search_rank": post.search_rank,
                            "title": post.title,
                            "author_name": post.author_name,
                            "liked_count_raw": post.liked_count_raw,
                            "liked_count": post.liked_count,
                            "content_chars": len(post.content),
                        }
                        for post in posts
                    ],
                    "warnings": warnings,
                },
            ),
        )
        return XhsResearchResult(
            keyword=search.keyword or keyword,
            posts=posts,
            warnings=warnings,
        )

    async def _read_detail(self, item: XhsSearchItem) -> _DetailAttempt:
        started = perf_counter()
        try:
            result = await self._client.get_note_detail(item.note_id, item.xsec_token)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            return _DetailAttempt(
                item=item,
                result=None,
                error=exc,
                duration_ms=_duration_ms(started),
            )
        return _DetailAttempt(
            item=item,
            result=result,
            error=None,
            duration_ms=_duration_ms(started),
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
) -> list[XhsSearchItem]:
    readable = [
        item for item in items if item.detail_available and item.note_id and item.xsec_token
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
    return unique


def _candidate_sort_key(item: XhsSearchItem) -> tuple[bool, int, int]:
    liked_count = normalize_xhs_count(item.interactions.liked_count)
    return liked_count is None, -(liked_count or 0), item.index


def _liked_count_raw(
    item: XhsSearchItem,
    result: XhsNoteDetailResult,
) -> str | None:
    return item.interactions.liked_count or result.detail.interactions.liked_count or None


def _usable_post_sort_key(
    post: tuple[XhsSearchItem, XhsNoteDetailResult],
) -> tuple[bool, int, int]:
    item, result = post
    liked_count = normalize_xhs_count(_liked_count_raw(item, result))
    return liked_count is None, -(liked_count or 0), item.index


def _search_trace_posts(
    items: list[XhsSearchItem],
    candidates: list[XhsSearchItem],
) -> list[dict[str, Any]]:
    candidate_objects = {id(item) for item in candidates}
    candidate_note_ids = {item.note_id for item in candidates}
    posts: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda candidate: candidate.index):
        if id(item) in candidate_objects:
            selection_status = "candidate"
            reason_code = "selected_for_detail"
            reason = "进入详情候选"
        elif not item.detail_available:
            selection_status = "rejected"
            reason_code = "detail_unavailable"
            reason = "搜索结果缺少可读取详情"
        elif not item.note_id or not item.xsec_token:
            selection_status = "rejected"
            reason_code = "missing_access_fields"
            reason = "缺少详情访问参数"
        elif item.note_id in candidate_note_ids:
            selection_status = "rejected"
            reason_code = "duplicate_note"
            reason = "重复笔记"
        else:
            selection_status = "rejected"
            reason_code = "not_selected"
            reason = "未进入详情候选"
        posts.append(
            {
                "search_rank": item.index + 1,
                "note_id": item.note_id,
                "title": item.title or "未命名笔记",
                "author_name": item.author.nickname or "未知作者",
                "liked_count_raw": item.interactions.liked_count or None,
                "liked_count": normalize_xhs_count(item.interactions.liked_count),
                "detail_available": item.detail_available,
                "selection_status": selection_status,
                "reason_code": reason_code,
                "reason": reason,
            }
        )
    return posts


def _duration_ms(started: float) -> int:
    return max(0, int((perf_counter() - started) * 1000))


def _emit_trace(
    callback: XhsResearchTraceCallback | None,
    update: XhsResearchTraceUpdate,
) -> None:
    if callback is not None:
        callback(update)


__all__ = [
    "XhsReadClient",
    "XhsResearchError",
    "XhsResearchService",
    "XhsResearchTraceUpdate",
    "normalize_xhs_count",
]
