from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, Field


class ToolDefinition(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]


class ToolCall(BaseModel):
    id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class LLMUsage(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None


class LLMRequest(BaseModel):
    system: str
    messages: list[dict[str, Any]]
    tools: list[ToolDefinition]
    model: str
    max_tokens: int = 2048
    temperature: float = 0.0
    prompt_cache_key: str | None = None
    safety_identifier: str | None = None
    thinking: str | None = None


class LLMResponse(BaseModel):
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: LLMUsage | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class LLMClient(Protocol):
    async def complete(self, request: LLMRequest) -> LLMResponse: ...


@dataclass(frozen=True)
class ProviderModels:
    fast: str
    primary: str
    strong: str
    vision: str


class FakeLLMClient:
    """Small deterministic client for tests and dry-run demos."""

    def __init__(self, script: list[ToolCall] | None = None) -> None:
        self.script = script or []
        self.index = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if self.index < len(self.script):
            call = self.script[self.index]
            self.index += 1
            return LLMResponse(content=f"calling {call.name}", tool_calls=[call])
        if self.index == 0:
            self.index += 1
            return LLMResponse(
                content="I need a real Anthropic or OpenAI model to operate autonomously.",
                tool_calls=[
                    ToolCall(
                        id="fake-done",
                        name="done",
                        args={
                            "success": False,
                            "summary": "Fake provider cannot complete arbitrary browser tasks.",
                            "evidence": [],
                            "remaining_risks": ["Configure Anthropic or OpenAI for autonomous runs."],
                        },
                    )
                ],
            )
        return LLMResponse(content="No further fake actions.", tool_calls=[])


def tool_call_to_text(call: ToolCall) -> str:
    return f"{call.name}({json.dumps(call.args, ensure_ascii=False, sort_keys=True)})"
