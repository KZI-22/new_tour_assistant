from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvalMessage(EvalModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class ExpectedResult(EvalModel):
    route: Literal["general_agent", "trip_planner"]
    outcome: Literal["answer", "clarify", "plan", "controlled_failure"]
    required_tools: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    fields: dict[str, Any] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    subjective: bool = False

    @model_validator(mode="after")
    def validate_tool_sets(self) -> ExpectedResult:
        required = set(self.required_tools)
        allowed = set(self.allowed_tools)
        forbidden = set(self.forbidden_tools)
        if required & forbidden:
            raise ValueError("required_tools and forbidden_tools must not overlap")
        if allowed & forbidden:
            raise ValueError("allowed_tools and forbidden_tools must not overlap")
        if self.outcome == "clarify" and self.required_tools:
            raise ValueError("clarification cases cannot require tool calls in the same turn")
        if self.outcome == "plan" and self.route != "trip_planner":
            raise ValueError("plan outcomes must use trip_planner")
        return self


class EvalCase(EvalModel):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    category: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    messages: list[EvalMessage] = Field(min_length=1)
    current_datetime: datetime = datetime.fromisoformat("2026-07-28T10:00:00+08:00")
    expected: ExpectedResult

    @field_validator("messages")
    @classmethod
    def require_final_user_message(cls, value: list[EvalMessage]) -> list[EvalMessage]:
        if value[-1].role != "user":
            raise ValueError("the final eval message must be from the user")
        return value


class ToolExecution(EvalModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    success: bool
    schema_valid: bool | None = None
    data_status: Literal["usable", "partial", "empty", "invalid", "failed"] | None = None
    duration_ms: int = Field(default=0, ge=0)
    layer: Literal["agent_tool", "provider"]


class EvalObservation(EvalModel):
    case_id: str
    category: str
    route: Literal["general_agent", "trip_planner"]
    outcome: Literal["answer", "clarify", "plan", "controlled_failure", "failed"]
    fields: dict[str, Any] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    observed_tools: list[str] = Field(default_factory=list)
    tool_executions: list[ToolExecution] = Field(default_factory=list)
    operation_statuses: dict[str, str] = Field(default_factory=dict)
    model_call_count: int = Field(default=0, ge=0)
    tool_call_count: int = Field(default=0, ge=0)
    provider_call_count: int = Field(default=0, ge=0)
    duration_ms: int = Field(ge=0)
    validation_passed: bool | None = None
    fact_reference_consistent: bool | None = None
    answer: str = ""
    error: str | None = None


class EvalMetrics(EvalModel):
    case_count: int
    route_accuracy: float
    outcome_accuracy: float
    necessary_tool_recall: float | None
    extra_tool_call_rate: float | None
    cases_with_extra_tool_calls_rate: float
    parameter_field_accuracy: float | None
    exact_field_case_accuracy: float | None
    missing_requirement_accuracy: float | None
    tool_execution_success_rate: float | None
    schema_valid_rate: float | None
    result_usable_rate: float | None
    final_plan_first_or_revised_validation_pass_rate: float | None
    fact_reference_consistency_rate: float | None
    end_to_end_p50_ms: int
    end_to_end_p95_ms: int
    average_model_call_count: float
    average_tool_call_count: float
    average_provider_call_count: float


class CategoryMetrics(EvalModel):
    case_count: int
    route_accuracy: float
    outcome_accuracy: float
    average_duration_ms: float


class EvalReport(EvalModel):
    generated_at: datetime
    metrics: EvalMetrics
    by_category: dict[str, CategoryMetrics]
    failed_case_ids: list[str]
    notes: list[str]


def load_eval_cases(path: Path, *, expected_count: int | None = None) -> list[EvalCase]:
    cases: list[EvalCase] = []
    seen_ids: set[str] = set()
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            case = EvalCase.model_validate_json(line)
        except Exception as exc:
            raise ValueError(f"{path}:{line_number}: invalid eval case: {exc}") from exc
        if case.id in seen_ids:
            raise ValueError(f"{path}:{line_number}: duplicate eval case id: {case.id}")
        seen_ids.add(case.id)
        cases.append(case)
    if expected_count is not None and len(cases) != expected_count:
        raise ValueError(f"expected {expected_count} eval cases, found {len(cases)}")
    return cases


def load_observations(path: Path) -> list[EvalObservation]:
    observations: list[EvalObservation] = []
    seen_ids: set[str] = set()
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            observation = EvalObservation.model_validate_json(line)
        except Exception as exc:
            raise ValueError(f"{path}:{line_number}: invalid eval observation: {exc}") from exc
        if observation.case_id in seen_ids:
            raise ValueError(
                f"{path}:{line_number}: duplicate eval observation id: {observation.case_id}"
            )
        seen_ids.add(observation.case_id)
        observations.append(observation)
    return observations


def score_observations(
    cases: list[EvalCase],
    observations: list[EvalObservation],
) -> EvalReport:
    case_by_id = {case.id: case for case in cases}
    observation_by_id = {observation.case_id: observation for observation in observations}
    missing = sorted(set(case_by_id) - set(observation_by_id))
    unknown = sorted(set(observation_by_id) - set(case_by_id))
    if missing or unknown:
        raise ValueError(f"observation ids mismatch: missing={missing}, unknown={unknown}")

    route_correct = 0
    outcome_correct = 0
    required_tool_total = 0
    required_tool_found = 0
    actual_tool_total = 0
    extra_tool_total = 0
    extra_tool_case_count = 0
    field_total = 0
    field_correct = 0
    exact_field_eligible = 0
    exact_field_correct = 0
    missing_requirement_total = 0
    missing_requirement_correct = 0
    tool_execution_total = 0
    tool_execution_success = 0
    schema_total = 0
    schema_valid = 0
    operation_total = 0
    operation_usable = 0
    plan_validation_total = 0
    plan_validation_passed = 0
    fact_total = 0
    fact_consistent = 0
    failed_case_ids: list[str] = []
    durations: list[int] = []
    model_calls = 0
    tool_calls = 0
    provider_calls = 0
    category_rows: dict[str, list[tuple[bool, bool, int]]] = defaultdict(list)

    for case in cases:
        observation = observation_by_id[case.id]
        route_match = observation.route == case.expected.route
        outcome_match = observation.outcome == case.expected.outcome
        route_correct += route_match
        outcome_correct += outcome_match

        actual_tools = list(dict.fromkeys(observation.observed_tools))
        actual_tool_set = set(actual_tools)
        required = set(case.expected.required_tools)
        permitted = required | set(case.expected.allowed_tools)
        forbidden = set(case.expected.forbidden_tools)
        required_tool_total += len(required)
        required_tool_found += len(required & actual_tool_set)
        unexpected = actual_tool_set - permitted
        unexpected |= actual_tool_set & forbidden
        actual_tool_total += len(actual_tool_set)
        extra_tool_total += len(unexpected)
        extra_tool_case_count += bool(unexpected)

        if case.expected.fields:
            exact_field_eligible += 1
            case_fields_correct = True
            for key, expected_value in case.expected.fields.items():
                field_total += 1
                matches = _normalize(observation.fields.get(key)) == _normalize(expected_value)
                field_correct += matches
                case_fields_correct = case_fields_correct and matches
            exact_field_correct += case_fields_correct
        else:
            case_fields_correct = True

        missing_fields_match = True
        if case.expected.outcome == "clarify":
            missing_requirement_total += 1
            missing_fields_match = set(observation.missing_fields) == set(
                case.expected.missing_fields
            )
            missing_requirement_correct += missing_fields_match

        for execution in observation.tool_executions:
            tool_execution_total += 1
            tool_execution_success += execution.success
            if execution.schema_valid is not None:
                schema_total += 1
                schema_valid += execution.schema_valid

        for status in observation.operation_statuses.values():
            operation_total += 1
            operation_usable += status == "usable"

        if case.expected.outcome == "plan":
            plan_validation_total += 1
            plan_validation_passed += observation.validation_passed is True
            if observation.fact_reference_consistent is not None:
                fact_total += 1
                fact_consistent += observation.fact_reference_consistent

        required_tools_match = required <= actual_tool_set
        plan_validation_match = (
            case.expected.outcome != "plan" or observation.validation_passed is True
        )
        if (
            not route_match
            or not outcome_match
            or unexpected
            or not required_tools_match
            or not case_fields_correct
            or not missing_fields_match
            or not plan_validation_match
        ):
            failed_case_ids.append(case.id)
        durations.append(observation.duration_ms)
        model_calls += observation.model_call_count
        tool_calls += observation.tool_call_count
        provider_calls += observation.provider_call_count
        category_rows[case.category].append((route_match, outcome_match, observation.duration_ms))

    count = len(cases)
    metrics = EvalMetrics(
        case_count=count,
        route_accuracy=_ratio(route_correct, count) or 0.0,
        outcome_accuracy=_ratio(outcome_correct, count) or 0.0,
        necessary_tool_recall=_ratio(required_tool_found, required_tool_total),
        extra_tool_call_rate=_ratio(extra_tool_total, actual_tool_total),
        cases_with_extra_tool_calls_rate=_ratio(extra_tool_case_count, count) or 0.0,
        parameter_field_accuracy=_ratio(field_correct, field_total),
        exact_field_case_accuracy=_ratio(exact_field_correct, exact_field_eligible),
        missing_requirement_accuracy=_ratio(
            missing_requirement_correct,
            missing_requirement_total,
        ),
        tool_execution_success_rate=_ratio(tool_execution_success, tool_execution_total),
        schema_valid_rate=_ratio(schema_valid, schema_total),
        result_usable_rate=_ratio(operation_usable, operation_total),
        final_plan_first_or_revised_validation_pass_rate=_ratio(
            plan_validation_passed,
            plan_validation_total,
        ),
        fact_reference_consistency_rate=_ratio(fact_consistent, fact_total),
        end_to_end_p50_ms=_percentile(durations, 50),
        end_to_end_p95_ms=_percentile(durations, 95),
        average_model_call_count=round(model_calls / count, 3),
        average_tool_call_count=round(tool_calls / count, 3),
        average_provider_call_count=round(provider_calls / count, 3),
    )
    by_category = {
        category: CategoryMetrics(
            case_count=len(rows),
            route_accuracy=round(sum(row[0] for row in rows) / len(rows), 4),
            outcome_accuracy=round(sum(row[1] for row in rows) / len(rows), 4),
            average_duration_ms=round(sum(row[2] for row in rows) / len(rows), 1),
        )
        for category, rows in sorted(category_rows.items())
    }
    return EvalReport(
        generated_at=datetime.now().astimezone(),
        metrics=metrics,
        by_category=by_category,
        failed_case_ids=failed_case_ids,
        notes=[
            "Schema 合法率衡量工具包装结果是否通过现有结构校验。",
            "结果可用率按结构化 operation_statuses 统计，empty 不计为 usable。",
            "事实引用一致率当前仅覆盖攻略确定性校验可证明的结构化事实。",
            "60 条单次运行的 p95 仅用于个人项目回归，不作为生产 SLA。",
        ],
    )


def write_report(report: EvalReport, output_directory: Path) -> tuple[Path, Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    json_path = output_directory / "eval_report.json"
    markdown_path = output_directory / "eval_report.md"
    json_path.write_text(
        report.model_dump_json(indent=2),
        encoding="utf-8",
    )
    markdown_path.write_text(_report_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def _report_markdown(report: EvalReport) -> str:
    metrics = report.metrics
    rows = [
        ("路由准确率", _format_rate(metrics.route_accuracy)),
        ("结果类型准确率", _format_rate(metrics.outcome_accuracy)),
        ("必要工具召回率", _format_rate(metrics.necessary_tool_recall)),
        ("多余工具调用率", _format_rate(metrics.extra_tool_call_rate)),
        ("参数字段准确率", _format_rate(metrics.parameter_field_accuracy)),
        ("整例字段完全正确率", _format_rate(metrics.exact_field_case_accuracy)),
        ("缺失项识别准确率", _format_rate(metrics.missing_requirement_accuracy)),
        ("工具执行成功率", _format_rate(metrics.tool_execution_success_rate)),
        ("Schema 合法率", _format_rate(metrics.schema_valid_rate)),
        ("结果可用率", _format_rate(metrics.result_usable_rate)),
        (
            "最终规划校验通过率",
            _format_rate(metrics.final_plan_first_or_revised_validation_pass_rate),
        ),
        ("事实引用一致率", _format_rate(metrics.fact_reference_consistency_rate)),
        ("端到端 p50", f"{metrics.end_to_end_p50_ms} ms"),
        ("端到端 p95", f"{metrics.end_to_end_p95_ms} ms"),
        ("平均模型调用次数", f"{metrics.average_model_call_count:.3f}"),
        ("平均工具调用次数", f"{metrics.average_tool_call_count:.3f}"),
        ("平均供应商请求次数", f"{metrics.average_provider_call_count:.3f}"),
    ]
    lines = [
        "# Trip Planner Eval 报告",
        "",
        f"- 生成时间：{report.generated_at.isoformat()}",
        f"- 案例数：{metrics.case_count}",
        "",
        "## 核心指标",
        "",
        "| 指标 | 结果 |",
        "|---|---:|",
        *(f"| {name} | {value} |" for name, value in rows),
        "",
        "## 分类结果",
        "",
        "| 类别 | 数量 | 路由准确率 | 结果类型准确率 | 平均耗时 |",
        "|---|---:|---:|---:|---:|",
    ]
    for category, item in report.by_category.items():
        lines.append(
            f"| {category} | {item.case_count} | {_format_rate(item.route_accuracy)} | "
            f"{_format_rate(item.outcome_accuracy)} | {item.average_duration_ms:.1f} ms |"
        )
    lines.extend(
        [
            "",
            "## 未完全通过的案例",
            "",
            ", ".join(report.failed_case_ids) if report.failed_case_ids else "无",
            "",
            "## 口径说明",
            "",
            *(f"- {note}" for note in report.notes),
            "",
        ]
    )
    return "\n".join(lines)


def _normalize(value: Any) -> Any:
    if isinstance(value, str):
        normalized = value.strip()
        if normalized.endswith(("市", "地区")) and len(normalized) > 2:
            normalized = normalized.removesuffix("市").removesuffix("地区")
        if re.fullmatch(r"-?\d+", normalized):
            return int(normalized)
        if re.fullmatch(r"-?(?:\d+\.\d*|\d*\.\d+)", normalized):
            return float(normalized)
        return normalized
    if isinstance(value, list):
        return sorted(_normalize(item) for item in value)
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in sorted(value.items())}
    return value


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _percentile(values: list[int], percentile: int) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile / 100 * len(ordered)) - 1)
    return ordered[index]


def _format_rate(value: float | None) -> str:
    return "N/A" if value is None else f"{value * 100:.2f}%"


def write_observations(observations: list[EvalObservation], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(
        json.dumps(observation.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
        for observation in observations
    )
    path.write_text(f"{content}\n", encoding="utf-8")


__all__ = [
    "EvalCase",
    "EvalObservation",
    "EvalReport",
    "ToolExecution",
    "load_eval_cases",
    "load_observations",
    "score_observations",
    "write_observations",
    "write_report",
]
