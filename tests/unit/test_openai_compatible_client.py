from pathlib import Path

from ai_browser_agent.llm.base import LLMRequest
from ai_browser_agent.llm.openai_compatible_client import (
    _chat_messages,
    _extra_body,
    _is_retryable_provider_error,
)


def test_chat_messages_include_image_data_url(tmp_path: Path) -> None:
    image = tmp_path / "shot.png"
    image.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    )
    request = LLMRequest(
        system="system",
        messages=[{"role": "user", "content": "inspect", "images": [str(image)]}],
        tools=[],
        model="kimi-k2.6",
    )

    messages = _chat_messages(request)

    assert messages[1]["content"][1]["type"] == "image_url"
    assert messages[1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_insufficient_quota_errors_are_not_retried() -> None:
    exc = RuntimeError("Error code: 429 - insufficient_quota current quota billing")

    assert not _is_retryable_provider_error(exc)


def test_generic_provider_errors_are_retryable() -> None:
    exc = RuntimeError("Error code: 500 - temporary upstream failure")

    assert _is_retryable_provider_error(exc)


def test_kimi_extra_body_includes_cache_and_thinking() -> None:
    request = LLMRequest(
        system="system",
        messages=[{"role": "user", "content": "next"}],
        tools=[],
        model="kimi-k2.6",
        prompt_cache_key="session-123",
        safety_identifier="profile:abc",
        thinking="disabled",
    )

    body = _extra_body(request, provider="kimi")

    assert body == {
        "prompt_cache_key": "session-123",
        "safety_identifier": "profile:abc",
        "thinking": {"type": "disabled"},
    }
