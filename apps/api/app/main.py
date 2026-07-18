from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.clients.amap_client import AmapClient
from app.clients.flyai_client import FlyAIClient
from app.core.model_registry import ModelRegistry
from app.core.request_context import RequestContextMiddleware
from app.core.settings import Settings, get_settings
from app.db.session import create_database
from app.services.chat_service import ChatService
from app.services.conversation_service import ConversationService
from app.services.tool_call_log_service import ToolCallLogService
from app.services.trip_plan_service import TripPlanService
from app.tools import build_travel_tools


def create_app(settings: Settings | None = None) -> FastAPI:
    current_settings = settings or get_settings()
    logging.basicConfig(
        level=current_settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    database_engine = None
    conversation_service = None
    tool_call_log_service = None
    trip_plan_service = None
    if current_settings.database_url:
        database_engine, session_factory = create_database(current_settings.database_url)
        conversation_service = ConversationService(session_factory)
        tool_call_log_service = ToolCallLogService(session_factory)
        trip_plan_service = TripPlanService(session_factory)

    amap_client = None
    if current_settings.amap_api_key:
        amap_client = AmapClient(
            current_settings.amap_api_key,
            base_url=current_settings.amap_base_url,
            timeout_seconds=current_settings.amap_timeout_seconds,
            max_retries=current_settings.amap_max_retries,
            min_request_interval_seconds=current_settings.amap_min_request_interval_seconds,
        )
    else:
        logging.getLogger(__name__).warning(
            "Amap tools are disabled because AMAP_API_KEY is not configured."
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if amap_client is not None:
            await amap_client.aclose()
        if database_engine is not None:
            await database_engine.dispose()

    application = FastAPI(
        title=current_settings.app_name,
        version="0.1.0",
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(current_settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
    application.add_middleware(
        RequestContextMiddleware,
        trusted_proxy_cidrs=current_settings.trusted_proxy_cidrs,
        timezone=current_settings.app_timezone,
    )

    registry = ModelRegistry(current_settings.model_config_path)
    flyai_client = FlyAIClient(
        current_settings.flyai_cli_path,
        default_timeout_seconds=current_settings.flyai_timeout_seconds,
        max_concurrency=current_settings.flyai_max_concurrency,
    )
    travel_tools = build_travel_tools(flyai_client, amap_client)
    application.state.model_registry = registry
    application.state.chat_service = ChatService(
        registry,
        travel_tools,
        max_tool_rounds=current_settings.max_tool_rounds,
        tool_timeout_seconds=current_settings.tool_execution_timeout_seconds,
        tool_call_log_writer=tool_call_log_service,
        trip_plan_service=trip_plan_service,
        trip_planner_settings=current_settings,
    )
    application.state.conversation_service = conversation_service
    application.state.tool_call_log_service = tool_call_log_service
    application.state.trip_plan_service = trip_plan_service
    application.state.flyai_client = flyai_client
    application.state.amap_client = amap_client
    application.state.travel_tools = travel_tools
    application.include_router(router)
    return application


app = create_app()
