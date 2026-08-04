from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from app.api.auth_routes import router as auth_router
from app.api.routes import router
from app.clients.amap_cache import AmapCache, InMemoryAmapCache, RedisAmapCache
from app.clients.amap_client import AmapClient
from app.clients.flyai_client import FlyAIClient
from app.clients.xhs_mcp_client import XhsMcpClient
from app.core.model_registry import ModelRegistry
from app.core.request_context import RequestContextMiddleware
from app.core.security import JwtCodec
from app.core.settings import Settings, get_settings
from app.db.session import create_database
from app.graphs.structured_trip_planner import StructuredTripPlanner
from app.services.auth_service import AuthService
from app.services.chat_service import ChatService
from app.services.conversation_service import ConversationService
from app.services.direct_travel_search_service import DirectTravelSearchService
from app.services.map_trip_collection_service import MapTripCollectionService
from app.services.map_weather_collection_service import MapWeatherCollectionService
from app.services.otp_service import OtpService
from app.services.otp_store import RedisOtpChallengeStore
from app.services.restaurant_recommendation_service import RestaurantRecommendationService
from app.services.tool_call_log_service import ToolCallLogService
from app.services.trip_plan_persistence_service import TripPlanPersistenceService
from app.services.weather_evidence_service import WeatherEvidenceService
from app.services.xhs_research_service import XhsResearchService
from app.tools import build_travel_assistant_tools, build_travel_tools


