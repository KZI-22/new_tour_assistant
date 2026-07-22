from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from typing import Any, Literal, cast

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from app.clients.xhs_mcp_client import XhsLoginSessionResult
from app.core.settings import Settings
from app.graphs.xhs_trip_state import XhsTripPlanningState
from app.schemas.chat import ChatMessage
from app.schemas.tool_execution import (
    ChatStreamEvent,
    MessageDeltaEvent,
    PlanningStageEvent,
    PlanningTraceEvent,
    XhsLoginRequiredEvent,
)
from app.services.agent_executor import AgentExecutionError
from app.services.xhs_posts_renderer import render_xhs_posts
from app.services.xhs_research_service import (
    XhsResearchError,
    XhsResearchService,
    XhsResearchTraceUpdate,
)

logger = logging.getLogger(__name__)
_SEARCH_KEYWORD_MAX_CHARS = 200
_WHITESPACE_PATTERN = re.compile(r"\s+")


class XhsTripPlanningError(AgentExecutionError):
    pass


def build_search_keyword(latest_user_message: str) -> str:
    """Build the MCP keyword deterministically from the latest user message."""
    normalized = _WHITESPACE_PATTERN.sub(" ", latest_user_message).strip()
    return normalized[:_SEARCH_KEYWORD_MAX_CHARS].rstrip()


class XhsTripPlanner:
    def __init__(
        self,
        research_service: XhsResearchService,
        settings: Settings,
    ) -> None:
        self._research_service = research_service
        self._settings = settings

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        route_source: Literal["llm_router", "fallback", "explicit"] = "explicit",
    ) -> AsyncIterator[ChatStreamEvent]:
        run = _XhsPostSearchRun(
            messages=messages,
            research_service=self._research_service,
            settings=self._settings,
            route_source=route_source,
        )
        async for event in run.stream():
            yield event


