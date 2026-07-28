from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from time import perf_counter
from typing import Any

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from app.clients.amap_cache import InMemoryAmapCache
from app.clients.amap_client import AmapClient
from app.clients.flyai_client import FlyAIClient
from app.core.model_registry import ModelRegistry
from app.core.request_context import build_time_context, use_request_context
from app.core.settings import Settings
from app.evals.trip_eval import EvalCase, EvalObservation, ToolExecution
from app.schemas.chat import ChatMessage
from app.schemas.context import TravelRequestContext
from app.schemas.tool_execution import (
    MessageDeltaEvent,
    PlanningTraceEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from app.services.chat_service import ChatService
from app.tools import build_travel_tools


@dataclass(slots=True)
class _RunRecorder:
    model_calls: list[dict[str, str]] = field(default_factory=list)
    provider_calls: list[ToolExecution] = field(default_factory=list)


class _CountingRunnable:
    def __init__(self, target: Any, recorder: _RunRecorder, label: str) -> None:
        self._target = target
        self._recorder = recorder
        self._label = label

    async def ainvoke(self, input_value: Any, **kwargs: Any) -> Any:
        self._recorder.model_calls.append({"label": self._label, "operation": "ainvoke"})
        return await self._target.ainvoke(input_value, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)


class _CountingModel(_CountingRunnable):
    def bind_tools(self, tools: Sequence[BaseTool], **kwargs: Any) -> _CountingModel:
        return _CountingModel(
            self._target.bind_tools(list(tools), **kwargs),
            self._recorder,
            self._label,
        )

    def with_structured_output(self, schema: type[BaseModel], **kwargs: Any) -> _CountingRunnable:
        target = self._target.with_structured_output(schema, **kwargs)
        return _CountingRunnable(
            target,
            self._recorder,
            f"{self._label}:{schema.__name__}",
        )

    async def astream(self, input_value: Any, **kwargs: Any) -> AsyncIterator[Any]:
        self._recorder.model_calls.append({"label": self._label, "operation": "astream"})
        async for item in self._target.astream(input_value, **kwargs):
            yield item


class _CountingRegistry:
    def __init__(self, target: ModelRegistry, recorder: _RunRecorder) -> None:
        self._target = target
        self._recorder = recorder

    def create_model(self, model_id: str) -> _CountingModel:
        return _CountingModel(
            self._target.create_model(model_id),
            self._recorder,
            f"main:{model_id}",
        )

    def create_router_model(self) -> tuple[_CountingModel, float]:
        model, timeout_seconds = self._target.create_router_model()
        return (
            _CountingModel(model, self._recorder, "router"),
            timeout_seconds,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)


class _RecordingProvider:
    def __init__(self, target: Any, recorder: _RunRecorder) -> None:
        self._target = target
        self._recorder = recorder

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._target, name)
        if not inspect.iscoroutinefunction(attribute):
            return attribute

        async def invoke(*args: Any, **kwargs: Any) -> Any:
            started = perf_counter()
            arguments = _call_arguments(args, kwargs)
            try:
                result = await attribute(*args, **kwargs)
            except Exception:
                self._recorder.provider_calls.append(
                    ToolExecution(
                        name=name,
                        arguments=arguments,
                        success=False,
                        schema_valid=None,
                        data_status="failed",
                        duration_ms=_duration_ms(started),
                        layer="provider",
                    )
                )
                raise
            success = bool(getattr(result, "success", True))
            data = getattr(result, "data", result)
            status = "failed" if not success else "empty" if _is_empty(data) else "usable"
            self._recorder.provider_calls.append(
                ToolExecution(
                    name=name,
                    arguments=arguments,
                    success=success,
                    schema_valid=True,
                    data_status=status,
                    duration_ms=_duration_ms(started),
                    layer="provider",
                )
            )
            return result

        return invoke


