from __future__ import annotations

from typing import TypedDict

from app.clients.xhs_mcp_client import XhsLoginSessionResult
from app.schemas.chat import ChatMessage
from app.schemas.xhs_planning import XhsResearchResult


class XhsTripPlanningState(TypedDict, total=False):
    messages: list[ChatMessage]
    xhs_logged_in: bool
    xhs_login_session: XhsLoginSessionResult | None
    search_keyword: str | None
    research: XhsResearchResult | None
    final_answer: str | None
    current_stage: str


__all__ = ["XhsTripPlanningState"]
