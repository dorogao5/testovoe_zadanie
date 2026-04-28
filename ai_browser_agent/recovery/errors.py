from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class RecoveryKind(str, Enum):
    wait_and_resnapshot = "wait_and_resnapshot"
    close_popup = "close_popup"
    scroll_into_view = "scroll_into_view"
    query_again = "query_again"
    screenshot_fallback = "screenshot_fallback"
    navigate_back = "navigate_back"
    ask_user = "ask_user"
    replan = "replan"
    stop = "stop"


class RecoveryPlan(BaseModel):
    kind: RecoveryKind
    reason: str
    instruction: str
    retryable: bool = True

