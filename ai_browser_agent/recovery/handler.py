from __future__ import annotations

from ai_browser_agent.recovery.errors import RecoveryKind, RecoveryPlan


class ErrorHandler:
    def choose(self, error_class: str, *, repeated_failures: int = 0) -> RecoveryPlan:
        if repeated_failures >= 3:
            return RecoveryPlan(
                kind=RecoveryKind.replan,
                reason="Repeated failures on the same path.",
                instruction="Observe the page again, revise the plan, and choose a different route.",
            )
        mapping = {
            "timeout_loading": RecoveryPlan(
                kind=RecoveryKind.wait_and_resnapshot,
                reason="The page may still be loading.",
                instruction="Wait briefly, then observe again.",
            ),
            "ref_not_found": RecoveryPlan(
                kind=RecoveryKind.query_again,
                reason="The element ref is stale or missing.",
                instruction="Call observe or query_dom and retry with a current ref.",
            ),
            "click_failed": RecoveryPlan(
                kind=RecoveryKind.screenshot_fallback,
                reason="The click may be blocked by overlay, layout shift, or poor DOM mapping.",
                instruction="Observe again, consider closing overlays, or take an annotated screenshot.",
            ),
            "type_failed": RecoveryPlan(
                kind=RecoveryKind.query_again,
                reason="The target may not be editable or is stale.",
                instruction="Find an editable textbox-like element and retry.",
            ),
            "navigation_failed": RecoveryPlan(
                kind=RecoveryKind.navigate_back,
                reason="Navigation did not complete.",
                instruction="Check current URL/title and choose another navigation path.",
            ),
        }
        return mapping.get(
            error_class,
            RecoveryPlan(
                kind=RecoveryKind.replan,
                reason="Unexpected error.",
                instruction="Inspect the latest state and choose a recovery action.",
            ),
        )

