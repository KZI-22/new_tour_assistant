from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import uuid4

from app.core.model_registry import ModelRegistry
from app.schemas.platform_planning import (
    DirectTravelSearchResponse,
    TravelSearchPresentation,
)
from app.schemas.travel import FlightSearchInput, FlyAIResult, HotelSearchInput, TrainSearchInput
from app.schemas.trip_options import TripOptionSnapshot
from app.services.flyai_option_normalizer import (
    OptionNormalization,
    normalize_hotel_options,
    normalize_transport_options,
)
from app.services.structured_output_service import StructuredOutputService

_PRESENTATION_PROMPT = """你是旅游查询结果编辑。请根据给出的结构化事实，用中文写 1～3 句简洁说明。
可以概括结果数量和明显差异，但不得新增、修改或推测价格、时间、班次、车站、酒店信息、
库存或预订状态。不要输出链接，不要使用 Markdown 表格，不要替用户作最终选择。"""


class DirectTravelSearchClient(Protocol):
    async def search_hotel(self, query: HotelSearchInput) -> FlyAIResult: ...

    async def search_flight(self, query: FlightSearchInput) -> FlyAIResult: ...

    async def search_train(self, query: TrainSearchInput) -> FlyAIResult: ...


class DirectTravelSearchService:
    def __init__(
        self,
        client: DirectTravelSearchClient,
        registry: ModelRegistry,
        *,
        presentation_timeout_seconds: float = 30,
    ) -> None:
        self._client = client
        self._registry = registry
        self._presentation_timeout_seconds = presentation_timeout_seconds

    async def search_hotel(self, query: HotelSearchInput) -> DirectTravelSearchResponse:
        result = await self._client.search_hotel(query)
        normalization = normalize_hotel_options(result.data) if result.success else None
        return await self._response(
            kind="hotel",
            tool_name="search_hotel",
            query=query,
            result=result,
            normalization=normalization,
        )

    async def search_flight(self, query: FlightSearchInput) -> DirectTravelSearchResponse:
        result = await self._client.search_flight(query)
        normalization = (
            normalize_transport_options(result.data, mode="flight", direction="outbound")
            if result.success
            else None
        )
        return await self._response(
            kind="flight",
            tool_name="search_flight",
            query=query,
            result=result,
            normalization=normalization,
        )

    async def search_train(self, query: TrainSearchInput) -> DirectTravelSearchResponse:
        result = await self._client.search_train(query)
        normalization = (
            normalize_transport_options(result.data, mode="train", direction="outbound")
            if result.success
            else None
        )
        return await self._response(
            kind="train",
            tool_name="search_train",
            query=query,
            result=result,
            normalization=normalization,
        )

    async def _response(
        self,
        *,
        kind: Literal["hotel", "flight", "train"],
        tool_name: Literal["search_hotel", "search_flight", "search_train"],
        query: HotelSearchInput | FlightSearchInput | TrainSearchInput,
        result: FlyAIResult,
        normalization: OptionNormalization | None,
    ) -> DirectTravelSearchResponse:
        arguments = query.model_dump(mode="json")
        queried_at = datetime.now(UTC)
        if not result.success:
            return DirectTravelSearchResponse(
                kind=kind,
                tool_call_id=f"direct_{uuid4().hex}",
                tool_name=tool_name,
                arguments=arguments,
                success=False,
                summary="查询暂时失败，请稍后重试或调整查询条件。",
                error_code=result.error_code.value
                if result.error_code is not None
                else "UNKNOWN_ERROR",
                provider_error_code=None,
                queried_at=queried_at,
            )

        assert normalization is not None
        if not normalization.schema_valid:
            return DirectTravelSearchResponse(
                kind=kind,
                tool_call_id=f"direct_{uuid4().hex}",
                tool_name=tool_name,
                arguments=arguments,
                success=False,
                summary="查询服务返回了无法识别的数据，请稍后重试。",
                error_code="INVALID_PROVIDER_DATA",
                provider_item_count=normalization.provider_item_count,
                rejected_item_count=normalization.rejected_item_count,
                queried_at=queried_at,
            )

        options = list(normalization.normalized_options)
        summary = await self._present(kind, options)
        return DirectTravelSearchResponse(
            kind=kind,
            tool_call_id=f"direct_{uuid4().hex}",
            tool_name=tool_name,
            arguments=arguments,
            success=True,
            summary=summary,
            options=options,
            provider_item_count=normalization.provider_item_count,
            rejected_item_count=normalization.rejected_item_count,
            queried_at=queried_at,
        )

    async def _present(self, kind: str, options: list[TripOptionSnapshot]) -> str:
        if not options:
            return "没有找到符合当前条件的结果，可以尝试放宽筛选条件。"
        catalog = self._registry.list_models()
        selected = next(
            (
                item.id
                for item in catalog.models
                if item.available and item.id == catalog.default_model
            ),
            None,
        ) or next((item.id for item in catalog.models if item.available), None)
        if selected is None:
            return _fallback_summary(kind, len(options))
        model = self._registry.create_model(selected)
        facts = []
        for option in options:
            payload = option.model_dump(mode="json")
            payload.pop("detail_url", None)
            payload.pop("display_text", None)
            facts.append(payload)
        try:
            presentation = await StructuredOutputService(model).invoke(
                TravelSearchPresentation,
                _PRESENTATION_PROMPT,
                json.dumps(
                    {"kind": kind, "result_count": len(facts), "options": facts},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                timeout_seconds=self._presentation_timeout_seconds,
            )
            return presentation.summary
        except Exception:
            return _fallback_summary(kind, len(options))


def _fallback_summary(kind: str, count: int) -> str:
    labels = {"hotel": "酒店", "flight": "航班", "train": "火车班次"}
    return f"为你整理了 {count} 个{labels.get(kind, '查询')}结果，请结合时间和价格自行比较。"


__all__ = ["DirectTravelSearchClient", "DirectTravelSearchService"]
