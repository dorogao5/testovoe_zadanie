from __future__ import annotations

from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential

from ai_browser_agent.llm.base import LLMRequest, LLMResponse, LLMUsage, ToolCall


class AnthropicToolClient:
    def __init__(self, *, api_key: str | None = None) -> None:
        from anthropic import AsyncAnthropic

        self.client = AsyncAnthropic(api_key=api_key)

    @retry(wait=wait_exponential(multiplier=1, min=1, max=12), stop=stop_after_attempt(3))
    async def complete(self, request: LLMRequest) -> LLMResponse:
        response = await self.client.messages.create(
            model=request.model,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            system=request.system,
            messages=_anthropic_messages(request.messages),
            tools=[
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in request.tools
            ],
        )
        content_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in response.content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                content_parts.append(getattr(block, "text", ""))
            elif block_type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=getattr(block, "id", ""),
                        name=getattr(block, "name", ""),
                        args=dict(getattr(block, "input", {}) or {}),
                    )
                )
        usage = None
        if getattr(response, "usage", None) is not None:
            input_tokens = getattr(response.usage, "input_tokens", None)
            output_tokens = getattr(response.usage, "output_tokens", None)
            usage = LLMUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=(input_tokens or 0) + (output_tokens or 0),
            )
        return LLMResponse(
            content="\n".join(part for part in content_parts if part),
            tool_calls=tool_calls,
            usage=usage,
            raw={"id": getattr(response, "id", None), "stop_reason": getattr(response, "stop_reason", None)},
        )


def _anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role", "user")
        if role == "system":
            continue
        if role not in {"user", "assistant"}:
            role = "user"
        content = message.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        converted.append({"role": role, "content": content})
    return converted

