from __future__ import annotations

from pathlib import Path

from app.evals.trip_eval import (
    EvalObservation,
    load_eval_cases,
    load_observations,
    score_observations,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CASE_PATH = PROJECT_ROOT / "evals" / "eval_case.jsonl"


def test_eval_case_file_has_expected_balanced_sixty_cases() -> None:
    cases = load_eval_cases(CASE_PATH, expected_count=60)

    counts: dict[str, int] = {}
    for case in cases:
        counts[case.category] = counts.get(case.category, 0) + 1

    assert counts == {
        "ambiguity": 5,
        "complete_plan": 10,
        "general_chat": 6,
        "missing_requirement": 5,
        "mixed_plan": 8,
        "multi_turn": 6,
        "single_flight": 5,
        "single_hotel": 5,
        "single_train": 5,
        "single_weather": 5,
    }
    assert sum(case.expected.subjective for case in cases) == 20


def test_clarification_cases_never_require_same_turn_tools() -> None:
    cases = load_eval_cases(CASE_PATH, expected_count=60)

    clarification_cases = [case for case in cases if case.expected.outcome == "clarify"]

    assert clarification_cases
    assert all(not case.expected.required_tools for case in clarification_cases)


def test_score_observations_uses_required_tools_fields_and_validation() -> None:
    case = load_eval_cases(CASE_PATH, expected_count=60)[26]
    observation = EvalObservation(
        case_id=case.id,
        category=case.category,
        route="trip_planner",
        outcome="plan",
        fields={
            "core.destination_city": "成都市",
            "core.duration_days": "3",
            "core.start_date": "2026-08-10",
            "transport.enabled": False,
            "hotel.enabled": False,
        },
        observed_tools=["collect_map_weather"],
        operation_statuses={"collect_map_weather": "usable"},
        duration_ms=1_000,
        validation_passed=True,
        fact_reference_consistent=True,
    )

    report = score_observations([case], [observation])

    assert report.metrics.route_accuracy == 1
    assert report.metrics.necessary_tool_recall == 1
    assert report.metrics.parameter_field_accuracy == 1
    assert report.metrics.final_plan_first_or_revised_validation_pass_rate == 1
    assert report.failed_case_ids == []


def test_load_observations_from_jsonl() -> None:
    path = Path(__file__).parent / "fixtures" / "eval_observation.jsonl"

    observations = load_observations(path)

    assert len(observations) == 1
    assert observations[0].case_id == "chat_001"
    assert observations[0].duration_ms == 100
