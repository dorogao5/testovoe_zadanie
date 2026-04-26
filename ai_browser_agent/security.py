"""SecurityLayer — Gatekeeper for destructive or high-risk browser actions.

Inspects every proposed action, classifies its risk level, detects destructive
keywords, and returns a :class:`SecurityDecision` that tells the caller whether
to allow, block, or ask the user for confirmation.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from models import SecurityDecision

logger = logging.getLogger("security")

class SecurityLayer:
    """Inspects browser actions and decides whether they are safe to execute."""

    # Destructive keywords for click actions (case-insensitive)
    _CLICK_DESTRUCTIVE_KEYWORDS = [
        "delete",
        "remove",
        "trash",
        "unsubscribe",
        "confirm",
        "pay",
        "buy",
        "checkout",
        "submit order",
        "permanently",
        "erase",
    ]

    # Patterns that indicate sensitive data entry
    _SENSITIVE_TYPE_PATTERNS = [
        (re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"), "credit_card_number"),
        (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "ssn"),
        (re.compile(r"password", re.IGNORECASE), "password_field"),
    ]

    # Destructive URL path segments
    _DESTRUCTIVE_URL_PATHS = [
        "/delete",
        "/remove",
        "/checkout",
        "/pay",
        "/buy",
        "/confirm",
    ]

    # Auto-approved action types (can be overridden at instance level)
    _SAFE_ACTIONS = {"scroll", "find_information", "ask_user", "done"}

    def __init__(
        self,
        auto_approve: list[str] | None = None,
        ask_before_critical: bool = True,
    ) -> None:
        """
        Args:
            auto_approve: Action types that are always allowed (e.g. ["scroll"]).
            ask_before_critical: If True, CRITICAL / HIGH risk actions return
                ``ask_user`` instead of being blocked outright.
        """
        self._auto_approve: set[str] = set(auto_approve or [])
        self.ask_before_critical = ask_before_critical

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def check_action(self, action_type: str, action_params: dict[str, Any]) -> SecurityDecision:
        """Evaluate a proposed action and return a security decision.

        The logic follows the risk-level hierarchy:

        * ``auto_approve`` list → always allow.
        * Safe action types (scroll, find_information, etc.) → allow.
        * Destructive / critical → ``ask_user`` (or ``block`` if configured).
        * High risk → ``ask_user`` or allow with warning.
        * Medium risk → allow with logged warning.
        * Low risk → silent allow.
        """
        # 1. Auto-approve list takes precedence
        if action_type in self._auto_approve:
            return SecurityDecision(
                verdict=SecurityDecision.Verdict.ALLOW,
                explanation=f"Action type '{action_type}' is in the auto-approve list.",
                risk_level="low",
            )

        # 2. Known safe actions
        if action_type in self._SAFE_ACTIONS:
            return SecurityDecision(
                verdict=SecurityDecision.Verdict.ALLOW,
                explanation=f"'{action_type}' is a read-only / safe action.",
                risk_level="low",
            )

        # 3. Assess risk
        risk_level = self.get_risk_level(action_type, action_params)
        is_destructive = self.is_destructive(action_type, action_params)
        keywords: list[str] = []

        if is_destructive:
            keywords = self._extract_destructive_keywords(action_type, action_params)

        warning = self.format_warning(action_type, action_params, risk_level)

        # 4. Render verdict
        if risk_level == "critical":
            if self.ask_before_critical:
                return SecurityDecision(
                    verdict=SecurityDecision.Verdict.ASK_USER,
                    explanation=f"Critical-risk action detected: {action_type}. User confirmation required.",
                    risk_level="critical",
                    warning_message=warning,
                    destructive_keywords_found=keywords,
                )
            return SecurityDecision(
                verdict=SecurityDecision.Verdict.BLOCK,
                explanation=f"Critical-risk action blocked: {action_type}.",
                risk_level="critical",
                warning_message=warning,
                destructive_keywords_found=keywords,
            )

        if risk_level == "high":
            if self.ask_before_critical:
                return SecurityDecision(
                    verdict=SecurityDecision.Verdict.ASK_USER,
                    explanation=f"High-risk action detected: {action_type}. User confirmation recommended.",
                    risk_level="high",
                    warning_message=warning,
                    destructive_keywords_found=keywords,
                )
            # When ask_before_critical is False, we allow high-risk but warn
            return SecurityDecision(
                verdict=SecurityDecision.Verdict.ALLOW,
                explanation=f"High-risk action allowed (ask_before_critical=False): {action_type}.",
                risk_level="high",
                warning_message=warning,
                destructive_keywords_found=keywords,
            )

        if risk_level == "medium":
            return SecurityDecision(
                verdict=SecurityDecision.Verdict.ALLOW,
                explanation=f"Medium-risk action allowed with warning: {action_type}.",
                risk_level="medium",
                warning_message=warning,
                destructive_keywords_found=keywords,
            )

        # low
        return SecurityDecision(
            verdict=SecurityDecision.Verdict.ALLOW,
            explanation=f"Low-risk action approved: {action_type}.",
            risk_level="low",
        )

    def is_destructive(self, action_type: str, params: dict[str, Any]) -> bool:
        """Return ``True`` if the action appears irreversible or harmful."""
        if action_type == "click":
            desc = params.get("element_description", "")
            return any(
                kw in desc.lower() for kw in self._CLICK_DESTRUCTIVE_KEYWORDS
            )

        if action_type == "type_text":
            text = params.get("text", "")
            element = params.get("element_description", "")
            # Sensitive data patterns
            for pattern, _name in self._SENSITIVE_TYPE_PATTERNS:
                if pattern.search(text):
                    return True
            # Password field typing
            if "password" in element.lower():
                return True
            return False

        if action_type == "navigate":
            url = params.get("url", "")
            return any(
                url.lower().startswith("http") and path in url.lower()
                for path in self._DESTRUCTIVE_URL_PATHS
            ) or any(path in url.lower() for path in self._DESTRUCTIVE_URL_PATHS)

        if action_type == "press_key":
            key = params.get("key", "").lower()
            context = params.get("context", "")
            # Enter on a form can trigger submission
            if key in ("enter", "return") and "form" in context.lower():
                return True
            return False

        return False

    def get_risk_level(self, action_type: str, params: dict[str, Any]) -> str:
        """Classify the action into one of ``low``, ``medium``, ``high``, ``critical``."""
        # Critical: financial transactions, permanent deletions
        if action_type == "click":
            desc = params.get("element_description", "").lower()
            if any(kw in desc for kw in ("pay", "buy", "checkout", "submit order")):
                return "critical"
            if any(kw in desc for kw in ("delete", "remove", "trash", "unsubscribe", "permanently", "erase")):
                return "critical"
            if "confirm" in desc:
                return "high"

        if action_type == "type_text":
            text = params.get("text", "")
            element = params.get("element_description", "").lower()
            # Credit card or SSN
            for pattern, name in self._SENSITIVE_TYPE_PATTERNS:
                if pattern.search(text):
                    if name == "credit_card_number":
                        return "critical"
                    if name == "ssn":
                        return "critical"
            # Password fields
            if "password" in element:
                return "high"
            # Login fields (username / email)
            if any(kw in element for kw in ("login", "username", "email", "sign in")):
                return "medium"
            return "medium"

        if action_type == "navigate":
            url = params.get("url", "").lower()
            if any(path in url for path in ("/checkout", "/pay", "/buy", "/confirm")):
                return "critical"
            if any(path in url for path in ("/delete", "/remove")):
                return "critical"
            # Login pages are medium risk
            if any(kw in url for kw in ("login", "signin", "auth")):
                return "medium"
            return "medium"

        if action_type == "press_key":
            key = params.get("key", "").lower()
            context = params.get("context", "").lower()
            if key in ("enter", "return"):
                if "form" in context or "submit" in context:
                    return "high"
                return "medium"
            return "low"

        # Default action types
        if action_type in ("scroll", "find_information", "ask_user", "done", "wait"):
            return "low"

        return "medium"

    def format_warning(self, action_type: str, params: dict[str, Any], risk_level: str) -> str | None:
        """Produce a human-readable warning for the given action and risk level."""
        if risk_level == "low":
            return None

        desc = params.get("element_description", "")
        url = params.get("url", "")
        text = params.get("text", "")
        key = params.get("key", "")

        parts: list[str] = []
        parts.append(f"Risk level: {risk_level.upper()}")
        parts.append(f"Action: {action_type}")

        if desc:
            parts.append(f"Target: {desc}")
        if url:
            parts.append(f"URL: {url}")
        if text:
            # Mask sensitive text
            masked = self._mask_sensitive(text)
            parts.append(f"Input: {masked}")
        if key:
            parts.append(f"Key: {key}")

        # Add contextual advice
        if risk_level == "critical":
            parts.append(
                "⚠️  This action may be IRREVERSIBLE or involve FINANCIAL impact. "
                "Please confirm before proceeding."
            )
        elif risk_level == "high":
            parts.append(
                "⚠️  This action may submit data or change account state. "
                "Please review carefully."
            )
        elif risk_level == "medium":
            parts.append("ℹ️  This action may affect page state or send data.")

        return "\n".join(parts)

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _extract_destructive_keywords(
        self, action_type: str, params: dict[str, Any]
    ) -> list[str]:
        """Return the specific destructive keywords matched by this action."""
        found: list[str] = []
        if action_type == "click":
            desc = params.get("element_description", "").lower()
            for kw in self._CLICK_DESTRUCTIVE_KEYWORDS:
                if kw in desc:
                    found.append(kw)
        elif action_type == "navigate":
            url = params.get("url", "").lower()
            for path in self._DESTRUCTIVE_URL_PATHS:
                if path in url:
                    found.append(path.lstrip("/"))
        elif action_type == "type_text":
            text = params.get("text", "")
            for pattern, name in self._SENSITIVE_TYPE_PATTERNS:
                if pattern.search(text):
                    found.append(name)
        return found

    @staticmethod
    def _mask_sensitive(text: str) -> str:
        """Mask potentially sensitive input for display purposes."""
        if len(text) <= 4:
            return "****"
        return text[:2] + "****" + text[-2:]
