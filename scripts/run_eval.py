from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_ROOT = PROJECT_ROOT / "apps" / "api"
sys.path.insert(0, str(API_ROOT))

from app.core.settings import get_settings  # noqa: E402
from app.evals.live_trip_eval import LiveTripEvalRunner  # noqa: E402
from app.evals.trip_eval import (  # noqa: E402
    EvalObservation,
    load_eval_cases,
    load_observations,
    score_observations,
    write_observations,
    write_report,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the local 60-case travel assistant eval.")
    parser.add_argument(
        "--cases",
        type=Path,
        default=PROJECT_ROOT / "evals" / "eval_case.jsonl",
    )
    parser.add_argument("--model", default=None)
    parser.add_argument("--output-directory", type=Path, default=None)
    parser.add_argument("--category", action="append", default=[])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--start-index",
        type=int,
        default=1,
        help="1-based index in the case file at which live execution starts.",
    )
    parser.add_argument(
        "--resume-from",
        type=Path,
        default=None,
        help="Prior eval_run.jsonl whose observations will be retained unless rerun.",
    )
    parser.add_argument(
        "--resume-model",
        default=None,
        help="Model label for observations retained from --resume-from.",
    )
    parser.add_argument("--validate-only", action="store_true")
    return parser.parse_args()


async def _run() -> int:
    args = _arguments()
    if args.start_index < 1:
        raise ValueError("--start-index must be at least 1")
    all_cases = load_eval_cases(
        args.cases,
        expected_count=None if args.category or args.limit else 60,
    )
    report_cases = all_cases
    if args.category:
        selected = set(args.category)
        report_cases = [case for case in all_cases if case.category in selected]
    selected_cases = [
        case
        for index, case in enumerate(all_cases, start=1)
        if index >= args.start_index and case in report_cases
    ]
    if args.limit is not None:
        selected_cases = selected_cases[: args.limit]
    print(
        f"Validated {len(all_cases)} eval cases; selected {len(selected_cases)} "
        f"from index {args.start_index}",
        flush=True,
    )
    if args.validate_only:
        return 0

    settings = get_settings()
    model_id = args.model
    if model_id is None:
        model_id = settings and _configured_model_id(settings.model_config_path, "default_model")
    if not model_id:
        raise RuntimeError("No eval model was selected and config/models.yaml has no default_model")
    router_model_id = _configured_model_id(settings.model_config_path, "router_model")

    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    output_directory = args.output_directory or PROJECT_ROOT / "evals" / "results" / stamp
    observations_path = output_directory / "eval_run.jsonl"
    observation_by_id: dict[str, EvalObservation] = {}
    resumed_case_ids: list[str] = []
    if args.resume_from is not None:
        resumed = load_observations(args.resume_from)
        allowed_ids = {case.id for case in report_cases}
        observation_by_id = {
            observation.case_id: observation
            for observation in resumed
            if observation.case_id in allowed_ids
        }
        resumed_case_ids = list(observation_by_id)
        print(
            f"Loaded {len(observation_by_id)} prior observations from {args.resume_from}",
            flush=True,
        )

    case_positions = {case.id: index for index, case in enumerate(all_cases, start=1)}
    async with LiveTripEvalRunner(settings, model_id=model_id) as runner:
        for case in selected_cases:
            index = case_positions[case.id]
            print(
                f"[{index:02d}/{len(all_cases):02d}] {case.id} ({case.category})",
                flush=True,
            )
            observation = await runner.run(case)
            observation_by_id[case.id] = observation
            current_observations = [
                observation_by_id[item.id]
                for item in report_cases
                if item.id in observation_by_id
            ]
            write_observations(current_observations, observations_path)
            status = "ok" if observation.error is None else observation.error
            print(
                f"  route={observation.route} outcome={observation.outcome} "
                f"duration_ms={observation.duration_ms} status={status}",
                flush=True,
            )

    observations = [
        observation_by_id[case.id]
        for case in report_cases
        if case.id in observation_by_id
    ]
    write_observations(observations, observations_path)
    rerun_case_ids = [case.id for case in selected_cases]
    report = score_observations(report_cases, observations)
    retained_case_count = len(
        [case_id for case_id in resumed_case_ids if case_id not in rerun_case_ids]
    )
    if args.resume_from is not None:
        report.notes.append(
            "主模型来源："
            f"{retained_case_count} 条沿用 {args.resume_model or 'unknown'}，"
            f"{len(rerun_case_ids)} 条由 {model_id} 重新执行。"
        )
    else:
        report.notes.append(f"主模型：{model_id}。")
    report.notes.append(f"路由模型：{router_model_id or 'unknown'}（独立于主模型配置）。")
    json_path, markdown_path = write_report(report, output_directory)
    manifest = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "case_file": str(args.cases.resolve()),
        "observation_file": str(observations_path.resolve()),
        "router_model": router_model_id,
        "runs": [
            {
                "model": args.resume_model or "unknown",
                "case_ids": [
                    case_id for case_id in resumed_case_ids if case_id not in rerun_case_ids
                ],
                "source": str(args.resume_from.resolve()) if args.resume_from else None,
            },
            {
                "model": model_id,
                "case_ids": rerun_case_ids,
                "source": "live",
            },
        ],
    }
    manifest_path = output_directory / "eval_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Observations: {observations_path}", flush=True)
    print(f"JSON report: {json_path}", flush=True)
    print(f"Markdown report: {markdown_path}", flush=True)
    print(f"Run manifest: {manifest_path}", flush=True)
    print(
        f"route_accuracy={report.metrics.route_accuracy:.4f} "
        f"field_accuracy={report.metrics.parameter_field_accuracy} "
        f"p95_ms={report.metrics.end_to_end_p95_ms}",
        flush=True,
    )
    return 0


def _configured_model_id(path: Path, key: str) -> str | None:
    import yaml

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    value = raw.get(key)
    return str(value) if value else None


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
