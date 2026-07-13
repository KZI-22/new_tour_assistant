from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.core.model_registry import ModelRegistry
from app.schemas.chat import ChatMessage

DEFAULT_SYSTEM_PROMPT = """You are a helpful travel assistant. Answer clearly and honestly.
When current travel facts such as prices, schedules, availability, weather, or opening hours
have not been retrieved from a tool, explicitly say that they need verification. Never invent
bookings or claim that an external action has been completed."""


def _to_langchain_messages(messages: list[ChatMessage]) -> list[Any]:
    converted: list[Any] = []
    if not messages or messages[0].role != "system":
        converted.append(SystemMessage(content=DEFAULT_SYSTEM_PROMPT))

    message_types = {
        "system": SystemMessage,
        "user": HumanMessage,
        "assistant": AIMessage,
    }
    converted.extend(message_types[item.role](content=item.content) for item in messages)
    return converted


def _chunk_text(chunk: Any) -> str:
    text = getattr(chunk, "text", None)
    if isinstance(text, str) and text:
        return text

    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


class ChatService:
    def __init__(self, registry: ModelRegistry) -> None:
        self._registry = registry

    async def stream(self, model_id: str, messages: list[ChatMessage]) -> AsyncIterator[str]:
        model = self._registry.create_model(model_id)
        langchain_messages = _to_langchain_messages(messages)
        async for chunk in model.astream(langchain_messages):
            if text := _chunk_text(chunk):
                yield text
