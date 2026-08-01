"""Interactively run FlyAI ``ai-search`` and append the response to Markdown."""

# The project source path must be added before importing the local app package.
# ruff: noqa: I001

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_SOURCE_ROOT = PROJECT_ROOT / "apps" / "api"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "fliai.md"
DEFAULT_TIMEOUT_SECONDS = 130.0

if str(API_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(API_SOURCE_ROOT))

from app.clients.flyai_client import FlyAIClient  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run FlyAI ai-search and append the result to a Markdown file.",
    )
    parser.add_argument(
        "-q",
        "--query",
        help="Query to send. When omitted, the script prompts for it interactively.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help=f"Markdown output path (default: {DEFAULT_OUTPUT_PATH}).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Timeout for one FlyAI request in seconds (default: {DEFAULT_TIMEOUT_SECONDS:g}).",
    )
    return parser.parse_args()


def query_from_user(provided_query: str | None) -> str:
    query = provided_query if provided_query is not None else input("请输入 FlyAI 查询：")
    query = query.strip()
    if not query:
        raise ValueError("查询不能为空。")
    return query


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def append_result(output_path: Path, query: str, payload: dict[str, Any]) -> None:
    """Append one self-contained record without rewriting prior user results."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not output_path.exists() or output_path.stat().st_size == 0:
        heading = "# FlyAI `ai-search` 查询记录\n"
    else:
        heading = ""

    timestamp = datetime.now(UTC).astimezone().isoformat(timespec="seconds")
    record = (
        f"{heading}\n"
        f"## {timestamp}\n\n"
        "**查询**\n\n"
        f"````text\n{query}\n````\n\n"
        "**FlyAI 返回**\n\n"
        f"````json\n{json_text(payload)}\n````\n"
    )
    with output_path.open("a", encoding="utf-8", newline="\n") as output_file:
        output_file.write(record)


async def run_query(query: str, timeout_seconds: float) -> dict[str, Any]:
    cli_path = os.getenv("FLYAI_CLI_PATH") or None
    client = FlyAIClient(
        cli_path=cli_path,
        default_timeout_seconds=timeout_seconds,
    )
    return (await client.ai_search(query, timeout_seconds=timeout_seconds)).model_dump(mode="json")


def print_result(payload: dict[str, Any]) -> None:
    if payload["success"]:
        print(json_text(payload.get("data")))
        return

    print(f"FlyAI 查询失败：{payload.get('error_message')}", file=sys.stderr)


async def async_main() -> int:
    args = parse_args()
    if args.timeout <= 0:
        raise ValueError("--timeout 必须大于 0。")

    load_dotenv(PROJECT_ROOT / ".env")
    query = query_from_user(args.query)
    payload = await run_query(query, args.timeout)
    append_result(args.output.resolve(), query, payload)
    print_result(payload)
    print(f"\n完整结果已追加保存至：{args.output.resolve()}")
    return 0 if payload["success"] else 1


def main() -> None:
    try:
        raise SystemExit(asyncio.run(async_main()))
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
