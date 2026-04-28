from __future__ import annotations

import json
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential

from ai_browser_agent.llm.base import LLMRequest, LLMResponse, LLMUsage, ToolCall


class OpenAIResponsesToolClient:
    def __init__(self, *, api_key: str | None = None) -> None:
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(api_key=api_key)

    @retry(wait=wait_exponential(multiplier=1, min=1, max=12), stop=stop_after_attempt(3))
    async def complete(self, request: LLMRequest) -> LLMResponse:
        tools = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            }
            for tool in request.tools
        ]
        response = await self.client.responses.create(
            model=request.model,
            instructions=request.system,
            input=_openai_input(request.messages),
            tools=tools,
            temperature=request.temperature,
            max_output_tokens=request.max_tokens,
        )
        content_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for item in getattr(response, "output", []) or []:
            item_type = getattr(item, "type", None)
            if item_type == "message":
                for block in getattr(item, "content", []) or []:
                    if getattr(block, "type", None) in {"output_text", "text"}:
                        content_parts.append(getattr(block, "text", ""))
            elif item_type == "function_call":
                args_raw = getattr(item, "arguments", "{}") or "{}"
                try:
                    args = json.loads(args_raw)
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(
                    ToolCall(
                        id=getattr(item, "call_id", getattr(item, "id", "")),
                        name=getattr(item, "name", ""),
                        args=args,
                    )
                )

        usage = None
        raw_usage = getattr(response, "usage", None)
        if raw_usage is not None:
            input_tokens = getattr(raw_usage, "input_tokens", None)
            output_tokens = getattr(raw_usage, "output_tokens", None)
            usage = LLMUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=getattr(raw_usage, "total_tokens", None),
            )
        return LLMResponse(
            content="\n".join(part for part in content_parts if part),
            tool_calls=tool_calls,
            usage=usage,
            raw={"id": getattr(response, "id", None)},
        )


def _openai_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role", "user")
        if role == "system":
            continue
        if role not in {"user", "assistant"}:
            role = "user"
        converted.append({"role": role, "content": str(message.get("content", ""))})
    return converted

