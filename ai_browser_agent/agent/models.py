from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from ai_browser_agent.browser.actions import BrowserState


class PlanStatus(str, Enum):
    pending = "pending"
    current = "current"
    done = "done"
    skipped = "skipped"


class PlanItem(BaseModel):
    text: str
    status: PlanStatus = PlanStatus.pending

    def render(self) -> str:
        marker = {
            PlanStatus.pending: "[ ]",
            PlanStatus.current: "[>]",
            PlanStatus.done: "[x]",
            PlanStatus.skipped: "[-]",
        }[self.status]
        return f"{marker} {self.text}"


class ActionRecord(BaseModel):
    step: int
    tool: str
    args: dict[str, Any]
    ok: bool
    summary: str
    target: dict[str, Any] | None = None
    page_fingerprint: str | None = None


class FailureRecord(BaseModel):
    step: int
    tool: str
    error_class: str
    message: str
    recovery: str


class ArtifactRef(BaseModel):
    kind: str
    path: Path
    note: str | None = None


class ConfirmationRequest(BaseModel):
    action: str
    target: str
    data: dict[str, Any] = Field(default_factory=dict)
    reason: str
    risk: str


class AgentState(BaseModel):
    task: str
    run_id: str
    step: int = 0
    plan: list[PlanItem] = Field(default_factory=list)
    memory: str = ""
    last_observation: BrowserState | None = None
    recent_actions: list[ActionRecord] = Field(default_factory=list)
    failures: list[FailureRecord] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    pending_confirmation: ConfirmationRequest | None = None

    def add_action(self, record: ActionRecord, *, keep: int = 24) -> None:
        self.recent_actions.append(record)
        if len(self.recent_actions) > keep:
            self.recent_actions = self.recent_actions[-keep:]

    def add_failure(self, record: FailureRecord, *, keep: int = 20) -> None:
        self.failures.append(record)
        if len(self.failures) > keep:
            self.failures = self.failures[-keep:]


class FinalResult(BaseModel):
    success: bool
    summary: str
    evidence: list[str] = Field(default_factory=list)
    remaining_risks: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)


class ToolExecutionResult(BaseModel):
    ok: bool
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    stop: bool = False
    final: FinalResult | None = None


class ModelRole(str, Enum):
    fast = "fast"
    primary = "primary"
    strong = "strong"
    vision = "vision"


class RunStatus(str, Enum):
    succeeded = "succeeded"
    partial = "partial"
    failed = "failed"
    blocked = "blocked"


class UserAnswer(BaseModel):
    answer: str
    source: Literal["terminal", "auto"] = "terminal"
