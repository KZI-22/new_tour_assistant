from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any, Literal

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)


class XhsMcpClientError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class _BoundaryModel(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class _Author(_BoundaryModel):
    nickname: str = ""


class _Interactions(_BoundaryModel):
    liked_count: str = ""
    collected_count: str = ""


class XhsMcpToolResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    structured_content: dict[str, Any]


class XhsLoginStatusResult(_BoundaryModel):
    is_logged_in: bool
    checked_at: str


class XhsLoginSessionResult(_BoundaryModel):
    login_id: str = Field(repr=False)
    status: Literal["pending", "succeeded", "expired", "cancelled", "failed"]
    created_at: str
    expires_at: str
    is_logged_in: bool = False
    message: str = ""


class XhsSearchItem(_BoundaryModel):
    note_id: str
    xsec_token: str = Field(repr=False)
    detail_available: bool
    index: int = Field(ge=0)
    title: str = ""
    author: _Author = Field(default_factory=_Author)
    interactions: _Interactions = Field(default_factory=_Interactions)


class XhsSearchResult(_BoundaryModel):
    keyword: str
    items: list[XhsSearchItem] = Field(default_factory=list)


class XhsNoteDetail(_BoundaryModel):
    note_id: str
    title: str = ""
    description: str = ""
    published_at: str | None = None
    author: _Author = Field(default_factory=_Author)
    interactions: _Interactions = Field(default_factory=_Interactions)


class XhsNoteDetailResult(_BoundaryModel):
    note_id: str
    detail: XhsNoteDetail


class XhsMcpClient:
    """Small typed boundary around the remote Streamable HTTP MCP server."""

    def __init__(
        self,
        url: str,
        *,
        auth_token: str | None = None,
        timeout_seconds: float = 75,
    ) -> None:
        normalized_url = url.strip()
        if not normalized_url:
            raise ValueError("XHS MCP URL cannot be empty")
        if timeout_seconds <= 0:
            raise ValueError("XHS MCP timeout must be positive")
        self._url = normalized_url
        self._auth_token = auth_token.strip() if auth_token and auth_token.strip() else None
        self._timeout_seconds = timeout_seconds
        self._session: ClientSession | None = None
        self._session_stack: AsyncExitStack | None = None
        self._session_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._reset_session()

    async def search_notes(self, keyword: str) -> XhsSearchResult:
        response = await self._call_tool(
            "xhs_search_notes",
            {
                "keyword": keyword,
                "sort_by": "most_liked",
            },
        )
        try:
            return XhsSearchResult.model_validate(response.structured_content)
        except ValidationError as exc:
            raise XhsMcpClientError(
                "INVALID_RESPONSE",
                "小红书搜索服务返回了无法识别的数据。",
            ) from exc

    async def get_note_detail(
        self,
        note_id: str,
        xsec_token: str,
    ) -> XhsNoteDetailResult:
        response = await self._call_tool(
            "xhs_get_note_detail",
            {
                "note_id": note_id,
                "xsec_token": xsec_token,
                "comment_mode": "none",
            },
        )
        try:
            return XhsNoteDetailResult.model_validate(response.structured_content)
        except ValidationError as exc:
            raise XhsMcpClientError(
                "INVALID_RESPONSE",
                "小红书笔记详情服务返回了无法识别的数据。",
            ) from exc

    async def check_login(self) -> XhsLoginStatusResult:
        response = await self._call_tool("xhs_check_login", {})
        try:
            return XhsLoginStatusResult.model_validate(response.structured_content)
        except ValidationError as exc:
            raise XhsMcpClientError(
                "INVALID_RESPONSE",
                "小红书登录检查返回了无法识别的数据。",
            ) from exc

    async def start_login(self) -> XhsLoginSessionResult:
        response = await self._call_tool(
            "xhs_start_login",
            {"force_restart": False},
        )
        try:
            session = XhsLoginSessionResult.model_validate(response.structured_content)
        except ValidationError as exc:
            raise XhsMcpClientError(
                "INVALID_RESPONSE",
                "小红书网页登录服务返回了无法识别的数据。",
            ) from exc
        return session

    async def get_login_status(self, login_id: str) -> XhsLoginSessionResult:
        normalized_id = login_id.strip()
        if not normalized_id:
            raise ValueError("login_id cannot be empty")
        response = await self._call_tool(
            "xhs_get_login_status",
            {"login_id": normalized_id},
        )
        try:
            return XhsLoginSessionResult.model_validate(response.structured_content)
        except ValidationError as exc:
            raise XhsMcpClientError(
                "INVALID_RESPONSE",
                "小红书登录状态服务返回了无法识别的数据。",
            ) from exc

    async def cancel_login(self, login_id: str) -> XhsLoginSessionResult:
        normalized_id = login_id.strip()
        if not normalized_id:
            raise ValueError("login_id cannot be empty")
        response = await self._call_tool(
            "xhs_cancel_login",
            {"login_id": normalized_id},
        )
        try:
            return XhsLoginSessionResult.model_validate(response.structured_content)
        except ValidationError as exc:
            raise XhsMcpClientError(
                "INVALID_RESPONSE",
                "小红书登录取消服务返回了无法识别的数据。",
            ) from exc

    async def _call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> XhsMcpToolResponse:
        session: ClientSession | None = None
        read_timeout = timedelta(seconds=self._timeout_seconds)

        try:
            session = await self._get_session()
            result = await session.call_tool(
                name,
                arguments=arguments,
                read_timeout_seconds=read_timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if session is not None:
                await self._reset_session(expected=session)
            logger.warning(
                "XHS MCP call failed tool=%s exception_type=%s",
                name,
                type(exc).__name__,
            )
            raise XhsMcpClientError(
                "MCP_UNAVAILABLE",
                "小红书内容服务当前不可用，请稍后重试。",
                retryable=True,
            ) from exc

        structured = result.structuredContent
        if result.isError:
            payload = structured if isinstance(structured, Mapping) else {}
            code = _safe_error_code(payload.get("code"))
            message = _safe_error_message(code)
            retryable = bool(payload.get("retryable"))
            raise XhsMcpClientError(code, message, retryable=retryable)
        if not isinstance(structured, Mapping):
            raise XhsMcpClientError(
                "INVALID_RESPONSE",
                "小红书内容服务没有返回结构化数据。",
            )
        return XhsMcpToolResponse(
            structured_content=dict(structured),
        )

    async def _get_session(self) -> ClientSession:
        if self._session is not None:
            return self._session

        async with self._session_lock:
            if self._session is not None:
                return self._session

            stack = AsyncExitStack()
            try:
                http_client = await stack.enter_async_context(
                    httpx.AsyncClient(
                        headers=self._headers(),
                        timeout=httpx.Timeout(self._timeout_seconds),
                        follow_redirects=False,
                    )
                )
                read_stream, write_stream, _ = await stack.enter_async_context(
                    streamable_http_client(
                        self._url,
                        http_client=http_client,
                        terminate_on_close=True,
                    )
                )
                session = await stack.enter_async_context(
                    ClientSession(
                        read_stream,
                        write_stream,
                        read_timeout_seconds=timedelta(seconds=self._timeout_seconds),
                    )
                )
                await session.initialize()
            except BaseException:
                await stack.aclose()
                raise

            self._session_stack = stack
            self._session = session
            return session

    async def _reset_session(self, *, expected: ClientSession | None = None) -> None:
        async with self._session_lock:
            if expected is not None and self._session is not expected:
                return
            stack = self._session_stack
            self._session = None
            self._session_stack = None
            if stack is not None:
                await stack.aclose()

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._auth_token is not None:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        return headers


def _safe_error_code(value: Any) -> str:
    if isinstance(value, str) and value.isascii() and 1 <= len(value) <= 80:
        return value
    return "MCP_ERROR"


def _safe_error_message(code: str) -> str:
    if code in {"NOT_LOGGED_IN", "LOGIN_EXPIRED"}:
        return "小红书内容服务尚未登录，请在本机 Chrome 中完成登录后重试。"
    if code == "TIMEOUT":
        return "读取小红书内容超时，请稍后重试。"
    if code == "RISK_CONTROL":
        return "小红书当前限制了内容读取，请稍后重试。"
    if code == "PAGE_STRUCTURE_CHANGED":
        return "小红书搜索或详情页面结构已变化，当前读取规则需要更新。"
    if code == "NOTE_UNAVAILABLE":
        return "暂时无法读取所选小红书笔记，请稍后重试。"
    return "小红书内容服务当前不可用，请稍后重试。"


__all__ = [
    "XhsMcpClient",
    "XhsMcpClientError",
    "XhsLoginSessionResult",
    "XhsLoginStatusResult",
    "XhsMcpToolResponse",
    "XhsNoteDetailResult",
    "XhsSearchItem",
    "XhsSearchResult",
]
