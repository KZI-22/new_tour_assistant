from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from app.services.rule_first_trip_requirement_extractor import RuleFirstExtractionResult

_REPORT_HEADER = """# 手动需求提取记录

本文件由 `scripts/run_requirement_extraction_eval.py` 追加生成。
每条记录都是一次相互独立的用户需求，不会继承上一条记录的对话上下文。

"""


def append_extraction_report(
    output_path: Path,
    *,
    raw_input: str,
    result: RuleFirstExtractionResult,
    model_id: str,
    generated_at: datetime | None = None,
) -> Path:
    timestamp = generated_at or datetime.now().astimezone()
    request_payload = result.request.model_dump(mode="json")
    diagnostic_payload = {
        "path": result.metrics.path,
        "field_sources": result.field_sources,
        "explicit_missing": result.explicit_missing,
        "ambiguities": [item.model_dump(mode="json") for item in result.ambiguities],
        "metrics": result.metrics.model_dump(mode="json"),
    }
    section = _record_section(
        timestamp=timestamp,
        model_id=model_id,
        status="success",
        raw_input=raw_input,
        request_payload=request_payload,
        diagnostic_payload=diagnostic_payload,
    )
    return _append_section(output_path, section)


def append_extraction_error(
    output_path: Path,
    *,
    raw_input: str,
    model_id: str,
    error: Exception,
    generated_at: datetime | None = None,
) -> Path:
    timestamp = generated_at or datetime.now().astimezone()
    section = _record_section(
        timestamp=timestamp,
        model_id=model_id,
        status="failed",
        raw_input=raw_input,
        request_payload=None,
        diagnostic_payload={
            "error_type": type(error).__name__,
            "error_message": str(error),
        },
    )
    return _append_section(output_path, section)


def _record_section(
    *,
    timestamp: datetime,
    model_id: str,
    status: str,
    raw_input: str,
    request_payload: dict[str, object] | None,
    diagnostic_payload: dict[str, object],
) -> str:
    request_block = (
        _json_block(request_payload)
        if request_payload is not None
        else "未生成需求提取字段。\n"
    )
    return "\n".join(
        [
            f"## {timestamp.isoformat()}",
            "",
            f"- 状态：`{status}`",
            f"- 模型：`{model_id}`",
            "",
            "### 原始输入",
            "",
            _json_block({"role": "user", "content": raw_input}).rstrip(),
            "",
            "### 需求提取字段",
            "",
            request_block.rstrip(),
            "",
            "### 提取诊断",
            "",
            _json_block(diagnostic_payload).rstrip(),
            "",
            "---",
            "",
        ]
    )


def _json_block(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, indent=2)
    return f"~~~json\n{payload}\n~~~\n"


def _append_section(output_path: Path, section: str) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not output_path.exists() or output_path.stat().st_size == 0
    with output_path.open("a", encoding="utf-8", newline="\n") as report:
        if needs_header:
            report.write(_REPORT_HEADER)
        report.write(section)
    return output_path


__all__ = [
    "append_extraction_error",
    "append_extraction_report",
]
