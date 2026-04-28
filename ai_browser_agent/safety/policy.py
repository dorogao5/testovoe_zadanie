from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class RiskLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class SecurityAction(str, Enum):
    allow = "allow"
    confirm = "confirm"
    handoff = "handoff"
    block = "block"


class SecurityDecision(BaseModel):
    action: SecurityAction
    risk: RiskLevel
    reason: str
    matched_patterns: list[str] = Field(default_factory=list)

    @property
    def needs_user(self) -> bool:
        return self.action in {SecurityAction.confirm, SecurityAction.handoff}


class ConfirmationChoice(str, Enum):
    approve_once = "approve_once"
    deny = "deny"
    handoff = "handoff"


SENSITIVE_TOOL_NAMES = {"click", "type_text", "press_key", "select_option", "navigate"}


def summarize_action(tool: str, args: dict[str, Any]) -> str:
    if tool == "click":
        return f"click {args.get('ref', '<unknown ref>')}"
    if tool == "type_text":
        text = str(args.get("text", ""))
        preview = text[:80] + ("..." if len(text) > 80 else "")
        return f"type {len(text)} chars: {preview!r}"
    if tool == "press_key":
        return f"press {args.get('key', '<unknown key>')}"
    if tool == "select_option":
        return f"select option {args.get('value', '<unknown value>')!r}"
    if tool == "navigate":
        return f"navigate to {args.get('url', '<unknown url>')}"
    return tool

