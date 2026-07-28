from __future__ import annotations

from datetime import date
from time import perf_counter

import pytest
from app.graphs.trip_planner_nodes import ExtractRequirementsNode
from app.schemas.chat import ChatMessage
from app.schemas.trip_capabilities import (
    CapabilityAction,
    JourneyScope,
    TransportMode,
)
from app.services.rule_first_trip_requirement_extractor import (
    RuleFirstTripRequirementExtractor,
    TripAmbiguityResolution,
    extract_trip_request_by_rules,
)
from langchain_core.messages import AIMessage


def _messages(*contents: str) -> list[ChatMessage]:
    return [ChatMessage(role="user", content=content) for content in contents]


class _Runnable:
    def __init__(self, value: object) -> None:
        self._value = value

    async def ainvoke(self, _: object) -> object:
        return self._value


class _NeverCalledModel:
    def with_structured_output(self, _: type[object]) -> object:
        raise AssertionError("LLM must not be called for an unambiguous request")


class _AmbiguityModel:
    def __init__(
        self,
        native_value: object,
        *,
        fallback_response: AIMessage | None = None,
    ) -> None:
        self.native_value = native_value
        self.fallback_response = fallback_response
        self.schemas: list[str] = []
        self.fallback_calls = 0

    def with_structured_output(self, schema: type[object]) -> _Runnable:
        self.schemas.append(schema.__name__)
        return _Runnable(self.native_value)

    async def ainvoke(self, _: object) -> AIMessage:
        self.fallback_calls += 1
        return self.fallback_response or AIMessage(content="not-json")


def test_core_acceptance_sample_is_fully_parsed_by_rules() -> None:
    result = extract_trip_request_by_rules(
        _messages("去西安三天旅游攻略，顺便查一下去西安的高铁，以及西安北站附近的酒店。")
    )

    request = result.request
    assert request.core.destination_city == "西安"
    assert request.core.duration_days == 3
    assert request.core.start_date is None
    assert request.transport.action is CapabilityAction.ENABLE
    assert request.transport.modes == [TransportMode.TRAIN]
    assert request.transport.journey_scope is JourneyScope.ROUND_TRIP
    assert request.transport.origin_city is None
    assert request.hotel.action is CapabilityAction.ENABLE
    assert request.hotel.nearby_poi == "西安北站"
    assert result.ambiguities == []
    assert result.explicit_missing == [
        "core.start_date",
        "transport.origin_city",
    ]


@pytest.mark.parametrize(
    "text",
    [
        "做一个去杭州的三日旅游规划",
        "做一个去杭州的三日旅游攻略",
        "做一个去杭州的三日旅行计划",
        "做一个去杭州的3日攻略",
        "做一个去杭州的三日旅游规划/攻略",
    ],
)
def test_duration_is_extracted_from_trip_planning_and_guide_phrases(text: str) -> None:
    result = extract_trip_request_by_rules(_messages(text))

    assert result.request.core.destination_city == "杭州"
    assert result.request.core.duration_days == 3


def test_clause_scoped_negation_keeps_train_and_hotel_enabled() -> None:
    result = extract_trip_request_by_rules(_messages("不要飞机，帮我查北京到西安的高铁和酒店"))

    assert result.request.core.destination_city == "西安"
    assert result.request.transport.origin_city == "北京"
    assert result.request.transport.action is CapabilityAction.ENABLE
    assert result.request.transport.modes == [TransportMode.TRAIN]
    assert result.request.hotel.action is CapabilityAction.ENABLE


@pytest.mark.asyncio
async def test_clear_but_missing_request_does_not_call_llm() -> None:
    extractor = RuleFirstTripRequirementExtractor(  # type: ignore[arg-type]
        _NeverCalledModel(),
        timeout_seconds=1,
    )

    result = await extractor.extract(_messages("帮我做三天攻略，顺便查高铁"))

    assert result.metrics.path == "rules"
    assert result.metrics.llm_call_count == 0
    assert result.explicit_missing == [
        "core.destination_city",
        "core.start_date",
        "transport.origin_city",
    ]


@pytest.mark.asyncio
async def test_ambiguous_destination_uses_only_small_resolution_schema() -> None:
    model = _AmbiguityModel(
        TripAmbiguityResolution(destination_city="苏州"),
    )
    extractor = RuleFirstTripRequirementExtractor(  # type: ignore[arg-type]
        model,
        timeout_seconds=1,
    )

    result = await extractor.extract(_messages("我想去上海或者苏州玩三天"))

    assert model.schemas == ["TripAmbiguityResolution"]
    assert result.request.core.destination_city == "苏州"
    assert result.request.core.duration_days == 3
    assert result.metrics.path == "hybrid"
    assert result.metrics.llm_call_count == 1
    assert result.field_sources["core.destination_city"] == "llm_resolution"


