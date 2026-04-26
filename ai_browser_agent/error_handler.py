"""ErrorHandler — Resilient execution with automatic error classification and recovery.

Provides:

* :meth:`execute_with_retry` — wrapper that retries a failing async action
  using configurable strategies.
* :meth:`classify_error` — maps exceptions to :class:`ErrorType`.
* :meth:`suggest_recovery` — maps :class:`ErrorType` to :class:`RecoveryStrategy`.
* Built-in recovery coroutines: ``retry_simple``, ``retry_alternative_selector``,
  ``retry_scroll_and_retry``.

All retry logic is fully async and uses exponential back-off with jitter.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any, Callable

from models import ActionResult, ErrorType, RecoveryStrategy

logger = logging.getLogger("error_handler")

class ErrorHandler:
    """Executes browser actions with automatic retries and recovery strategies."""

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
    ) -> None:
        """
        Args:
            max_retries: Maximum number of retry attempts per action.
            base_delay: Initial back-off delay in seconds.
        """
        self.max_retries = max_retries
        self.base_delay = base_delay

        # Map strategy names to coroutine methods
        self._strategies: dict[str, Callable[..., Any]] = {
            "simple": self.retry_simple,
            "alternative_selector": self.retry_alternative_selector,
            "scroll_and_retry": self.retry_scroll_and_retry,
        }

    # ------------------------------------------------------------------ #
    # Core execution wrapper
    # ------------------------------------------------------------------ #

    async def execute_with_retry(
        self,
        action_func: Callable[..., Any],
        context: dict[str, Any],
        strategy: str = "simple",
    ) -> ActionResult:
        """Execute *action_func* with the chosen recovery strategy.

        Parameters
        ----------
        action_func:
            An async callable that performs the browser action.  It receives
            ``**context`` as keyword arguments.
        context:
            Dictionary of arguments to pass to *action_func*.  May also contain
            a special ``"_selector_alternatives"`` key for the
            ``alternative_selector`` strategy.
        strategy:
            Name of the recovery strategy to use when the action fails.
            One of ``"simple"``, ``"alternative_selector"``, ``"scroll_and_retry"``.

        Returns
        -------
        ActionResult
            Result object indicating success / failure, message, retry count, etc.
        """
        if strategy not in self._strategies:
            logger.warning("Unknown strategy '%s'; falling back to 'simple'", strategy)
            strategy = "simple"

        strategy_func = self._strategies[strategy]

        try:
            result = await strategy_func(action_func, context)
            return result
        except Exception as exc:
            # If the strategy itself raised, produce a final failure result
            error_type = self.classify_error(exc)
            recovery = self.suggest_recovery(error_type)
            logger.error(
                "Action failed after %d retries (%s): %s",
                result.retry_count if "result" in dir() else 0,
                error_type.value,
                exc,
            )
            return ActionResult(
                success=False,
                message=f"Action failed: {exc}",
                data={"exception": str(exc), "recovery": recovery.model_dump()},
                error_type=error_type.value,
                retry_count=getattr(result, "retry_count", 0) if "result" in dir() else 0,
            )

    # ------------------------------------------------------------------ #
    # Error classification
    # ------------------------------------------------------------------ #

    def classify_error(self, error: Exception) -> ErrorType:
        """Map an exception to a known :class:`ErrorType`.

        Heuristics are based on exception type names and messages so that
        framework-specific exceptions (Playwright, network, etc.) are handled
        without hard imports.
        """
        exc_name = type(error).__name__.lower()
        exc_msg = str(error).lower()

        # Timeout family
        if any(kw in exc_name for kw in ("timeout", "timedout", "waiterror")):
            return ErrorType.TIMEOUT

        # Selector / element not found
        if any(
            kw in exc_msg
            for kw in (
                "could not find element",
                "no element matches",
                "element not found",
                "selector",
                "locator",
                "resolved to 0",
            )
        ):
            return ErrorType.SELECTOR_NOT_FOUND

        if any(kw in exc_name for kw in ("notfound", "nosuchelement", "selectorerror")):
            return ErrorType.SELECTOR_NOT_FOUND

        # Navigation
        if any(kw in exc_name for kw in ("navigation", "navigate", "goto", "net")):
            if "404" in exc_msg or "not found" in exc_msg:
                return ErrorType.NAVIGATION_ERROR
            return ErrorType.NETWORK_ERROR

        # Network / connection
        if any(
            kw in exc_msg
            for kw in (
                "net::",
                "err_",
                "connection",
                "refused",
                "reset",
                "dns",
                "offline",
            )
        ):
            return ErrorType.NETWORK_ERROR

        # Rate limit
        if any(kw in exc_msg for kw in ("rate limit", "too many requests", "429")):
            return ErrorType.RATE_LIMIT

        # Authentication
        if any(kw in exc_msg for kw in ("401", "403", "unauthorized", "forbidden", "auth")):
            return ErrorType.AUTHENTICATION_ERROR

        # Stale element reference (detached from DOM)
        if any(kw in exc_msg for kw in ("stale", "detached", "element is no longer")):
            return ErrorType.SELECTOR_NOT_FOUND  # Maps to selector family for recovery

        # Element not visible / hidden
        if any(kw in exc_msg for kw in ("not visible", "hidden", "display:none", "invisible")):
            # We return a custom string but ErrorType doesn't have a dedicated one;
            # map to SELECTOR_NOT_FOUND so recovery scrolls / waits.
            return ErrorType.SELECTOR_NOT_FOUND

        # Unexpected alert / dialog
        if any(kw in exc_msg for kw in ("alert", "dialog", "confirm", "prompt")):
            # ErrorType doesn't have a dedicated dialog type; map to UNKNOWN
            return ErrorType.UNKNOWN

        # LLM errors
        if any(kw in exc_name for kw in ("apierror", "openai", "kimi", "llm")):
            return ErrorType.LLM_ERROR

        # Validation
        if any(kw in exc_name for kw in ("validation", "valueerror", "typeerror")):
            return ErrorType.VALIDATION_ERROR

        return ErrorType.UNKNOWN

    def suggest_recovery(self, error_type: ErrorType) -> RecoveryStrategy:
        """Suggest a recovery strategy for a classified error."""
        strategies: dict[ErrorType, RecoveryStrategy] = {
            ErrorType.TIMEOUT: RecoveryStrategy(
                strategy="simple",
                description="Wait longer and retry — element may be lazy-loaded.",
                max_attempts=3,
                backoff_seconds=2.0,
            ),
            ErrorType.SELECTOR_NOT_FOUND: RecoveryStrategy(
                strategy="alternative_selector",
                description="Try a broader or alternative selector strategy.",
                max_attempts=3,
                backoff_seconds=1.0,
            ),
            ErrorType.NAVIGATION_ERROR: RecoveryStrategy(
                strategy="simple",
                description="Check the URL and retry with https or a corrected path.",
                max_attempts=2,
                backoff_seconds=1.5,
            ),
            ErrorType.NETWORK_ERROR: RecoveryStrategy(
                strategy="simple",
                description="Retry with exponential backoff for transient network issues.",
                max_attempts=5,
                backoff_seconds=2.0,
            ),
            ErrorType.RATE_LIMIT: RecoveryStrategy(
                strategy="simple",
                description="Wait longer between retries due to rate limiting.",
                max_attempts=5,
                backoff_seconds=5.0,
            ),
            ErrorType.AUTHENTICATION_ERROR: RecoveryStrategy(
                strategy="abort",
                description="Authentication failed — manual intervention required.",
                max_attempts=1,
                backoff_seconds=0.0,
            ),
            ErrorType.LLM_ERROR: RecoveryStrategy(
                strategy="simple",
                description="Retry the LLM call with reduced complexity.",
                max_attempts=3,
                backoff_seconds=2.0,
            ),
            ErrorType.VALIDATION_ERROR: RecoveryStrategy(
                strategy="ask_user",
                description="Input validation failed — ask user for corrected parameters.",
                max_attempts=1,
                backoff_seconds=0.0,
            ),
            ErrorType.UNKNOWN: RecoveryStrategy(
                strategy="simple",
                description="Generic retry for unclassified errors.",
                max_attempts=3,
                backoff_seconds=1.0,
            ),
        }
        return strategies.get(error_type, strategies[ErrorType.UNKNOWN])

    # ------------------------------------------------------------------ #
    # Built-in recovery strategies
    # ------------------------------------------------------------------ #

    async def retry_simple(
        self,
        action_func: Callable[..., Any],
        context: dict[str, Any],
    ) -> ActionResult:
        """Retry the same action with exponential back-off and jitter."""
        last_exception: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                result = await action_func(**context)
                # If the action returns an ActionResult, pass it through enriched
                if isinstance(result, ActionResult):
                    result.retry_count = attempt
                    return result
                # Otherwise wrap it
                return ActionResult(
                    success=True,
                    message=str(result),
                    data={"raw_result": result},
                    retry_count=attempt,
                )
            except Exception as exc:
                last_exception = exc
                error_type = self.classify_error(exc)
                recovery = self.suggest_recovery(error_type)

                if attempt == self.max_retries:
                    break

                # Exponential back-off with jitter
                delay = min(
                    recovery.backoff_seconds * (2 ** attempt),
                    60.0,
                )
                jitter = delay * random.uniform(0, 0.2)
                sleep_time = delay + jitter

                logger.warning(
                    "Retry %d/%d for '%s' after %s (%.2fs sleep): %s",
                    attempt + 1,
                    self.max_retries,
                    action_func.__name__ if hasattr(action_func, "__name__") else "action",
                    error_type.value,
                    sleep_time,
                    exc,
                )
                await asyncio.sleep(sleep_time)

        # All retries exhausted
        final_error_type = self.classify_error(last_exception) if last_exception else ErrorType.UNKNOWN
        return ActionResult(
            success=False,
            message=f"Failed after {self.max_retries} retries: {last_exception}",
            data={"exception": str(last_exception) if last_exception else None},
            error_type=final_error_type.value,
            retry_count=self.max_retries,
        )

    async def retry_alternative_selector(
        self,
        action_func: Callable[..., Any],
        context: dict[str, Any],
    ) -> ActionResult:
        """Retry with modified selector strategies.

        Expects ``context`` to optionally contain
        ``"_selector_alternatives"`` — a list of alternative selector strings
        or element-description dicts to try in order.
        """
        alternatives = context.get("_selector_alternatives", [])
        original_context = dict(context)

        # If no alternatives were provided, we just do a simple retry but log the
        # hint that the caller should supply alternatives next time.
        if not alternatives:
            logger.info(
                "No selector alternatives provided; falling back to simple retry."
            )
            return await self.retry_simple(action_func, context)

        last_exception: Exception | None = None
        total_attempts = 0

        # Try each alternative up to max_retries total across all alternatives
        for alt_idx, alt in enumerate(alternatives):
            modified_context = dict(original_context)
            if isinstance(alt, dict):
                modified_context.update(alt)
            else:
                # Assume it's a selector string; try common keys
                modified_context["selector"] = alt
                modified_context["element_description"] = alt

            for attempt in range(self.max_retries + 1):
                total_attempts += 1
                try:
                    result = await action_func(**modified_context)
                    if isinstance(result, ActionResult):
                        result.retry_count = total_attempts - 1
                        return result
                    return ActionResult(
                        success=True,
                        message=f"Succeeded with alternative selector {alt_idx}.",
                        data={"raw_result": result, "alternative_used": alt},
                        retry_count=total_attempts - 1,
                    )
                except Exception as exc:
                    last_exception = exc
                    error_type = self.classify_error(exc)
                    if attempt < self.max_retries:
                        delay = min(self.base_delay * (2 ** attempt), 60.0)
                        jitter = delay * random.uniform(0, 0.2)
                        await asyncio.sleep(delay + jitter)
                    else:
                        break  # Move to next alternative

        final_error_type = self.classify_error(last_exception) if last_exception else ErrorType.UNKNOWN
        return ActionResult(
            success=False,
            message=(
                f"Failed after trying {len(alternatives)} alternative selector(s): "
                f"{last_exception}"
            ),
            data={"exception": str(last_exception) if last_exception else None},
            error_type=final_error_type.value,
            retry_count=total_attempts - 1,
        )

    async def retry_scroll_and_retry(
        self,
        action_func: Callable[..., Any],
        context: dict[str, Any],
    ) -> ActionResult:
        """Scroll the page (down / up) and retry the action.

        Useful when elements are below the fold, inside lazy-loaded regions,
        or hidden behind animation.
        """
        last_exception: Exception | None = None
        scroll_directions = ["down", "up"]
        scroll_amounts = [500, 800, 1200]

        for attempt in range(self.max_retries + 1):
            try:
                result = await action_func(**context)
                if isinstance(result, ActionResult):
                    result.retry_count = attempt
                    return result
                return ActionResult(
                    success=True,
                    message="Action succeeded.",
                    data={"raw_result": result},
                    retry_count=attempt,
                )
            except Exception as exc:
                last_exception = exc
                if attempt == self.max_retries:
                    break

                # Scroll before the next attempt
                direction = scroll_directions[attempt % len(scroll_directions)]
                amount = scroll_amounts[min(attempt, len(scroll_amounts) - 1)]

                logger.info(
                    "Scrolling %s by %d px before retry %d/%d",
                    direction,
                    amount,
                    attempt + 1,
                    self.max_retries,
                )

                # If a page object is in context, use it to scroll
                page = context.get("page")
                browser = context.get("browser")
                if page and hasattr(page, "evaluate"):
                    delta = -amount if direction == "up" else amount
                    try:
                        await page.evaluate(f"window.scrollBy(0, {delta})")
                        await asyncio.sleep(0.5)
                    except Exception as scroll_exc:
                        logger.debug("Scroll via page object failed: %s", scroll_exc)
                elif browser and hasattr(browser, "scroll"):
                    try:
                        await browser.scroll(direction=direction, amount=amount)
                        await asyncio.sleep(0.5)
                    except Exception as scroll_exc:
                        logger.debug("Scroll via browser controller failed: %s", scroll_exc)
                else:
                    # No scrollable object available — just wait a bit
                    await asyncio.sleep(1.0)

                # Exponential back-off
                delay = min(self.base_delay * (2 ** attempt), 60.0)
                jitter = delay * random.uniform(0, 0.2)
                await asyncio.sleep(delay + jitter)

        final_error_type = self.classify_error(last_exception) if last_exception else ErrorType.UNKNOWN
        return ActionResult(
            success=False,
            message=f"Failed after {self.max_retries} scroll-and-retry attempts: {last_exception}",
            data={"exception": str(last_exception) if last_exception else None},
            error_type=final_error_type.value,
            retry_count=self.max_retries,
        )
