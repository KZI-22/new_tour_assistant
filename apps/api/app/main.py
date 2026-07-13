from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.clients.flyai_client import FlyAIClient
from app.core.model_registry import ModelRegistry
from app.core.settings import Settings, get_settings
from app.db.session import create_database
from app.services.chat_service import ChatService
from app.services.conversation_service import ConversationService
from app.tools import build_travel_tools


def create_app(settings: Settings | None = None) -> FastAPI:
    current_settings = settings or get_settings()
    logging.basicConfig(
        level=current_settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    database_engine = None
    conversation_service = None
    if current_settings.database_url:
        database_engine, session_factory = create_database(current_settings.database_url)
        conversation_service = ConversationService(session_factory)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
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

    registry = ModelRegistry(current_settings.model_config_path)
    flyai_client = FlyAIClient(
        current_settings.flyai_cli_path,
        default_timeout_seconds=current_settings.flyai_timeout_seconds,
        max_concurrency=current_settings.flyai_max_concurrency,
    )
    application.state.model_registry = registry
    application.state.chat_service = ChatService(registry)
    application.state.conversation_service = conversation_service
    application.state.flyai_client = flyai_client
    application.state.travel_tools = build_travel_tools(flyai_client)
    application.include_router(router)
    return application


app = create_app()
