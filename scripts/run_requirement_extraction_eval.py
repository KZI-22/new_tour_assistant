from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_ROOT = PROJECT_ROOT / "apps" / "api"
sys.path.insert(0, str(API_ROOT))

from app.core.model_registry import ModelRegistry  # noqa: E402
from app.core.settings import get_settings  # noqa: E402
from app.evals.manual_requirement_extraction import (  # noqa: E402
    append_extraction_error,
    append_extraction_report,
)
from app.schemas.chat import ChatMessage  # noqa: E402
from app.services.rule_first_trip_requirement_extractor import (  # noqa: E402
    RuleFirstTripRequirementExtractor,
)

DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "evals"
    / "results"
    / "requirement-extraction-manual.md"
)
EXIT_COMMANDS = {"q", "quit", "exit", "退出"}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interactively test trip requirement extraction and append results to Markdown."
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model id used only when rule extraction detects an ambiguity.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Markdown report path. Defaults to {DEFAULT_OUTPUT}.",
    )
    return parser.parse_args()


async def _run() -> int:
    args = _arguments()
    settings = get_settings()
    registry = ModelRegistry(settings.model_config_path)
    model_info = registry.list_models()
    model_id = args.model or model_info.default_model
    if model_id is None:
        raise RuntimeError(
            "No model was selected and config/models.yaml has no default_model."
        )

    model = registry.create_model(model_id)
    extractor = RuleFirstTripRequirementExtractor(
        model,
        timeout_seconds=settings.trip_planner_request_extraction_timeout_seconds,
    )
    output_path = args.output

    print("手动需求提取测试已启动。")
    print("每次输入是一条独立需求；输入 q 结束。")
    print(f"模型：{model_id}")
    print(f"Markdown：{output_path.resolve()}")

    while True:
        try:
            raw_input = input("\n请输入旅行需求 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not raw_input:
            continue
        if raw_input.casefold() in EXIT_COMMANDS:
            break

        messages = [ChatMessage(role="user", content=raw_input)]
        try:
            result = await extractor.extract(messages)
        except Exception as exc:
            append_extraction_error(
                output_path,
                raw_input=raw_input,
                model_id=model_id,
                error=exc,
            )
            print(
                f"提取失败：{type(exc).__name__}。错误已追加到 {output_path.resolve()}"
            )
            continue

        append_extraction_report(
            output_path,
            raw_input=raw_input,
            result=result,
            model_id=model_id,
        )
        request = result.request
        print(
            "提取完成："
            f"destination={request.core.destination_city or '-'} "
            f"days={request.core.duration_days or '-'} "
            f"start_date={request.core.start_date or '-'} "
            f"path={result.metrics.path}"
        )
        print(f"已追加到：{output_path.resolve()}")

    print(f"测试结束。报告保存在：{output_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