class LiveTripEvalRunner:
    def __init__(self, settings: Settings, *, model_id: str) -> None:
        self._settings = settings
        self._model_id = model_id
        self._amap_client: AmapClient | None = None
        self._flyai_client: FlyAIClient | None = None

    async def __aenter__(self) -> LiveTripEvalRunner:
        if not self._settings.amap_api_key:
            raise RuntimeError("AMAP_API_KEY is required for the configured 60-case live eval")
        self._amap_client = AmapClient(
            self._settings.amap_api_key,
            base_url=self._settings.amap_base_url,
            timeout_seconds=self._settings.amap_timeout_seconds,
            max_retries=self._settings.amap_max_retries,
            min_request_interval_seconds=self._settings.amap_min_request_interval_seconds,
            cache=InMemoryAmapCache(),
            cache_ttl_overrides=self._settings.amap_cache_ttl_overrides,
        )
        self._flyai_client = FlyAIClient(
            self._settings.flyai_cli_path,
            default_timeout_seconds=self._settings.flyai_timeout_seconds,
            max_concurrency=self._settings.flyai_max_concurrency,
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._amap_client is not None:
            await self._amap_client.aclose()

    async def run(self, case: EvalCase) -> EvalObservation:
        if self._amap_client is None or self._flyai_client is None:
            raise RuntimeError("LiveTripEvalRunner must be used as an async context manager")
        recorder = _RunRecorder()
        amap = _RecordingProvider(self._amap_client, recorder)
        flyai = _RecordingProvider(self._flyai_client, recorder)
        registry = _CountingRegistry(
            ModelRegistry(self._settings.model_config_path),
            recorder,
        )
        tools = build_travel_tools(flyai, amap)  # type: ignore[arg-type]
        service = ChatService(
            registry,  # type: ignore[arg-type]
            tools,
            max_tool_rounds=self._settings.max_tool_rounds,
            tool_timeout_seconds=self._settings.tool_execution_timeout_seconds,
            amap_client=amap,  # type: ignore[arg-type]
            flyai_client=flyai,  # type: ignore[arg-type]
            trip_planner_settings=self._settings,
        )
        messages = [
            ChatMessage(role=message.role, content=message.content) for message in case.messages
        ]
        context = TravelRequestContext(
            client_ip=None,
            client_ip_is_public_ipv4=False,
            time=build_time_context(
                self._settings.app_timezone,
                now=case.current_datetime,
            ),
        )
        events: list[Any] = []
        error: str | None = None
        started = perf_counter()
        try:
            with use_request_context(context):
                async for event in service.stream(self._model_id, messages):
                    events.append(event)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        duration_ms = _duration_ms(started)
        return _build_observation(
            case,
            events,
            recorder,
            duration_ms=duration_ms,
            error=error,
        )


def _build_observation(
    case: EvalCase,
    events: list[Any],
    recorder: _RunRecorder,
    *,
    duration_ms: int,
    error: str | None,
) -> EvalObservation:
    traces = [event for event in events if isinstance(event, PlanningTraceEvent)]
    tool_calls = [event for event in events if isinstance(event, ToolCallEvent)]
    tool_results = [event for event in events if isinstance(event, ToolResultEvent)]
    answer = "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
    route = (
        "trip_planner"
        if any(
            trace.step == "route_selected" and trace.data.get("route") == "trip_planner_graph"
            for trace in traces
        )
        else "general_agent"
    )
    fields = _extract_fields(traces, tool_calls)
    missing_fields = _extract_missing_fields(traces)
    validation_events = [trace for trace in traces if trace.step == "validation_completed"]
    validation_passed = validation_events[-1].status == "success" if validation_events else None
    requirements_validated = next(
        (trace for trace in reversed(traces) if trace.step == "requirements_validated"),
        None,
    )
    requirements_complete = (
        bool(requirements_validated.data.get("complete"))
        if requirements_validated is not None
        else None
    )
    response_completed = any(trace.step == "response_completed" for trace in traces)
    if route == "trip_planner" and requirements_complete is False and answer:
        outcome = "clarify"
    elif route == "trip_planner" and validation_passed is True and response_completed:
        outcome = "plan"
    elif route == "trip_planner" and validation_passed is False:
        outcome = "controlled_failure"
    elif error is not None:
        outcome = "failed"
    else:
        outcome = "answer"

    provider_names = [execution.name for execution in recorder.provider_calls]
    if route == "trip_planner":
        observed_tools = [
            *(
                ["collect_map_weather"]
                if any(trace.step == "evidence_selected" for trace in traces)
                else []
            ),
            *(
                name
                for name in provider_names
                if name in {"search_flight", "search_train", "search_hotel"}
            ),
        ]
        executions = recorder.provider_calls
    else:
        observed_tools = [event.tool_name for event in tool_calls]
        executions = _agent_tool_executions(tool_calls, tool_results)

    operation_statuses = _operation_statuses(
        route,
        traces,
        tool_results,
        observed_tools,
    )
    return EvalObservation(
        case_id=case.id,
        category=case.category,
        route=route,
        outcome=outcome,
        fields=fields,
        missing_fields=missing_fields,
        observed_tools=list(dict.fromkeys(observed_tools)),
        tool_executions=executions,
        operation_statuses=operation_statuses,
        model_call_count=len(recorder.model_calls),
        tool_call_count=len(set(observed_tools)),
        provider_call_count=len(recorder.provider_calls),
        duration_ms=duration_ms,
        validation_passed=validation_passed,
        fact_reference_consistent=validation_passed,
        answer=answer,
        error=error,
    )


def _extract_fields(
    traces: list[PlanningTraceEvent],
    tool_calls: list[ToolCallEvent],
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    mappings = {
        "destination_city": "core.destination_city",
        "duration_days": "core.duration_days",
        "start_date": "core.start_date",
        "transport_action": "transport.action",
        "transport_modes": "transport.modes",
        "transport_journey_scope": "transport.journey_scope",
        "transport_origin_city": "transport.origin_city",
        "transport_enabled": "transport.enabled",
        "journey_scope": "transport.journey_scope",
        "transport_origin": "transport.origin",
        "transport_destination": "transport.destination",
        "transport_outbound_date": "transport.outbound_date",
        "transport_return_date": "transport.return_date",
        "hotel_action": "hotel.action",
        "hotel_enabled": "hotel.enabled",
        "hotel_destination": "hotel.destination",
        "hotel_check_in_date": "hotel.check_in_date",
        "hotel_check_out_date": "hotel.check_out_date",
        "hotel_nearby_poi": "hotel.nearby_poi",
    }
    for trace in traces:
        if trace.step != "requirements_extracted":
            continue
        for source, destination in mappings.items():
            if source in trace.data:
                fields[destination] = trace.data[source]
    for call in tool_calls:
        for key, value in call.arguments.items():
            fields.setdefault(f"tool.{call.tool_name}.{key}", value)
    return fields


def _extract_missing_fields(traces: list[PlanningTraceEvent]) -> list[str]:
    for trace in reversed(traces):
        if trace.step == "requirements_validated":
            raw = trace.data.get("missing_fields", [])
            return [str(item) for item in raw] if isinstance(raw, list) else []
    return []


def _agent_tool_executions(
    calls: list[ToolCallEvent],
    results: list[ToolResultEvent],
) -> list[ToolExecution]:
    calls_by_id = {call.tool_call_id: call for call in calls}
    executions: list[ToolExecution] = []
    for result in results:
        call = calls_by_id.get(result.tool_call_id)
        schema_valid = result.data_status != "invalid" and result.error_code not in {
            "INVALID_TOOL_ARGUMENTS",
            "UNKNOWN_TOOL",
        }
        status = (
            result.data_status
            if result.data_status is not None
            else "usable"
            if result.success
            else "failed"
        )
        executions.append(
            ToolExecution(
                name=result.tool_name,
                arguments=call.arguments if call is not None else {},
                success=result.success,
                schema_valid=schema_valid,
                data_status=status,
                duration_ms=result.duration_ms,
                layer="agent_tool",
            )
        )
    return executions


def _operation_statuses(
    route: str,
    traces: list[PlanningTraceEvent],
    results: list[ToolResultEvent],
    observed_tools: list[str],
) -> dict[str, str]:
    if route == "general_agent":
        return {
            f"{result.tool_name}:{index}": (
                result.data_status
                if result.data_status is not None
                else "usable"
                if result.success
                else "failed"
            )
            for index, result in enumerate(results)
        }
    evidence_trace = next(
        (trace for trace in reversed(traces) if trace.step == "evidence_selected"),
        None,
    )
    if evidence_trace is None:
        return {}
    statuses = {"collect_map_weather": str(evidence_trace.data.get("map_weather_status", "failed"))}
    transport_status = str(evidence_trace.data.get("transport_status", "skipped"))
    for name in {"search_flight", "search_train"} & set(observed_tools):
        statuses[name] = transport_status
    if "search_hotel" in observed_tools:
        statuses["search_hotel"] = str(evidence_trace.data.get("hotel_status", "skipped"))
    return statuses


def _call_arguments(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if args:
        if len(args) == 1:
            value = _jsonable(args[0])
            result = value if isinstance(value, dict) else {"value": value}
        else:
            result["args"] = [_jsonable(value) for value in args]
    result.update({key: _jsonable(value) for key, value in kwargs.items()})
    return result


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _is_empty(value: Any) -> bool:
    return value is None or value == [] or value == {} or value == ""


def _duration_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1_000))


__all__ = ["LiveTripEvalRunner"]
