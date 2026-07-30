from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from time import perf_counter
from typing import Protocol

from app.schemas.travel import FlyAIResult, HotelSearchInput
from app.schemas.trip_capabilities import HotelCapabilityPlan
from app.schemas.trip_evidence import EvidenceStatus, RawCapabilityEvidence
from app.services.flyai_option_normalizer import normalize_hotel_options

_PROVIDER_SCHEMA_ERROR = "PROVIDER_SCHEMA_INVALID"


class FlyAIHotelClient(Protocol):
    async def search_hotel(self, query: HotelSearchInput) -> FlyAIResult: ...


class HotelSearchService:
    def __init__(self, client: FlyAIHotelClient) -> None:
        self._client = client

    async def search(self, plan: HotelCapabilityPlan) -> RawCapabilityEvidence:
        queried_at = datetime.now(UTC)
        started = perf_counter()
        if not plan.enabled:
            return _hotel_evidence(
                status=EvidenceStatus.SKIPPED,
                queried_at=queried_at,
                started=started,
                query={"enabled": False},
            )
        if plan.destination is None or plan.check_in_date is None or plan.check_out_date is None:
            return _hotel_evidence(
                status=EvidenceStatus.FAILED,
                queried_at=queried_at,
                started=started,
                query={"enabled": True},
                warnings=["酒店执行计划缺少必要字段，未调用供应商。"],
                error_code="INVALID_CAPABILITY_PLAN",
            )

        query = HotelSearchInput(
            destination=plan.destination,
            check_in_date=plan.check_in_date,
            check_out_date=plan.check_out_date,
            keywords=plan.keywords,
            nearby_poi=plan.nearby_poi,
            hotel_stars=tuple(plan.hotel_stars),
            max_price=plan.max_nightly_price,
        )
        summary: dict[str, object] = {
            "enabled": True,
            "destination": query.destination,
            "check_in_date": query.check_in_date.isoformat(),
            "check_out_date": query.check_out_date.isoformat(),
            "keywords": query.keywords,
            "nearby_poi": query.nearby_poi,
            "hotel_stars": list(query.hotel_stars),
            "max_price": query.max_price,
        }
        try:
            result = await self._client.search_hotel(query)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _hotel_evidence(
                status=EvidenceStatus.FAILED,
                queried_at=queried_at,
                started=started,
                query=summary,
                warnings=[f"酒店供应商调用异常（{type(exc).__name__}）。"],
                error_code="PROVIDER_EXCEPTION",
            )

        normalization = normalize_hotel_options(result.data) if result.success else None
        if normalization is not None and normalization.usable:
            return _hotel_evidence(
                status=EvidenceStatus.USABLE,
                queried_at=queried_at,
                started=started,
                query=summary,
                data=result.data,
                normalized_options=list(normalization.normalized_options),
                display_options=list(normalization.options),
            )
        if normalization is not None and normalization.empty:
            return _hotel_evidence(
                status=EvidenceStatus.EMPTY,
                queried_at=queried_at,
                started=started,
                query=summary,
                warnings=["酒店供应商返回空结果。"],
            )
        if normalization is not None:
            return _hotel_evidence(
                status=EvidenceStatus.FAILED,
                queried_at=queried_at,
                started=started,
                query=summary,
                warnings=["酒店供应商返回了无法识别的数据结构。"],
                error_code=_PROVIDER_SCHEMA_ERROR,
            )
        error_code = result.error_code.value if result.error_code is not None else "UNKNOWN_ERROR"
        return _hotel_evidence(
            status=EvidenceStatus.FAILED,
            queried_at=queried_at,
            started=started,
            query=summary,
            warnings=[f"酒店查询失败（{error_code}）。"],
            error_code=error_code,
        )


def _hotel_evidence(
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
        capability="hotel",
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


__all__ = ["FlyAIHotelClient", "HotelSearchService"]
