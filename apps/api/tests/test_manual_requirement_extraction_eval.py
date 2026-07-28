from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.evals.manual_requirement_extraction import (
    append_extraction_error,
    append_extraction_report,
)
from app.schemas.chat import ChatMessage
from app.services.rule_first_trip_requirement_extractor import (
    extract_trip_request_by_rules,
)


def test_markdown_report_appends_raw_input_and_extracted_fields() -> None:
    raw_input = "2027-10-01 出发，帮我规划西安三日游"
    result = extract_trip_request_by_rules(
        [ChatMessage(role="user", content=raw_input)]
    )
    output_path = Path(__file__).parent / f".requirement-extraction-{uuid4().hex}.md"
    generated_at = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

    try:
        append_extraction_report(
            output_path,
            raw_input=raw_input,
            result=result,
            model_id="test-model",
            generated_at=generated_at,
        )
        append_extraction_report(
            output_path,
            raw_input="去杭州玩两天",
            result=extract_trip_request_by_rules(
                [ChatMessage(role="user", content="去杭州玩两天")]
            ),
            model_id="test-model",
            generated_at=generated_at,
        )
        append_extraction_error(
            output_path,
            raw_input="测试失败记录",
            model_id="test-model",
            error=RuntimeError("model unavailable"),
            generated_at=generated_at,
        )

        report = output_path.read_text(encoding="utf-8")
        assert report.count("# 手动需求提取记录") == 1
        assert raw_input in report
        assert '"destination_city": "西安"' in report
        assert '"duration_days": 3' in report
        assert '"path": "rules"' in report
        assert "去杭州玩两天" in report
        assert '"destination_city": "杭州"' in report
        assert "测试失败记录" in report
        assert '"error_type": "RuntimeError"' in report
        assert '"error_message": "model unavailable"' in report
    finally:
        output_path.unlink(missing_ok=True)
