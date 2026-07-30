from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from typing import cast

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool

from app.clients.amap_client import AmapClient
from app.clients.flyai_client import FlyAIClient
from app.core.model_registry import ModelRegistry, UnavailableModelError
from app.core.request_context import get_request_context
from app.core.settings import Settings
from app.graphs.standard_trip_planner import StandardTripPlanner
from app.graphs.xhs_trip_planner import XhsTripPlanner
from app.schemas.chat import ChatMessage
from app.schemas.tool_execution import ChatStreamEvent
from app.schemas.trip_planning import PlanningSource
from app.services.agent_executor import (
    MAX_TOOL_ROUNDS,
    AgentExecutionError,
    AgentExecutor,
    ToolEnabledModel,
)
from app.services.agent_state_service import AgentStateStore
from app.services.map_trip_collection_service import MapTripCollectionService
from app.services.multi_agent_orchestrator import MultiAgentOrchestrator
from app.services.tool_call_log_service import ToolCallLogWriter
from app.services.tool_execution import ToolExecutionContext, ToolExecutor
from app.services.trip_plan_persistence_service import TripPlanVersionWriter
from app.services.trip_request_router import TripRequestRouter
from app.services.weather_evidence_service import WeatherEvidenceService
from app.services.xhs_research_service import XhsResearchService

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = """你是旅游规划助手。请清晰、诚实地回答用户。

当问题涉及实时或外部数据时，必须优先调用已提供的工具，而不是依赖模型记忆，包括：
- 航班、火车和酒店
- 景点和 POI
- 天气、路线、距离与预计耗时
- 当前城市

当工具缺少不可可靠推测的必填参数时，先向用户追问，不要编造参数：
- 航班和火车至少需要出发地、目的地与日期
- 酒店至少需要城市、入住日期与退房日期
- 路线至少需要起点与终点
相对日期可以结合系统提供的当前日期和时区解释。
只问“今天天气”且没有城市时，可以先使用 IP 城市推测工具，
但必须明确说明定位结果只是城市级网络位置推测，不是精确位置。

工具执行失败时，如实说明失败；可以使用同一轮中其他成功结果继续回答，
但不得伪造查询结果或声称已经查到实际数据。
清楚区分实时查询结果与一般建议，并提醒价格、余票、天气和耗时可能变化。
不要向用户展示内部错误、API Key、环境变量、供应商原始响应、内部 URL、
命令、技术堆栈或本机路径。

普通问候、概念解释和创作文案等不需要外部数据的问题应直接回答，不要调用工具。
禁止声称已完成预订或任何未实际执行的外部操作。"""


def _to_langchain_messages(messages: list[ChatMessage]) -> list[BaseMessage]:
    prompt = DEFAULT_SYSTEM_PROMPT
    if request_context := get_request_context():
        current = request_context.time
        prompt = (
            f"{prompt}\n\n当前日期时间：{current.current_datetime.isoformat()}；"
            f"时区：{current.timezone}；星期：{current.weekday}。"
        )

    converted: list[BaseMessage] = [SystemMessage(content=prompt)]
    message_types = {
        "system": SystemMessage,
        "user": HumanMessage,
        "assistant": AIMessage,
    }
    converted.extend(message_types[item.role](content=item.content) for item in messages)
    return converted