class _XhsPostSearchRun:
    def __init__(
        self,
        *,
        messages: list[ChatMessage],
        research_service: XhsResearchService,
        settings: Settings,
        route_source: Literal["llm_router", "fallback", "explicit"],
    ) -> None:
        self._messages = messages
        self._research_service = research_service
        self._settings = settings
        self._route_source = route_source
        self._trace_sequence = 0
        self._graph = self._build_graph()

    def _trace_event(
        self,
        step: str,
        title: str,
        status: str,
        *,
        detail: str | None = None,
        duration_ms: int | None = None,
        data: dict[str, Any] | None = None,
    ) -> PlanningTraceEvent:
        self._trace_sequence += 1
        return PlanningTraceEvent(
            sequence=self._trace_sequence,
            step=step,
            title=title,
            status=status,
            detail=detail,
            duration_ms=duration_ms,
            data=data or {},
        )

    def _trace(
        self,
        writer: Any,
        step: str,
        title: str,
        status: str,
        *,
        detail: str | None = None,
        duration_ms: int | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        writer(
            self._trace_event(
                step,
                title,
                status,
                detail=detail,
                duration_ms=duration_ms,
                data=data,
            )
        )

    def _build_graph(self) -> Any:
        workflow = StateGraph(XhsTripPlanningState)
        workflow.add_node("check_xhs_login", self.check_xhs_login)
        workflow.add_node("start_xhs_login", self.start_xhs_login)
        workflow.add_node("wait_xhs_login", self.wait_xhs_login)
        workflow.add_node("collect_xhs_posts", self.collect_xhs_posts)
        workflow.add_node("finalize_response", self.finalize_response)

        workflow.add_edge(START, "check_xhs_login")
        workflow.add_conditional_edges(
            "check_xhs_login",
            self._route_after_login_check,
            {"logged_in": "collect_xhs_posts", "login_required": "start_xhs_login"},
        )
        workflow.add_conditional_edges(
            "start_xhs_login",
            self._route_after_login_start,
            {"logged_in": "collect_xhs_posts", "wait": "wait_xhs_login"},
        )
        workflow.add_edge("wait_xhs_login", "collect_xhs_posts")
        workflow.add_edge("collect_xhs_posts", "finalize_response")
        workflow.add_edge("finalize_response", END)
        return workflow.compile()

    async def stream(self) -> AsyncIterator[ChatStreamEvent]:
        latest_user_message = next(
            (message.content for message in reversed(self._messages) if message.role == "user"),
            "",
        )
        keyword = build_search_keyword(latest_user_message)
        if not keyword:
            raise XhsTripPlanningError(
                "XHS_SEARCH_KEYWORD_MISSING",
                "请先输入要在小红书搜索的内容。",
            )

        yield self._trace_event(
            "request_received",
            "收到小红书原帖检索请求",
            "success",
            data={
                "latest_user_message": latest_user_message[:2_000],
                "conversation_message_count": len(self._messages),
            },
        )
        yield self._trace_event(
            "route_selected",
            "请求已进入小红书原帖检索链路",
            "success",
            data={
                "route": "xhs_post_search",
                "route_source": self._route_source,
                "llm_used": False,
            },
        )
        yield self._trace_event(
            "search_query_built",
            "已从最新消息生成小红书搜索词",
            "success",
            data={
                "keyword": keyword,
                "keyword_source": "latest_user_message",
                "truncated": len(_WHITESPACE_PATTERN.sub(" ", latest_user_message).strip())
                > _SEARCH_KEYWORD_MAX_CHARS,
                "sort_by": "most_liked",
                "result_scope": "initial_results_only",
            },
        )
        initial: XhsTripPlanningState = {
            "messages": self._messages,
            "xhs_logged_in": False,
            "xhs_login_session": None,
            "search_keyword": keyword,
            "research": None,
            "final_answer": None,
        }
        async for event in self._graph.astream(initial, stream_mode="custom"):
            if hasattr(event, "type"):
                yield cast(ChatStreamEvent, event)

    async def check_xhs_login(self, _: XhsTripPlanningState) -> dict[str, Any]:
        writer = get_stream_writer()
        _stage(writer, "checking_xhs_login", "正在检查小红书登录状态", "running")
        try:
            login = await self._research_service.check_login()
        except XhsResearchError as exc:
            _stage(
                writer,
                "checking_xhs_login",
                "正在检查小红书登录状态",
                "failed",
                detail=exc.message,
            )
            raise XhsTripPlanningError(exc.code, exc.message) from exc
        _stage(
            writer,
            "checking_xhs_login",
            "正在检查小红书登录状态",
            "success",
            detail="小红书已登录。" if login.is_logged_in else "需要登录小红书。",
        )
        self._trace(
            writer,
            "login_checked",
            "小红书登录状态检查完成",
            "success" if login.is_logged_in else "partial",
            data={"is_logged_in": login.is_logged_in},
        )
        return {
            "xhs_logged_in": login.is_logged_in,
            "current_stage": "checking_xhs_login",
        }

    async def start_xhs_login(self, _: XhsTripPlanningState) -> dict[str, Any]:
        session = await self._start_login(get_stream_writer())
        return {
            "xhs_logged_in": session.status == "succeeded",
            "xhs_login_session": session,
            "current_stage": "waiting_xhs_login",
        }

    async def wait_xhs_login(self, state: XhsTripPlanningState) -> dict[str, Any]:
        session = state.get("xhs_login_session")
        if session is None:
            raise XhsTripPlanningError(
                "XHS_LOGIN_SESSION_MISSING",
                "小红书登录会话不可用，请重新发起检索。",
            )
        completed = await self._wait_login(get_stream_writer(), session)
        return {
            "xhs_logged_in": True,
            "xhs_login_session": completed,
            "current_stage": "waiting_xhs_login",
        }

    async def _start_login(self, writer: Any) -> XhsLoginSessionResult:
        _stage(writer, "waiting_xhs_login", "等待登录小红书", "running")
        try:
            session = await self._research_service.start_login()
        except XhsResearchError as exc:
            _stage(
                writer,
                "waiting_xhs_login",
                "等待登录小红书",
                "failed",
                detail=exc.message,
            )
            raise XhsTripPlanningError(exc.code, exc.message) from exc
        if session.status == "succeeded":
            _stage(
                writer,
                "waiting_xhs_login",
                "等待登录小红书",
                "success",
                detail="小红书登录成功。",
            )
            self._trace(
                writer,
                "login_completed",
                "小红书登录已恢复",
                "success",
                data={"status": "succeeded"},
            )
            return session
        if session.status != "pending":
            _raise_login_terminal(writer, session)
        writer(
            XhsLoginRequiredEvent(
                login_id=session.login_id,
                expires_at=session.expires_at,
                message=session.message or "请在已打开的 Google Chrome 中完成小红书登录。",
                fallback_available=True,
                fallback_mode="map_weather",
            )
        )
        return session

    async def _wait_login(
        self,
        writer: Any,
        session: XhsLoginSessionResult,
    ) -> XhsLoginSessionResult:
        try:
            while True:
                await asyncio.sleep(self._settings.xhs_login_poll_seconds)
                current = await self._research_service.get_login_status(session.login_id)
                if current.status == "pending":
                    continue
                if current.status == "succeeded":
                    _stage(
                        writer,
                        "waiting_xhs_login",
                        "等待登录小红书",
                        "success",
                        detail="小红书登录成功。",
                    )
                    self._trace(
                        writer,
                        "login_completed",
                        "小红书登录已恢复",
                        "success",
                        data={"status": "succeeded"},
                    )
                    return current
                _raise_login_terminal(writer, current)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(self._research_service.cancel_login(session.login_id))
            except Exception as exc:
                logger.warning(
                    "Could not cancel XHS login session exception_type=%s",
                    type(exc).__name__,
                )
            raise
        except XhsResearchError as exc:
            _stage(
                writer,
                "waiting_xhs_login",
                "等待登录小红书",
                "failed",
                detail=exc.message,
            )
            raise XhsTripPlanningError(exc.code, exc.message) from exc

    async def _recover_login_after_search(self, writer: Any) -> None:
        _stage(writer, "checking_xhs_login", "正在重新检查小红书登录状态", "running")
        try:
            login = await self._research_service.check_login()
        except XhsResearchError as exc:
            _stage(
                writer,
                "checking_xhs_login",
                "正在重新检查小红书登录状态",
                "failed",
                detail=exc.message,
            )
            raise XhsTripPlanningError(exc.code, exc.message) from exc
        _stage(
            writer,
            "checking_xhs_login",
            "正在重新检查小红书登录状态",
            "success",
        )
        if login.is_logged_in:
            return
        session = await self._start_login(writer)
        if session.status == "pending":
            await self._wait_login(writer, session)
        _stage(
            writer,
            "searching_xhs",
            "正在搜索小红书原帖",
            "running",
            detail="登录已恢复，正在重新搜索。",
        )

    async def collect_xhs_posts(self, state: XhsTripPlanningState) -> dict[str, Any]:
        keyword = state.get("search_keyword")
        if not keyword:
            raise XhsTripPlanningError(
                "XHS_SEARCH_KEYWORD_MISSING",
                "缺少小红书搜索词。",
            )
        writer = get_stream_writer()
        _stage(writer, "searching_xhs", "正在搜索小红书原帖", "running")
        reading_started = False

        def on_search_complete(candidate_count: int) -> None:
            nonlocal reading_started
            _stage(
                writer,
                "searching_xhs",
                "正在搜索小红书原帖",
                "success",
                detail=f"找到 {candidate_count} 篇可读取的候选笔记。",
            )
            if candidate_count:
                reading_started = True
                _stage(writer, "reading_xhs_posts", "正在读取小红书原帖正文", "running")

        def on_trace(update: XhsResearchTraceUpdate) -> None:
            self._trace(
                writer,
                update.step,
                update.title,
                update.status,
                duration_ms=update.duration_ms,
                data=update.data,
            )

        try:
            try:
                research = await self._research_service.collect(
                    keyword,
                    on_search_complete=on_search_complete,
                    on_trace=on_trace,
                )
            except XhsResearchError as exc:
                if exc.code != "NOT_LOGGED_IN":
                    raise
                await self._recover_login_after_search(writer)
                research = await self._research_service.collect(
                    keyword,
                    on_search_complete=on_search_complete,
                    on_trace=on_trace,
                )
        except XhsResearchError as exc:
            failed_stage = "reading_xhs_posts" if reading_started else "searching_xhs"
            display_name = "正在读取小红书原帖正文" if reading_started else "正在搜索小红书原帖"
            _stage(writer, failed_stage, display_name, "failed", detail=exc.message)
            raise XhsTripPlanningError(exc.code, exc.message) from exc

        _stage(
            writer,
            "reading_xhs_posts",
            "正在读取小红书原帖内容",
            "success",
            detail=(
                f"已读取 {len(research.posts)} 篇原帖和 "
                f"{sum(len(post.images) for post in research.posts)} 张图片。"
            ),
        )
        return {
            "research": research,
            "current_stage": "reading_xhs_posts",
        }

    async def finalize_response(self, state: XhsTripPlanningState) -> dict[str, Any]:
        research = state.get("research")
        if research is None:
            raise XhsTripPlanningError(
                "XHS_POSTS_MISSING",
                "小红书原帖读取失败，请稍后重试。",
            )
        writer = get_stream_writer()
        _stage(writer, "finalizing", "正在整理小红书原帖", "running")
        answer = render_xhs_posts(research)
        writer(MessageDeltaEvent(delta=answer))
        self._trace(
            writer,
            "response_completed",
            "小红书原帖已返回",
            "success",
            data={
                "output_chars": len(answer),
                "source_count": len(research.posts),
                "source_content_chars": sum(len(post.content) for post in research.posts),
                "source_image_count": sum(len(post.images) for post in research.posts),
                "llm_used": False,
            },
        )
        _stage(writer, "finalizing", "正在整理小红书原帖", "success")
        return {
            "final_answer": answer,
            "current_stage": "finalizing",
        }

    @staticmethod
    def _route_after_login_check(state: XhsTripPlanningState) -> str:
        return "logged_in" if state.get("xhs_logged_in") else "login_required"

    @staticmethod
    def _route_after_login_start(state: XhsTripPlanningState) -> str:
        session = state.get("xhs_login_session")
        return "logged_in" if session and session.status == "succeeded" else "wait"


def _stage(
    writer: Any,
    stage: str,
    display_name: str,
    status: str,
    *,
    detail: str | None = None,
) -> None:
    writer(
        PlanningStageEvent(
            stage=stage,
            display_name=display_name,
            status=status,
            detail=detail,
        )
    )


def _raise_login_terminal(writer: Any, session: XhsLoginSessionResult) -> None:
    errors = {
        "expired": (
            "XHS_LOGIN_EXPIRED",
            "小红书登录会话已过期，请重新发起检索。",
        ),
        "cancelled": (
            "XHS_LOGIN_CANCELLED",
            "小红书登录已取消，请重新发起检索。",
        ),
        "failed": (
            "XHS_LOGIN_FAILED",
            "小红书登录失败，请稍后重试。",
        ),
    }
    code, message = errors.get(
        session.status,
        ("XHS_LOGIN_FAILED", "小红书登录失败，请稍后重试。"),
    )
    _stage(
        writer,
        "waiting_xhs_login",
        "等待登录小红书",
        "failed",
        detail=session.message or message,
    )
    raise XhsTripPlanningError(code, message)


__all__ = ["XhsTripPlanner", "XhsTripPlanningError", "build_search_keyword"]
