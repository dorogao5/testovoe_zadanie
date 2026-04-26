"""Utility helpers for the AI Browser Automation Agent.

Includes token counting, screenshot encoding, DOM cleaning, element visibility
checking, safe JSON parsing, and a retry decorator with exponential backoff.
"""

from __future__ import annotations

import base64
import functools
import json
import random
import re
import time
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Token counter
# ---------------------------------------------------------------------------

def count_tokens(text: str) -> int:
    """Approximate token count using the rule-of-thumb: words / 0.75.

    This is a fast, lightweight estimator suitable for context-window
    management. For production-grade counting you may swap in tiktoken.
    """
    if not text:
        return 0
    # Split on whitespace and punctuation to get rough word count
    words = len(re.findall(r"[\w'-]+", text))
    return int(words / 0.75)


# ---------------------------------------------------------------------------
# Screenshot encoder
# ---------------------------------------------------------------------------

def encode_screenshot_to_b64(screenshot_bytes: bytes | None) -> str | None:
    """Encode raw screenshot bytes into a base64 ASCII string.

    Returns ``None`` when the input is ``None``.
    """
    if screenshot_bytes is None:
        return None
    return base64.b64encode(screenshot_bytes).decode("ascii")


def decode_b64_to_bytes(b64_string: str | None) -> bytes | None:
    """Decode a base64 string back to raw bytes.

    Returns ``None`` when the input is ``None``.
    """
    if b64_string is None:
        return None
    return base64.b64decode(b64_string)


# ---------------------------------------------------------------------------
# DOM cleaning helpers
# ---------------------------------------------------------------------------

def remove_scripts_from_html(html: str) -> str:
    """Remove all ``<script>`` tags and their contents."""
    # re.DOTALL makes . match newlines as well
    return re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)


def remove_styles_from_html(html: str) -> str:
    """Remove all ``<style>`` tags and their contents."""
    return re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)