class ChatService:
    def __init__(
        self,
        registry: ModelRegistry,
        tools: Sequence[BaseTool],
        *,
        max_tool_rounds: int = MAX_TOOL_ROUNDS,
        tool_timeout_seconds: float = 130,
        tool_call_log_writer: ToolCallLogWriter | None = None,
        xhs_research_service: XhsResearchService | None = None,
        amap_client: AmapClient | None = None,
        flyai_client: FlyAIClient | None = None,
        trip_planner_settings: Settings | None = None,
        trip_plan_version_writer: TripPlanVersionWriter | None = None,
        agent_state_store: AgentStateStore | None = None,
    ) -> None:
        self._registry = registry
        self._trip_request_router = TripRequestRouter(registry)
        self._tools = tuple(tools)
        self._max_tool_rounds = max_tool_rounds
        self._tool_executor = ToolExecutor(
            self._tools,
            timeout_seconds=tool_timeout_seconds,
            log_writer=tool_call_log_writer,
        )
        self._multi_agent = None
        if trip_planner_settings and trip_planner_settings.multi_agent_enabled:
            self._multi_agent = MultiAgentOrchestrator(
                registry.create_model,
                self._tools,
                max_tool_rounds=max_tool_rounds,
                supervisor_timeout_seconds=(
                    trip_planner_settings.multi_agent_supervisor_timeout_seconds
                ),
                agent_timeout_seconds=trip_planner_settings.multi_agent_agent_timeout_seconds,
                tool_timeout_seconds=tool_timeout_seconds,
                log_writer=tool_call_log_writer,
                state_store=agent_state_store,
            )
        self._trip_planner = None
        self._standard_trip_planner = None
        weather_service = WeatherEvidenceService(amap_client)
        if trip_planner_settings and trip_planner_settings.trip_planner_enabled:
            self._standard_trip_planner = StandardTripPlanner(
                collection_service=MapTripCollectionService(
                    amap_client,
                    poi_max_concurrency=trip_planner_settings.amap_poi_max_concurrency,
                    route_max_concurrency=trip_planner_settings.amap_route_max_concurrency,
                    poi_page_size=trip_planner_settings.amap_poi_page_size,
                    max_raw_candidates=trip_planner_settings.max_raw_poi_candidates,
                    max_transit_transfers=trip_planner_settings.max_transit_transfers,
                    max_transit_duration_minutes=(
                        trip_planner_settings.max_transit_duration_minutes
                    ),
                    max_walk_distance_meters=(trip_planner_settings.max_walk_distance_meters),
                    cluster_max_iterations=(
                        trip_planner_settings.trip_planning_cluster_max_iterations
                    ),
                    data_timeout_seconds=(trip_planner_settings.trip_planning_data_timeout_seconds),
                ),
                weather_service=weather_service,
                flyai_client=flyai_client,
                settings=trip_planner_settings,
                version_writer=trip_plan_version_writer,
            )
        if (
            trip_planner_settings
            and trip_planner_settings.trip_planner_enabled
            and xhs_research_service is not None
        ):
            self._trip_planner = XhsTripPlanner(
                research_service=xhs_research_service,
                settings=trip_planner_settings,
            )
        elif trip_planner_settings and trip_planner_settings.trip_planner_enabled:
            logger.warning(
                "XHS post search is disabled because the research service is unavailable."
            )

    async def stream(
        self,
        model_id: str,
        messages: list[ChatMessage],
        *,
        planning_source: PlanningSource = "standard",
        execution_context: ToolExecutionContext | None = None,
    ) -> AsyncIterator[ChatStreamEvent]:
        if planning_source == "xhs":
            if self._trip_planner is None:
                raise AgentExecutionError(
                    "XHS_PLANNING_DISABLED",
                    "小红书原帖检索功能当前未启用。",
                )
            async for event in self._trip_planner.stream(
                messages,
                route_source="explicit",
            ):
                yield event
            return

        if self._multi_agent is not None:
            async for event in self._multi_agent.stream(
                model_id,
                messages,
                execution_context=execution_context,
            ):
                yield event
            return

        model = self._registry.create_model(model_id)
        if self._standard_trip_planner is not None or self._trip_planner is not None:
            route = await self._trip_request_router.route(messages)
            if route.route == "trip_planner":
                if self._standard_trip_planner is None:
                    raise AgentExecutionError(
                        "MAP_PLANNING_DISABLED",
                        "标准地图与天气规划功能当前未启用。",
                    )
                planner = self._standard_trip_planner
                async for event in planner.stream(
                    model,
                    messages,
                    route_source=route.source,
                    execution_context=execution_context,
                ):
                    yield event
                return

        try:
            bound_model = model.bind_tools(list(self._tools))
        except Exception as exc:
            logger.warning(
                "Model does not support tool calling model_id=%s exception_type=%s",
                model_id,
                type(exc).__name__,
            )
            raise UnavailableModelError(
                f"The selected model '{model_id}' does not support tool calling."
            ) from None

        executor = AgentExecutor(
            cast(ToolEnabledModel, bound_model),
            self._tool_executor,
            max_tool_rounds=self._max_tool_rounds,
        )
        async for event in executor.stream(
            _to_langchain_messages(messages),
            execution_context=execution_context,
        ):
            yield event


__all__ = ["ChatService", "DEFAULT_SYSTEM_PROMPT"]