def test_alternative_destination_and_origin_are_detected_by_field() -> None:
    destination_result = extract_trip_request_by_rules(_messages("上海或苏州三日游"))
    origin_result = extract_trip_request_by_rules(_messages("从北京或上海出发去西安玩三天，查高铁"))

    assert destination_result.request.core.destination_city is None
    assert destination_result.ambiguities[0].field == "core.destination_city"
    assert destination_result.ambiguities[0].candidates == ["上海", "苏州"]
    assert origin_result.request.core.destination_city == "西安"
    assert origin_result.request.transport.origin_city is None
    assert any(
        item.field == "transport.origin_city" and item.candidates == ["北京", "上海"]
        for item in origin_result.ambiguities
    )


@pytest.mark.asyncio
async def test_invalid_native_resolution_retries_once_with_json_fallback() -> None:
    model = _AmbiguityModel(
        {"duration_days": 0},
        fallback_response=AIMessage(content='{"destination_city":"上海"}'),
    )
    extractor = RuleFirstTripRequirementExtractor(  # type: ignore[arg-type]
        model,
        timeout_seconds=1,
    )

    result = await extractor.extract(_messages("我想去上海或者苏州玩三天"))

    assert result.request.core.destination_city == "上海"
    assert result.metrics.llm_call_count == 2
    assert result.metrics.llm_retry_count == 1
    assert model.fallback_calls == 1


@pytest.mark.asyncio
async def test_failed_ambiguity_resolution_stays_missing_for_clarification() -> None:
    model = _AmbiguityModel({"duration_days": 0})
    extractor = RuleFirstTripRequirementExtractor(  # type: ignore[arg-type]
        model,
        timeout_seconds=1,
    )

    result = await extractor.extract(_messages("我想去上海或者苏州玩三天"))

    assert result.request.core.destination_city is None
    assert "core.destination_city" in result.explicit_missing
    assert result.metrics.llm_call_count == 2
    assert result.metrics.llm_retry_count == 1


def test_multi_turn_context_is_inherited_and_latest_correction_wins() -> None:
    result = extract_trip_request_by_rules(
        _messages(
            "帮我规划成都三日游",
            "改成西安四日游，2027-08-01 开始，从南京出发查高铁",
        )
    )

    assert result.request.core.destination_city == "西安"
    assert result.request.core.duration_days == 4
    assert result.request.core.start_date == date(2027, 8, 1)
    assert result.request.transport.origin_city == "南京"
    assert result.request.transport.modes == [TransportMode.TRAIN]
    assert result.field_sources["core.destination_city"] == "explicit_rule"


def test_short_city_reply_and_unique_pronoun_reuse_context_without_ambiguity() -> None:
    short_reply = extract_trip_request_by_rules(_messages("想做三天城市攻略", "西安"))
    pronoun_reply = extract_trip_request_by_rules(_messages("想去成都玩三天", "那就去那里吧"))

    assert short_reply.request.core.destination_city == "西安"
    assert short_reply.ambiguities == []
    assert pronoun_reply.request.core.destination_city == "成都"
    assert pronoun_reply.ambiguities == []


def test_explicit_date_range_derives_start_and_duration() -> None:
    result = extract_trip_request_by_rules(
        _messages("2027-08-01 至 2027-08-04 去杭州旅游，推荐酒店")
    )

    assert result.request.core.destination_city == "杭州"
    assert result.request.core.start_date == date(2027, 8, 1)
    assert result.request.core.duration_days == 4
    assert result.request.hotel.check_in_date is None
    assert result.request.hotel.check_out_date is None


@pytest.mark.asyncio
async def test_extract_node_exposes_rule_path_metrics_without_llm() -> None:
    node = ExtractRequirementsNode(  # type: ignore[arg-type]
        _NeverCalledModel(),
        timeout_seconds=1,
    )

    result = await node(
        {
            "messages": _messages("2027-08-01 开始去西安玩三天"),
        }
    )

    assert result["extraction_method"] == "rules"
    assert result["extraction_details"]["llm_call_count"] == 0
    assert result["request"].core.destination_city == "西安"


def test_rules_path_p95_is_below_fifty_milliseconds() -> None:
    messages = _messages("去西安三天旅游攻略，顺便查一下去西安的高铁，以及西安北站附近的酒店。")
    durations: list[float] = []
    for _ in range(100):
        started = perf_counter()
        extract_trip_request_by_rules(messages)
        durations.append((perf_counter() - started) * 1_000)

    p95 = sorted(durations)[94]
    assert p95 < 50
