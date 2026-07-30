from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping
from typing import cast

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, HumanMessage, SystemMessage

from app.schemas.agent_runtime import AgentTaskSnapshot
from app.schemas.chat import ChatMessage
from app.schemas.tool_execution import MessageDeltaEvent
from app.services.agent_executor import AgentExecutionError

ANSWER_SYSTEM_PROMPT = """你是回答 Agent，只负责把主 Agent 提供的结构化业务结果组织成最终中文回复。
按需分区展示行程、天气、交通和酒店。必须如实呈现空结果、部分成功、缺失数据和工具失败。
不得调用工具，不得修改业务结果，不得补造价格、班次、酒店、天气、景点或其他事实。
交通与酒店作为独立选项展示，不反向改写每日行程。"""

DIRECT_SYSTEM_PROMPT = """你是旅游助手的主 Agent。当前请求不需要专业工具，请结合最近对话直接、自然地
回答。不要声称查询过实时数据、完成过预订或执行过任何外部操作。"""


class AnswerAgent:
    def __init__(
        self,
        model_factory: Callable[[str], BaseChatModel],
        *,
        timeout_seconds: float,
    ) -> None:
        self._model_factory = model_factory
        self._timeout_seconds = timeout_seconds

    async def stream(
        self,
        model_id: str,
        current_request: str,
        tasks: list[AgentTaskSnapshot],
    ) -> AsyncIterator[MessageDeltaEvent]:
        payload = {
            "current_request": current_request,
            "business_results": [
                {
                    "agent": task.agent,
                    "status": task.status,
                    "result": task.result.model_dump(mode="json") if task.result else None,
                    "error_code": task.error_code,
                }
                for task in tasks
            ],
        }
        async for event in self._stream_messages(
            model_id,
            ANSWER_SYSTEM_PROMPT,
            json.dumps(payload, ensure_ascii=False),
        ):
            yield event

    async def stream_direct(
        self,
        model_id: str,
        messages: list[ChatMessage],
    ) -> AsyncIterator[MessageDeltaEvent]:
        recent = [
            {"role": item.role, "content": item.content}
            for item in messages[-10:]
            if item.role in {"user", "assistant"}
        ]
        async for event in self._stream_messages(
            model_id,
            DIRECT_SYSTEM_PROMPT,
            json.dumps({"recent_messages": recent}, ensure_ascii=False),
        ):
            yield event

    async def _stream_messages(
        self,
        model_id: str,
        system_prompt: str,
        user_prompt: str,
    ) -> AsyncIterator[MessageDeltaEvent]:
        model = self._model_factory(model_id)
        streamed = False
        iterator = model.astream(
            [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_prompt),
            ]
        )
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async for chunk in iterator:
                    if not isinstance(chunk, AIMessageChunk):
                        raise AgentExecutionError(
                            "MODEL_RESPONSE_INVALID",
                            "回答 Agent 返回了无法处理的响应。",
                        )
                    if text := _message_text(chunk):
                        streamed = True
                        yield MessageDeltaEvent(delta=text)
        except TimeoutError as exc:
            raise AgentExecutionError(
                "ANSWER_AGENT_TIMEOUT",
                "回答 Agent 生成回复超时，请稍后重试。",
            ) from exc
        if not streamed:
            raise AgentExecutionError(
                "MODEL_EMPTY_RESPONSE",
                "回答 Agent 没有返回有效内容，请稍后重试。",
            )


def _message_text(message: AIMessageChunk) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for item in content:
        if isinstance(item, str):
            chunks.append(item)
        elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
            chunks.append(cast(str, item["text"]))
    return "".join(chunks)


__all__ = ["ANSWER_SYSTEM_PROMPT", "AnswerAgent", "DIRECT_SYSTEM_PROMPT"]
