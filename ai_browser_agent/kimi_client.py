"""OpenAI-compatible LLM client with function/tool calling support.

This module provides an async client for OpenAI-compatible APIs
(for example Alibaba Model Studio). It supports chat completions,
tool/function calling, retry logic with exponential backoff, and
token estimation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from typing import Any

from models import ToolCall

logger = logging.getLogger("kimi_client")

# ---------------------------------------------------------------------------
# Optional openai import with graceful fallback for environments where the
# package is not yet installed.
# ---------------------------------------------------------------------------
try:
    from openai import AsyncOpenAI, APIError, RateLimitError, APITimeoutError
except ImportError as _openai_err:  # pragma: no cover
    AsyncOpenAI = None  # type: ignore[misc, assignment]
    APIError = Exception  # type: ignore[misc, assignment]
    RateLimitError = Exception  # type: ignore[misc, assignment]
    APITimeoutError = Exception  # type: ignore[misc, assignment]
    logger.warning("openai package not installed; KimiClient will fail at runtime. "
                   "Install with: pip install openai>=1.0")

# ---------------------------------------------------------------------------
# Optional tiktoken import with fallback approximation.
# ---------------------------------------------------------------------------
try:
    import tiktoken
except ImportError:  # pragma: no cover
    tiktoken = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# httpx is required by AsyncOpenAI custom http client.
# ---------------------------------------------------------------------------
try:
    import httpx
except ImportError as _httpx_err:  # pragma: no cover
    httpx = None  # type: ignore[assignment]
    logger.warning("httpx package not installed; required for API client support.")


class KimiClient:
    """Async client for an OpenAI-compatible API.

    Parameters
    ----------
    api_key:
        Provider API key.
    base_url:
        API endpoint base URL. Defaults to Alibaba Model Studio endpoint.
    model:
        Model identifier to use for completions.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        model: str = "qwen3.6-max-preview",
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        if AsyncOpenAI is None:
            raise RuntimeError(
                "The 'openai' package is required but not installed. "
                "Install it with: pip install openai>=1.0"
            )
        if httpx is None:
            raise RuntimeError(
                "The 'httpx' package is required but not installed. "
                "Install it with: pip install httpx"
            )

        http_client = httpx.AsyncClient()

        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            http_client=http_client,
        )
        self.model = model
        self._base_url = base_url

        # Retry configuration
        self._max_retries = 5
        self._base_delay = 1.0  # seconds
        self._max_delay = 60.0  # seconds
        self._retryable_errors = (
            RateLimitError,
            APITimeoutError,
            APIError,
            asyncio.TimeoutError,
        )

        logger.info("LLM client initialised (model=%s, base_url=%s)", model, base_url)

    # ------------------------------------------------------------------
    # Core chat method
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        """Send a chat completion request to the configured API.

        Supports function/tool calling in standard OpenAI format.  The method
        implements exponential-backoff retries for rate-limit and transient
        errors.

        Parameters
        ----------
        messages:
            List of message dicts with ``role`` and ``content`` keys.
        tools:
            Optional list of tool definitions in OpenAI function-calling
            schema.
        temperature:
            Sampling temperature (0.0 = deterministic, 1.0 = creative).
        max_tokens:
            Maximum number of tokens to generate.

        Returns
        -------
        dict
            Raw JSON-serialisable response dict from the API.
        """
        # Validate / normalise tool definitions
        prepared_tools = self.prepare_tool_definitions(tools) if tools else None

        request_payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if prepared_tools:
            request_payload["tools"] = prepared_tools
            request_payload["tool_choice"] = "auto"

        # Token estimate for logging
        token_estimate = sum(self.count_tokens_approx(str(m)) for m in messages)
        logger.debug(
            "LLM chat request -> tokens≈%d, tools=%s, temp=%.2f, max_tokens=%d",
            token_estimate,
            len(prepared_tools or []),
            temperature,
            max_tokens,
        )

        last_exception: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                response = await self._client.chat.completions.create(**request_payload)
                raw = response.model_dump()
                logger.debug("LLM chat response received (attempt %d)", attempt)
                return raw

            except Exception as exc:  # noqa: BLE001 – we classify below
                last_exception = exc
                is_retryable = isinstance(exc, self._retryable_errors)
                if not is_retryable:
                    # Also treat connection / network errors as retryable
                    exc_name = type(exc).__name__.lower()
                    is_retryable = any(
                        kw in exc_name
                        for kw in ("connection", "network", "timeout", "retry")
                    )

                if not is_retryable or attempt == self._max_retries:
                    logger.error(
                        "LLM chat failed after %d attempt(s): %s",
                        attempt,
                        exc,
                    )
                    raise

                # Exponential backoff with jitter
                delay = min(
                    self._base_delay * (2 ** (attempt - 1)),
                    self._max_delay,
                )
                jitter = random.uniform(0, delay * 0.2)
                sleep_time = delay + jitter
                logger.warning(
                    "LLM chat attempt %d/%d failed (%s). "
                    "Retrying in %.2f s …",
                    attempt,
                    self._max_retries,
                    exc,
                    sleep_time,
                )
                await asyncio.sleep(sleep_time)

        # Should never be reached, but keep the type-checker happy.
        raise last_exception or RuntimeError("Unexpected retry exhaustion")

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    def parse_tool_calls(self, response: dict[str, Any]) -> list[ToolCall]:
        """Extract tool calls from an assistant message in the response.

        Handles both the modern ``tool_calls`` list and the legacy single
        ``function_call`` dict.

        Parameters
        ----------
        response:
            Raw response dict returned by :meth:`chat`.

        Returns
        -------
        list[ToolCall]
            Parsed tool calls, or an empty list if none are present.
        """
        choices = response.get("choices", [])
        if not choices:
            return []

        message = choices[0].get("message", {})
        if not message:
            return []

        results: list[ToolCall] = []

        # Modern tool_calls format
        tool_calls = message.get("tool_calls")
        if tool_calls:
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                tc_id = tc.get("id", "")
                function = tc.get("function", {})
                if not isinstance(function, dict):
                    continue
                name = function.get("name", "")
                arguments_raw = function.get("arguments", "{}")
                # arguments may be a JSON string or already a dict
                if isinstance(arguments_raw, str):
                    try:
                        arguments = json.loads(arguments_raw)
                    except json.JSONDecodeError:
                        arguments = {}
                elif isinstance(arguments_raw, dict):
                    arguments = arguments_raw
                else:
                    arguments = {}
                results.append(ToolCall(id=tc_id, name=name, arguments=arguments))

        # Legacy function_call format
        function_call = message.get("function_call")
        if function_call and isinstance(function_call, dict):
            name = function_call.get("name", "")
            arguments_raw = function_call.get("arguments", "{}")
            if isinstance(arguments_raw, str):
                try:
                    arguments = json.loads(arguments_raw)
                except json.JSONDecodeError:
                    arguments = {}
            elif isinstance(arguments_raw, dict):
                arguments = arguments_raw
            else:
                arguments = {}
            results.append(ToolCall(id="legacy_0", name=name, arguments=arguments))

        return results

    def extract_content(self, response: dict[str, Any]) -> str:
        """Extract text content from the assistant message in the response.

        Parameters
        ----------
        response:
            Raw response dict returned by :meth:`chat`.

        Returns
        -------
        str
            The assistant's text content, or an empty string if absent.
        """
        choices = response.get("choices", [])
        if not choices:
            return ""
        message = choices[0].get("message", {})
        content = message.get("content")
        return content if content is not None else ""

    # ------------------------------------------------------------------
    # Tool definition helpers
    # ------------------------------------------------------------------

    def prepare_tool_definitions(self, tools: list[dict]) -> list[dict]:
        """Validate and format tool definitions for the OpenAI API.

        Ensures every tool follows the exact schema required:

        .. code-block:: json

            {
              "type": "function",
              "function": {
                "name": "...",
                "description": "...",
                "parameters": {
                  "type": "object",
                  "properties": {...},
                  "required": [...]
                }
              }
            }

        Parameters
        ----------
        tools:
            Raw tool definition dicts.

        Returns
        -------
        list[dict]
            Normalised and validated tool definitions.

        Raises
        ------
        ValueError
            If a tool definition is malformed.
        """
        if not isinstance(tools, list):
            raise ValueError(f"tools must be a list, got {type(tools).__name__}")

        validated: list[dict] = []
        for idx, tool in enumerate(tools):
            if not isinstance(tool, dict):
                raise ValueError(f"Tool at index {idx} is not a dict")

            # If already wrapped in OpenAI format, validate it
            if tool.get("type") == "function" and "function" in tool:
                func = tool["function"]
                if not isinstance(func, dict):
                    raise ValueError(f"Tool {idx}: 'function' must be a dict")
                name = func.get("name")
                if not name or not isinstance(name, str):
                    raise ValueError(f"Tool {idx}: missing or invalid 'name'")
                validated.append(tool)
                continue

            # If only the inner function definition is provided, wrap it
            if "name" in tool and "description" in tool:
                params = tool.get("parameters", {})
                if not isinstance(params, dict):
                    params = {"type": "object", "properties": {}}
                if "type" not in params:
                    params["type"] = "object"
                if "properties" not in params:
                    params["properties"] = {}
                wrapped = {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": params,
                    },
                }
                validated.append(wrapped)
                continue

            raise ValueError(
                f"Tool at index {idx} does not match expected schema. "
                "Expected keys: 'type'=='function' with 'function' block, "
                "or flat keys 'name' and 'description'."
            )

        return validated

    # ------------------------------------------------------------------
    # Token counting
    # ------------------------------------------------------------------

    def count_tokens_approx(self, text: str) -> int:
        """Approximate the number of tokens in *text*.

        Uses ``tiktoken`` (cl100k_base) when available; otherwise falls back
        to a simple heuristic.

        Parameters
        ----------
        text:
            Input text to estimate.

        Returns
        -------
        int
            Estimated token count (≥ 1 for non-empty text).
        """
        if not text:
            return 0

        if tiktoken is not None:
            try:
                enc = tiktoken.get_encoding("cl100k_base")
                return len(enc.encode(text))
            except Exception:  # noqa: BLE001 – fallback on any tiktoken error
                pass

        # Fallback: roughly 4 characters per token for CJK, ~4 for Latin.
        # A slightly better heuristic: ~0.3 tokens per character on average.
        return max(1, int(len(text) * 0.3))

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    @property
    def client(self) -> "AsyncOpenAI":
        """Expose the underlying ``AsyncOpenAI`` client for advanced use."""
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.close()

    def __repr__(self) -> str:
        return (
            f"KimiClient(model={self.model!r}, "
            f"base_url={self._base_url!r})"
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def create_default_tools() -> list[dict]:
    """Return the default tool definitions for the browser agent.

    These definitions follow the OpenAI function-calling schema exactly and
    are derived from the ``Tool Definitions`` section of the project SPEC.

    Returns
    -------
    list[dict]
        Tool definitions ready to be passed to :meth:`KimiClient.chat`.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "navigate",
                "description": "Navigate to a URL. Use when you need to visit a new page.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "Full URL to navigate to",
                        }
                    },
                    "required": ["url"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "click",
                "description": (
                    "Click on an element. Describe the element in natural language "
                    "(e.g., 'button with text Submit', 'link with text About Us'). "
                    "The system will find it."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "element_description": {
                            "type": "string",
                            "description": "Natural language description of the element to click",
                        },
                        "reason": {
                            "type": "string",
                            "description": "Why you want to click this element",
                        },
                    },
                    "required": ["element_description", "reason"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "type_text",
                "description": (
                    "Type text into an input field. Describe the field in natural language."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "element_description": {
                            "type": "string",
                            "description": "Natural language description of the input field",
                        },
                        "text": {
                            "type": "string",
                            "description": "Text to type",
                        },
                        "submit_after": {
                            "type": "boolean",
                            "description": "Whether to press Enter after typing",
                            "default": False,
                        },
                    },
                    "required": ["element_description", "text"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "scroll",
                "description": "Scroll the page up or down.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "direction": {
                            "type": "string",
                            "enum": ["up", "down"],
                            "description": "Scroll direction",
                        },
                        "amount": {
                            "type": "integer",
                            "description": "Pixels to scroll",
                            "default": 500,
                        },
                    },
                    "required": ["direction"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "ask_user",
                "description": (
                    "Ask the user a question when you need clarification or "
                    "additional information."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "Question to ask the user",
                        }
                    },
                    "required": ["question"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "done",
                "description": "Mark the task as completed and provide the final result.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "result": {
                            "type": "string",
                            "description": "Summary of what was accomplished",
                        }
                    },
                    "required": ["result"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_information",
                "description": (
                    "Search for specific information on the current page. "
                    "Returns matching text snippets."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "What information to look for",
                        }
                    },
                    "required": ["query"],
                },
            },
        },
    ]


# ---------------------------------------------------------------------------
# Simple sanity-check when run directly
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)

    # Verify create_default_tools produces valid definitions
    client = KimiClient(api_key="dummy-key")
    raw_tools = create_default_tools()
    validated = client.prepare_tool_definitions(raw_tools)
    print(f"Validated {len(validated)} tool definitions")
    for t in validated:
        print(f"  - {t['function']['name']}: {t['function']['description'][:40]}...")

    # Token counting demo
    sample = "The quick brown fox jumps over the lazy dog."
    print(f"Token estimate for sample text: {client.count_tokens_approx(sample)}")

    # Parse tool calls demo
    fake_response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "type": "function",
                            "function": {
                                "name": "navigate",
                                "arguments": '{"url": "https://example.com"}',
                            },
                        }
                    ],
                }
            }
        ]
    }
    calls = client.parse_tool_calls(fake_response)
    print(f"Parsed {len(calls)} tool call(s)")
    for c in calls:
        print(f"  -> {c.name}({c.arguments})")

    # Legacy function_call demo
    legacy_response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "I'll navigate there.",
                    "function_call": {
                        "name": "navigate",
                        "arguments": '{"url": "https://legacy.example.com"}',
                    },
                }
            }
        ]
    }
    legacy_calls = client.parse_tool_calls(legacy_response)
    print(f"Parsed {len(legacy_calls)} legacy call(s)")
    for c in legacy_calls:
        print(f"  -> {c.name}({c.arguments})")

    print("\nAll sanity checks passed.")