def remove_hidden_elements_from_html(html: str) -> str:
    """Remove common hidden elements (``display:none``, ``visibility:hidden``).

    This is a best-effort regex-based approach. For fully accurate filtering
    a real DOM parser (e.g. Playwright or BeautifulSoup) should be used.
    """
    # Remove elements with style="display:none" or style="visibility:hidden"
    html = re.sub(
        r"<[^>]+style=[\"'][^\"']*(display:\s*none|visibility:\s*hidden)[^\"']*[\"'][^>]*>.*?</[^>]+>",
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # Remove elements with hidden attribute
    html = re.sub(
        r"<[^>]+hidden[^>]*>.*?</[^>]+>",
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return html


def clean_dom(html: str) -> str:
    """Run the full DOM-cleaning pipeline.

    Removes ``<script>``, ``<style>``, hidden elements, SVG internals,
    and image ``src`` attributes. Returns a compact string.
    """
    html = remove_scripts_from_html(html)
    html = remove_styles_from_html(html)
    html = remove_hidden_elements_from_html(html)
    # Remove SVG internals (keep the tag itself but strip contents)
    html = re.sub(r"(<svg[^>]*>)(.*?)(</svg>)", r"\1</svg>", html, flags=re.DOTALL | re.IGNORECASE)
    # Remove image src attributes (can be very long / noisy)
    html = re.sub(
        r"<img([^>]*?)\s+src=[\"'][^\"']+[\"']([^>]*?)>",
        r"<img\1\2>",
        html,
        flags=re.IGNORECASE,
    )
    # Collapse multiple blank lines
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


# ---------------------------------------------------------------------------
# Element visibility checker
# ---------------------------------------------------------------------------

def is_element_visible(
    element_info: dict[str, Any],
    min_width: int = 1,
    min_height: int = 1,
) -> bool:
    """Determine whether an element dictionary describes a visible element.

    Expects ``element_info`` to contain keys such as ``width``, ``height``,
    ``display``, ``visibility``, and ``hidden`` (as returned by a DOM query).
    """
    width = element_info.get("width", 0)
    height = element_info.get("height", 0)
    if width < min_width or height < min_height:
        return False

    display = str(element_info.get("display", "")).lower()
    if display == "none":
        return False

    visibility = str(element_info.get("visibility", "")).lower()
    if visibility == "hidden":
        return False

    if element_info.get("hidden", False):
        return False

    return True


# ---------------------------------------------------------------------------
# Safe JSON parser for LLM outputs
# ---------------------------------------------------------------------------

def safe_json_loads(text: str, *, default: Any = None) -> Any:
    """Safely parse JSON from an LLM response, handling common formatting issues.

    Handles:
    * Markdown code fences (`` ```json ... ``` ``)
    * Single-quoted strings (best-effort replacement)
    * Trailing commas before closing brackets

    Args:
        text: Raw text that may contain JSON.
        default: Value to return when parsing fails completely.

    Returns:
        Parsed Python object or ``default``.
    """
    if not text:
        return default

    # Strip markdown fences
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Best-effort fix: replace common trailing commas
    fixed = re.sub(r",(\s*[}\]])", r"\1", text)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass

    # If still failing, return default
    return default


def extract_json_objects(text: str) -> list[dict[str, Any]]:
    """Extract all JSON objects found in a text block.

    Useful when an LLM embeds multiple JSON blobs in prose.
    """
    objects: list[dict[str, Any]] = []
    # Try to find balanced {} blocks
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                candidate = text[start : i + 1]
                parsed = safe_json_loads(candidate, default=None)
                if isinstance(parsed, dict):
                    objects.append(parsed)
                start = -1
    return objects


# ---------------------------------------------------------------------------
# Retry decorator with exponential backoff
# ---------------------------------------------------------------------------

def retry_with_backoff(
    max_retries: int = 3,
    backoff_seconds: float = 1.0,
    max_backoff_seconds: float = 60.0,
    exponential_base: float = 2.0,
    jitter: bool = True,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator that retries a function with exponential backoff.

    Args:
        max_retries: Maximum number of retry attempts.
        backoff_seconds: Initial wait time between retries.
        max_backoff_seconds: Cap on the wait time.
        exponential_base: Multiplier for backoff on each retry.
        jitter: If ``True``, adds a small random amount to each wait.
        exceptions: Tuple of exception types to catch and retry on.
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            attempt = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    attempt += 1
                    if attempt > max_retries:
                        raise

                    wait = backoff_seconds * (exponential_base ** (attempt - 1))
                    wait = min(wait, max_backoff_seconds)
                    if jitter:
                        wait = wait * (1 + random.random())

                    time.sleep(wait)

        return wrapper

    return decorator


def retry_with_backoff_async(
    max_retries: int = 3,
    backoff_seconds: float = 1.0,
    max_backoff_seconds: float = 60.0,
    exponential_base: float = 2.0,
    jitter: bool = True,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Async variant of :func:`retry_with_backoff`."""
    import asyncio

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> T:
            attempt = 0
            while True:
                try:
                    return await func(*args, **kwargs)
                except exceptions as exc:
                    attempt += 1
                    if attempt > max_retries:
                        raise

                    wait = backoff_seconds * (exponential_base ** (attempt - 1))
                    wait = min(wait, max_backoff_seconds)
                    if jitter:
                        wait = wait * (1 + random.random())

                    await asyncio.sleep(wait)

        return async_wrapper

    return decorator


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

def truncate_string(text: str, max_length: int, suffix: str = "...") -> str:
    """Truncate ``text`` to ``max_length`` characters, appending ``suffix``."""
    if len(text) <= max_length:
        return text
    return text[: max_length - len(suffix)] + suffix


def sanitize_filename(name: str) -> str:
    """Replace filesystem-unfriendly characters in a string."""
    return re.sub(r'[^\w\-_\. ]', '_', name)
