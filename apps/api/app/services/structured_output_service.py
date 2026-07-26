from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable, Mapping
from typing import Literal, TypeVar, cast

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

logger = logging.getLogger(__name__)
SchemaT = TypeVar("SchemaT", bound=BaseModel)


class StructuredOutputError(RuntimeError):
    pass


class StructuredOutputService:
    def __init__(self, model: BaseChatModel) -> None:
        self._model = model

    async def invoke(
        self,
        schema: type[SchemaT],
        system_prompt: str,
        user_prompt: str,
        *,
        timeout_seconds: float,
        attempt_observer: Callable[[Literal["native", "fallback"]], None] | None = None,
    ) -> SchemaT:
        native_error: Exception | None = None
        try:
            structured_model = self._model.with_structured_output(schema)
            if attempt_observer is not None:
                attempt_observer("native")
            raw = await asyncio.wait_for(
                structured_model.ainvoke(
                    [
                        SystemMessage(content=system_prompt),
                        HumanMessage(content=user_prompt),
                    ]
                ),
                timeout=timeout_seconds,
            )
            return raw if isinstance(raw, schema) else schema.model_validate(raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            native_error = exc
            logger.info(
                "Native structured output unavailable schema=%s exception_type=%s",
                schema.__name__,
                type(exc).__name__,
            )

        fallback_prompt = (
            f"{user_prompt}\n\n请只输出符合以下 JSON Schema 的 JSON 对象，不要输出 Markdown：\n"
            f"{json.dumps(schema.model_json_schema(), ensure_ascii=False)}"
        )
        try:
            if attempt_observer is not None:
                attempt_observer("fallback")
            response = await asyncio.wait_for(
                self._model.ainvoke(
                    [
                        SystemMessage(content=system_prompt),
                        HumanMessage(content=fallback_prompt),
                    ]
                ),
                timeout=timeout_seconds,
            )
            return schema.model_validate_json(_extract_json(_message_text(response)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise StructuredOutputError("model did not return valid structured output") from (
                native_error or exc
            )


def _message_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
                chunks.append(cast(str, item["text"]))
        return "".join(chunks)
    if isinstance(message, AIMessage):
        return str(content)
    return ""


def _extract_json(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, count=1, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped, count=1)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise ValueError("model response does not contain a JSON object")
    return stripped[start : end + 1]


__all__ = [
    "StructuredOutputError",
    "StructuredOutputService",
]
