from __future__ import annotations

import os
from datetime import date, timedelta

import pytest
from app.clients.flyai_client import FlyAIClient
from app.core.settings import get_settings
from app.schemas.travel import FlightSearchInput


@pytest.mark.flyai
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_FLYAI_TESTS") != "1",
    reason="Set RUN_FLYAI_TESTS=1 to use the configured FlyAI request quota.",
)
async def test_real_flyai_flight_search() -> None:
    settings = get_settings()
    query = FlightSearchInput(
        origin=os.getenv("FLYAI_TEST_ORIGIN", "上海"),
        destination=os.getenv("FLYAI_TEST_DESTINATION", "北京"),
        departure_date=date.fromisoformat(
            os.getenv(
                "FLYAI_TEST_DEPARTURE_DATE",
                (date.today() + timedelta(days=14)).isoformat(),
            )
        ),
    )
    client = FlyAIClient(
        settings.flyai_cli_path,
        default_timeout_seconds=settings.flyai_timeout_seconds,
        max_concurrency=settings.flyai_max_concurrency,
    )

    result = await client.search_flight(query, timeout_seconds=90)

    assert result.success, result.model_dump(mode="json")
