from __future__ import annotations

import re
from typing import Any

from ai_browser_agent.safety.policy import RiskLevel, SecurityAction, SecurityDecision


class SecurityLayer:
    """Deterministic safety layer for browser side effects.

    The classifier is intentionally generic. It looks at the model-declared intent and
    tool arguments, not at site-specific selectors or routes.
    """

    def __init__(self, *, high_risk_domains: list[str] | None = None) -> None:
        self.high_risk_domains = [domain.lower() for domain in (high_risk_domains or [])]

    def review_tool_call(self, tool: str, args: dict[str, Any], *, user_task: str) -> SecurityDecision:
        intent_text = " ".join(
            [
                tool,
                str(args.get("intent", "")),
                str(args.get("reason", "")),
                str(args.get("url", "")),
                str(args.get("text", "")),
            ]
        ).lower()
        element_role = str(args.get("element_role", "")).lower()
        element_name = str(args.get("element_name", ""))
        element_text = str(args.get("element_text", ""))
        element_action_text = _actionable_element_text(element_role, element_name, element_text)
        action_text = " ".join([intent_text, element_action_text]).lower()
        user_task_text = str(user_task).lower()
        matched: list[str] = []

        if tool == "navigate":
            if self._url_matches_high_risk_domain(str(args.get("url", ""))):
                return SecurityDecision(
                    action=SecurityAction.confirm,
                    risk=RiskLevel.high,
                    reason="The destination is configured as high risk.",
                    matched_patterns=["configured high-risk domain"],
                )
            return SecurityDecision(
                action=SecurityAction.allow,
                risk=RiskLevel.low,
                reason="Navigation is non-destructive; risky words in the broader task are handled at the later side-effecting action.",
                matched_patterns=[],
            )

        if self._is_low_risk_read_or_open(tool, intent_text, element_role, element_action_text):
            return SecurityDecision(
                action=SecurityAction.allow,
                risk=RiskLevel.low,
                reason=(
                    "Opening or reading an existing page item is non-destructive. Risky words in "
                    "untrusted item content are handled only if the agent chooses a real side effect."
                ),
                matched_patterns=[],
            )

        if self._matches(action_text, CRITICAL_PATTERNS, matched):
            return SecurityDecision(
                action=SecurityAction.handoff,
                risk=RiskLevel.critical,
                reason="The action appears to finalize payment, credentials, regulated decisions, CAPTCHA, or a safety-warning bypass.",
                matched_patterns=matched,
            )

        if self._matches(action_text, HIGH_PATTERNS, matched):
            return SecurityDecision(
                action=SecurityAction.confirm,
                risk=RiskLevel.high,
                reason="The action can delete, send, submit, upload, post, or change user-visible data.",
                matched_patterns=matched,
            )

        if tool in {"type_text", "select_option"} or self._matches(action_text, MEDIUM_PATTERNS, matched):
            return SecurityDecision(
                action=SecurityAction.allow,
                risk=RiskLevel.medium,
                reason="The action changes local page state but is not final or destructive.",
                matched_patterns=matched,
            )

        if self._url_matches_high_risk_domain(str(args.get("url", ""))):
            return SecurityDecision(
                action=SecurityAction.confirm,
                risk=RiskLevel.high,
                reason="The destination is configured as high risk.",
                matched_patterns=["configured high-risk domain"],
            )

        if self._matches(user_task_text, HIGH_PATTERNS + CRITICAL_PATTERNS, []):
            return SecurityDecision(
                action=SecurityAction.allow,
                risk=RiskLevel.low,
                reason="The broader task contains risky goals, but this specific action is not risky. Confirmation is required at the exact side-effecting step.",
                matched_patterns=[],
            )

        return SecurityDecision(
            action=SecurityAction.allow,
            risk=RiskLevel.low,
            reason="No risky side effect detected.",
            matched_patterns=matched,
        )

    def detect_prompt_injection(self, texts: list[str]) -> list[str]:
        warnings: list[str] = []
        for text in texts:
            lowered = text.lower()
            for pattern in PROMPT_INJECTION_PATTERNS:
                if re.search(pattern, lowered, re.I):
                    warnings.append(f"Suspicious untrusted content matched /{pattern}/: {text[:160]}")
                    break
        return warnings[:10]

    def _matches(self, text: str, patterns: list[str], matched: list[str]) -> bool:
        found = False
        for pattern in patterns:
            if re.search(pattern, text, re.I):
                matched.append(pattern)
                found = True
        return found

    def _url_matches_high_risk_domain(self, url: str) -> bool:
        lowered = url.lower()
        return any(domain in lowered for domain in self.high_risk_domains)

    def _is_low_risk_read_or_open(
        self,
        tool: str,
        intent_text: str,
        element_role: str,
        element_action_text: str,
    ) -> bool:
        if tool != "click":
            return False
        if element_role not in {"link", "listitem", "row"}:
            return False
        if not re.search(
            r"\b(open|read|view|inspect|review|прочит|открыть|посмотр|изуч|просмотр)\b",
            intent_text,
            re.I,
        ):
            return False
        return not self._matches(element_action_text, CRITICAL_PATTERNS + HIGH_PATTERNS, [])


CRITICAL_PATTERNS = [
    r"\b(pay|payment|purchase|buy now|place order|confirm order|checkout final|оплат|купить|подтвердить заказ)\b",
    r"\b(password change|change password|two-factor|2fa|otp|captcha|safety warning|browser warning)\b",
    r"\b(vpn check|vpn|anti[- ]?bot|bot check|not a robot|cloudflare|security check|access check)\b",
    r"\b(проверка безопасности|я не использую vpn|код для поддержки|не робот)\b",
    r"\b(bank|brokerage|legal|medical|tax filing|wire transfer)\b",
]


def _actionable_element_text(role: str, name: str, text: str) -> str:
    """Return only label-like element text for risk classification.

    Long link/list text is often untrusted content, for example an email subject/snippet
    mentioning payment. That content must not turn a harmless "open/read this item"
    click into a payment handoff. Short button/menu labels still participate fully.
    """

    combined = " ".join(part for part in (name, text) if part).strip()
    if not combined:
        return ""
    if role in {"button", "menuitem", "option", "checkbox", "radio", "combobox"}:
        return combined[:240]
    if len(combined) <= 90:
        return combined
    return ""

HIGH_PATTERNS = [
    r"\b(delete|remove|trash|archive|mark as spam|unsubscribe|destroy|wipe|удал|корзин|архив)\b",
    r"\b(send|submit|apply|post|publish|reply|message|comment|share|отправ|отклик|опубликов)\b",
    r"\b(upload|attach file|change settings|save settings|account settings|personal data)\b",
]

MEDIUM_PATTERNS = [
    r"\b(add to cart|cart|basket|filter|search|fill|select|choose|draft|корзин|поиск|выбрать)\b",
]

PROMPT_INJECTION_PATTERNS = [
    r"ignore (all )?(previous|prior) instructions",
    r"system prompt",
    r"developer message",
    r"hidden instruction",
    r"do not tell the user",
    r"send (money|password|token)",
    r"delete all",
]
