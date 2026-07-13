from __future__ import annotations

from typing import Any

from app.schemas.travel import FlyAIResult


def result_payload(result: FlyAIResult) -> dict[str, Any]:
    """Return a JSON-safe payload that graph state and tool messages can persist."""

    return result.model_dump(mode="json")
