from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import perf_counter
from typing import Protocol

from app.schemas.travel import (
    FlightSearchInput,
    FlyAIErrorCode,
    FlyAIResult,
    TrainSearchInput,
)
from app.schemas.trip_capabilities import (
    JourneyScope,
    TransportCapabilityPlan,
    TransportMode,
)
from app.schemas.trip_evidence import EvidenceStatus, RawCapabilityEvidence
from app.services.flyai_option_normalizer import (
    OptionNormalization,
    normalize_transport_options,
)

_PROVIDER_SCHEMA_ERROR = "PROVIDER_SCHEMA_INVALID"


class FlyAITransportClient(Protocol):
    async def search_flight(self, query: FlightSearchInput) -> FlyAIResult: ...

    async def search_train(self, query: TrainSearchInput) -> FlyAIResult: ...


class IntercityTransportService:
    def __init__(self, client: FlyAITransportClient) -> None:
        self._client = client

    async def search(
        self,
        plan: TransportCapabilityPlan,
    ) -> RawCapabilityEvidence:
        queried_at = datetime.now(UTC)
        started = perf_counter()
        if not plan.enabled:
            return _transport_evidence(
                status=EvidenceStatus.SKIPPED,
                queried_at=queried_at,
                started=started,
                query={"enabled": False, "queries": []},
            )
        if (
            not plan.modes
            or plan.origin is None
            or plan.destination is None
            or plan.outbound_date is None
        ):
            return _transport_evidence(
                status=EvidenceStatus.FAILED,
                queried_at=queried_at,
                started=started,
                query={"enabled": True, "queries": []},
                warnings=["交通执行计划缺少必要字段，未调用供应商。"],
                error_code="INVALID_CAPABILITY_PLAN",
            )

        calls = _build_calls(plan, self._client)
        tasks = [
            asyncio.create_task(
                _execute_call(
                    search,
                    query,
                    mode=mode,
                    direction=direction,
                )
            )
            for mode, direction, query, search in calls
        ]
        try:
            results = await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            await _cancel_tasks(*tasks)
            raise

        query_summary = {
            "enabled": True,
            "queries": [
                _transport_query_summary(mode, direction, query)
                for mode, direction, query, _ in calls
            ],
        }
        usable = [item for item in results if item["normalization"].usable]
        failures = [item for item in results if item["error_code"] is not None]
        if usable:
            display_options = [
                option for item in usable for option in item["normalization"].options
            ]
            normalized_options = [
                option
                for item in usable
                for option in item["normalization"].normalized_options
            ]
            return _transport_evidence(
                status=EvidenceStatus.USABLE,
                queried_at=queried_at,
                started=started,
                query=query_summary,
                data={
                    "results": [
                        {
                            "mode": item["mode"],
                            "direction": item["direction"],
                            "data": item["result"].data,
                        }
                        for item in usable
                    ]
                },
                normalized_options=normalized_options,
                display_options=display_options,
                warnings=[
                    f"{item['mode']} {item['direction']} 查询失败（{item['error_code']}）。"
                    for item in failures
                ],
            )
        if failures:
            return _transport_evidence(
                status=EvidenceStatus.FAILED,
                queried_at=queried_at,
                started=started,
                query=query_summary,
                warnings=[
                    f"{item['mode']} {item['direction']} 查询失败（{item['error_code']}）。"
                    for item in failures
                ],
                error_code=str(failures[0]["error_code"]),
            )
        return _transport_evidence(
            status=EvidenceStatus.EMPTY,
            queried_at=queried_at,
            started=started,
            query=query_summary,
            warnings=["交通供应商返回空结果。"],
        )


def _build_calls(
    plan: TransportCapabilityPlan,
    client: FlyAITransportClient,
) -> list[
    tuple[
        TransportMode,
        str,
        FlightSearchInput | TrainSearchInput,
        Callable[
            [FlightSearchInput | TrainSearchInput],
            Awaitable[FlyAIResult],
        ],
    ]
]:
    assert plan.origin is not None
    assert plan.destination is not None
    assert plan.outbound_date is not None
    calls: list[
        tuple[
            TransportMode,
            str,
            FlightSearchInput | TrainSearchInput,
            Callable[
                [FlightSearchInput | TrainSearchInput],
                Awaitable[FlyAIResult],
            ],
        ]
    ] = []
    for mode in plan.modes:
        query_type = FlightSearchInput if mode is TransportMode.FLIGHT else TrainSearchInput
        search = client.search_flight if mode is TransportMode.FLIGHT else client.search_train
        outbound = query_type(
            origin=plan.origin,
            destination=plan.destination,
            departure_date=plan.outbound_date,
            max_price=plan.max_price,
        )
        calls.append((mode, "outbound", outbound, search))
        if plan.journey_scope is JourneyScope.ROUND_TRIP and plan.return_date is not None:
            inbound = query_type(
                origin=plan.destination,
                destination=plan.origin,
                departure_date=plan.return_date,
                max_price=plan.max_price,
            )
            calls.append((mode, "return", inbound, search))
    return calls


async def _execute_call(
    search: Callable[
        [FlightSearchInput | TrainSearchInput],
        Awaitable[FlyAIResult],
    ],
    query: FlightSearchInput | TrainSearchInput,
    *,
    mode: TransportMode,
    direction: str,
) -> dict[str, object]:
    try:
        result = await search(query)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        result = FlyAIResult(
            success=False,
            command=[],
            error_code=FlyAIErrorCode.UNKNOWN_ERROR,
            error_message=f"provider exception: {type(exc).__name__}",
            duration_ms=0,
        )
    normalization = (
        normalize_transport_options(
            result.data,
            mode=mode.value,
            direction=direction,
        )
        if result.success
        else OptionNormalization(
            options=(),
            provider_item_count=0,
            rejected_item_count=0,
            schema_valid=False,
        )
    )
    return {
        "mode": mode.value,
        "direction": direction,
        "result": result,
        "normalization": normalization,
        "error_code": (
            _error_code(result)
            if not result.success
            else _PROVIDER_SCHEMA_ERROR
            if not normalization.schema_valid
            or (normalization.provider_item_count > 0 and not normalization.options)
            else None
        ),
    }


def _transport_query_summary(
    mode: TransportMode,
    direction: str,
    query: FlightSearchInput | TrainSearchInput,
) -> dict[str, object]:
    return {
        "mode": mode.value,
        "direction": direction,
        "origin": query.origin,
        "destination": query.destination,
        "departure_date": query.departure_date.isoformat(),
        "max_price": query.max_price,
    }


def _transport_evidence(
    *,
    status: EvidenceStatus,
    queried_at: datetime,
    started: float,
    query: dict[str, object],
    data: object | None = None,
    normalized_options: list[object] | None = None,
    display_options: list[str] | None = None,
    warnings: list[str] | None = None,
    error_code: str | None = None,
) -> RawCapabilityEvidence:
    return RawCapabilityEvidence(
        capability="transport",
        status=status,
        query=query,
        queried_at=queried_at,
        duration_ms=max(0, round((perf_counter() - started) * 1_000)),
        data=data,
        normalized_options=normalized_options or [],
        display_options=display_options or [],
        warnings=warnings or [],
        error_code=error_code,
    )


def _error_code(result: FlyAIResult) -> str:
    return result.error_code.value if result.error_code is not None else "UNKNOWN_ERROR"


async def _cancel_tasks(*tasks: asyncio.Task[object]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


__all__ = [
    "FlyAITransportClient",
    "IntercityTransportService",
]
