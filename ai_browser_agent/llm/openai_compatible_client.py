from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from ai_browser_agent.llm.base import LLMRequest, LLMResponse, LLMUsage, ToolCall


def _is_retryable_provider_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(marker in text for marker in ("insufficient_quota", "billing", "current quota")):
        return False
    return True


class OpenAICompatibleChatClient:
    """OpenAI-compatible Chat Completions client.

    Kimi exposes K2.6 through an OpenAI-compatible `/chat/completions`
    endpoint. This adapter intentionally uses Chat Completions because Kimi's
    tool calling and multimodal examples are documented on that surface.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str,
        provider: str = "openai-compatible",
    ) -> None:
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.provider = provider

    @retry(
        wait=wait_exponential(multiplier=1, min=1, max=12),
        stop=stop_after_attempt(3),
        retry=retry_if_exception(_is_retryable_provider_error),
        reraise=True,
    )
    async def complete(self, request: LLMRequest) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": _chat_messages(request),
            "max_tokens": request.max_tokens,
        }
        if request.tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in request.tools
            ]
            kwargs["tool_choice"] = "auto"
        extra_body = _extra_body(request, provider=self.provider)
        if extra_body:
            kwargs["extra_body"] = extra_body
        if self.provider != "kimi":
            kwargs["temperature"] = request.temperature

        response = await self.client.chat.completions.create(**kwargs)
        message = response.choices[0].message if response.choices else None
        content = ""
        tool_calls: list[ToolCall] = []
        if message is not None:
            raw_content = getattr(message, "content", "") or ""
            content = raw_content if isinstance(raw_content, str) else str(raw_content)
            for call in getattr(message, "tool_calls", []) or []:
                function = getattr(call, "function", None)
                if function is None:
                    continue
                try:
                    args = json.loads(getattr(function, "arguments", "{}") or "{}")
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(
                    ToolCall(
                        id=getattr(call, "id", ""),
                        name=getattr(function, "name", ""),
                        args=args,
                    )
                )

        usage = None
        if getattr(response, "usage", None) is not None:
            prompt_tokens = getattr(response.usage, "prompt_tokens", None)
            completion_tokens = getattr(response.usage, "completion_tokens", None)
            usage = LLMUsage(
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
                total_tokens=getattr(response.usage, "total_tokens", None),
                cached_input_tokens=_cached_prompt_tokens(response.usage),
            )
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            raw={
                "id": getattr(response, "id", None),
                "model": getattr(response, "model", None),
                "finish_reason": getattr(response.choices[0], "finish_reason", None)
                if response.choices
                else None,
            },
        )


def _chat_messages(request: LLMRequest) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": request.system}]
    for message in request.messages:
        role = message.get("role", "user")
        if role not in {"user", "assistant", "system"}:
            role = "user"
        if role == "system":
            messages[0]["content"] += "\n\n" + str(message.get("content", ""))
            continue
        images = message.get("images") or []
        if images:
            content: list[dict[str, Any]] = [{"type": "text", "text": str(message.get("content", ""))}]
            for image_path in images:
                data_url = _image_data_url(Path(str(image_path)))
                if data_url:
                    content.append({"type": "image_url", "image_url": {"url": data_url}})
            messages.append({"role": role, "content": content})
        else:
            messages.append({"role": role, "content": str(message.get("content", ""))})
    return messages


def _image_data_url(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(path.suffix.lower())
    if mime is None:
        return None
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _extra_body(request: LLMRequest, *, provider: str) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if request.prompt_cache_key:
        body["prompt_cache_key"] = request.prompt_cache_key
    if request.safety_identifier:
        body["safety_identifier"] = request.safety_identifier
    if provider == "kimi" and request.thinking:
        thinking = request.thinking.strip().lower()
        if thinking in {"enabled", "disabled"}:
            body["thinking"] = {"type": thinking}
    return body


def _cached_prompt_tokens(usage: Any) -> int | None:
    details = getattr(usage, "prompt_tokens_details", None)
    if details is None and hasattr(usage, "model_dump"):
        details = usage.model_dump(mode="json").get("prompt_tokens_details")
    if isinstance(details, dict):
        value = details.get("cached_tokens") or details.get("cached_input_tokens")
        return int(value) if isinstance(value, int) else None
    value = getattr(details, "cached_tokens", None) if details is not None else None
    return int(value) if isinstance(value, int) else None