def create_app(settings: Settings | None = None) -> FastAPI:
    current_settings = settings or get_settings()
    logging.basicConfig(
        level=current_settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    database_engine = None
    session_factory = None
    conversation_service = None
    tool_call_log_service = None
    trip_plan_persistence_service = None
    if current_settings.database_url:
        database_engine, session_factory = create_database(current_settings.database_url)
        conversation_service = ConversationService(session_factory)
        tool_call_log_service = ToolCallLogService(session_factory)
        trip_plan_persistence_service = TripPlanPersistenceService(session_factory)

    # Created independently of authentication: the shared Amap cache needs it too.
    redis_client = (
        Redis.from_url(current_settings.redis_url, decode_responses=True)
        if current_settings.redis_url
        else None
    )

    auth_service = None
    otp_service = None
    if current_settings.auth_enabled:
        assert session_factory is not None
        assert redis_client is not None
        assert current_settings.auth_jwt_secret is not None
        assert current_settings.auth_hmac_secret is not None
        jwt_codec = JwtCodec(
            current_settings.auth_jwt_secret,
            issuer=current_settings.auth_jwt_issuer,
            audience=current_settings.auth_jwt_audience,
            access_token_minutes=current_settings.auth_access_token_minutes,
        )
        auth_service = AuthService(
            session_factory,
            jwt_codec,
            refresh_token_days=current_settings.auth_refresh_token_days,
        )
        otp_service = OtpService(
            RedisOtpChallengeStore(redis_client),
            hmac_secret=current_settings.auth_hmac_secret,
            ttl_seconds=current_settings.auth_otp_ttl_seconds,
            resend_seconds=current_settings.auth_otp_resend_seconds,
            max_attempts=current_settings.auth_otp_max_attempts,
            phone_limit=current_settings.auth_otp_phone_limit,
            ip_limit=current_settings.auth_otp_ip_limit,
            rate_window_seconds=current_settings.auth_otp_rate_window_seconds,
            expose_debug_code=current_settings.app_environment in {"local", "test"},
        )

    amap_cache: AmapCache = (
        RedisAmapCache(redis_client) if redis_client is not None else InMemoryAmapCache()
    )
    amap_client = None
    if current_settings.amap_api_key:
        amap_client = AmapClient(
            current_settings.amap_api_key,
            base_url=current_settings.amap_base_url,
            timeout_seconds=current_settings.amap_timeout_seconds,
            max_retries=current_settings.amap_max_retries,
            min_request_interval_seconds=current_settings.amap_min_request_interval_seconds,
            cache=amap_cache,
            cache_ttl_overrides=current_settings.amap_cache_ttl_overrides,
        )
    else:
        logging.getLogger(__name__).warning(
            "Amap tools are disabled because AMAP_API_KEY is not configured."
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if xhs_mcp_client is not None:
            await xhs_mcp_client.aclose()
        if amap_client is not None:
            await amap_client.aclose()
        if redis_client is not None:
            await redis_client.aclose()
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
    direct_travel_search_service = DirectTravelSearchService(
        flyai_client,
        registry,
        presentation_timeout_seconds=current_settings.direct_search_presentation_timeout_seconds,
    )
    travel_tools = build_travel_tools(flyai_client, amap_client)
    travel_assistant_tools = build_travel_assistant_tools(flyai_client, amap_client)
    structured_trip_planner = None
    if current_settings.trip_planner_enabled and amap_client is not None:
        map_service = MapTripCollectionService(
            amap_client,
            poi_max_concurrency=current_settings.amap_poi_max_concurrency,
            route_max_concurrency=current_settings.amap_route_max_concurrency,
            poi_page_size=current_settings.amap_poi_page_size,
            max_raw_candidates=current_settings.max_raw_poi_candidates,
            max_transit_transfers=current_settings.max_transit_transfers,
            max_transit_duration_minutes=current_settings.max_transit_duration_minutes,
            max_walk_distance_meters=current_settings.max_walk_distance_meters,
            cluster_max_iterations=current_settings.trip_planning_cluster_max_iterations,
            data_timeout_seconds=current_settings.trip_planning_data_timeout_seconds,
        )
        structured_trip_planner = StructuredTripPlanner(
            MapWeatherCollectionService(
                map_service,
                WeatherEvidenceService(amap_client),
                weather_timeout_seconds=current_settings.trip_planning_data_timeout_seconds,
            ),
            RestaurantRecommendationService(amap_client),
            current_settings,
            version_writer=trip_plan_persistence_service,
        )
    xhs_mcp_client = None
    xhs_research_service = None
    if current_settings.trip_planner_enabled:
        xhs_mcp_client = XhsMcpClient(
            current_settings.xhs_mcp_url,
            transport=current_settings.xhs_mcp_transport,
            auth_token=current_settings.xhs_mcp_auth_token,
            timeout_seconds=current_settings.xhs_mcp_timeout_seconds,
            stdio_command=current_settings.xhs_mcp_stdio_command,
            stdio_args=current_settings.xhs_mcp_stdio_args,
            stdio_cwd=current_settings.xhs_mcp_stdio_cwd,
        )
        xhs_research_service = XhsResearchService(
            xhs_mcp_client,
            min_post_content_chars=current_settings.xhs_min_post_content_chars,
            detail_candidate_limit=current_settings.xhs_detail_candidate_limit,
        )
    application.state.model_registry = registry
    application.state.settings = current_settings
    application.state.auth_service = auth_service
    application.state.jwt_codec = jwt_codec if current_settings.auth_enabled else None
    application.state.otp_service = otp_service
    application.state.redis_client = redis_client
    application.state.amap_cache = amap_cache
    application.state.chat_service = ChatService(
        registry,
        travel_tools,
        plan_tools=travel_assistant_tools,
        max_tool_rounds=current_settings.max_tool_rounds,
        tool_timeout_seconds=current_settings.tool_execution_timeout_seconds,
        tool_call_log_writer=tool_call_log_service,
        xhs_research_service=xhs_research_service,
        amap_client=amap_client,
        flyai_client=flyai_client,
        trip_planner_settings=current_settings,
        trip_plan_version_writer=trip_plan_persistence_service,
    )
    application.state.conversation_service = conversation_service
    application.state.tool_call_log_service = tool_call_log_service
    application.state.trip_plan_persistence_service = trip_plan_persistence_service
    application.state.structured_trip_planner = structured_trip_planner
    application.state.direct_travel_search_service = direct_travel_search_service
    application.state.xhs_mcp_client = xhs_mcp_client
    application.state.xhs_research_service = xhs_research_service
    application.state.flyai_client = flyai_client
    application.state.amap_client = amap_client
    application.state.travel_tools = travel_tools
    application.state.travel_assistant_tools = travel_assistant_tools
    application.include_router(auth_router)
    application.include_router(router)
    return application


app = create_app()
