"""Pydantic models for the AI Browser Automation Agent.

All data structures used across the agent system, including browser state,
agent decisions, action results, and error classifications.
"""

from __future__ import annotations

import base64
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ToolCall(BaseModel):
    """Represents a function/tool call from the LLM."""

    id: str = Field(description="Unique identifier for this tool call")
    name: str = Field(description="Name of the function/tool being called")
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="Arguments passed to the function",
    )

    @field_validator("name")
    @classmethod
    def _name_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Tool name must not be empty")
        return v

    @field_validator("arguments")
    @classmethod
    def _arguments_not_none(cls, v: dict[str, Any] | None) -> dict[str, Any]:
        return v if v is not None else {}


class BrowserState(BaseModel):
    """Snapshot of the current browser state."""

    url: str = Field(description="Current page URL")
    title: str = Field(description="Current page title")
    distilled_dom: str = Field(description="Semantic, interactive elements distilled DOM")
    screenshot: bytes | None = Field(
        default=None,
        description="Screenshot bytes (optional)",
    )
    timestamp: datetime = Field(
        default_factory=datetime.utcnow,
        description="When the state was captured",
    )

    def screenshot_b64(self) -> str | None:
        """Return the screenshot encoded as a base64 string."""
        if self.screenshot is None:
            return None
        return base64.b64encode(self.screenshot).decode("ascii")

    @field_validator("url")
    @classmethod
    def _url_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("URL must not be empty")
        return v


class Step(BaseModel):
    """A single step in the agent's execution history."""

    number: int = Field(description="Sequential step number (1-based)", ge=1)
    thought: str = Field(description="The agent's reasoning for this step")
    action: ToolCall = Field(description="The tool call executed")
    result: str = Field(description="Result or observation after executing the action")
    timestamp: datetime = Field(
        default_factory=datetime.utcnow,
        description="When the step was executed",
    )


class AgentResult(BaseModel):
    """Final result after the agent completes (or fails) a task."""

    success: bool = Field(description="Whether the task was completed successfully")
    task: str = Field(description="Original task description")
    steps: list[Step] = Field(
        default_factory=list,
        description="Complete execution history",
    )
    final_answer: str = Field(
        default="",
        description="Summary of what was accomplished or the final answer",
    )
    total_steps: int = Field(default=0, description="Total number of steps taken")
    total_time_seconds: float = Field(
        default=0.0,
        description="Total wall-clock time in seconds",
        ge=0,
    )


class PageAnalysis(BaseModel):
    """Analysis of a web page produced by the Explorer sub-agent."""

    page_type: str = Field(
        description="Detected page type (e.g., login, listing, form, detail, search_results)",
    )
    available_actions: list[str] = Field(
        default_factory=list,
        description="List of high-level actions that appear possible on this page",
    )
    key_elements: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Important interactive elements with metadata",
    )
    navigation_options: list[str] = Field(
        default_factory=list,
        description="Available navigation options (links, breadcrumbs, etc.)",
    )


class Action(BaseModel):
    """Represents a concrete browser action to be executed."""

    type: str = Field(description="Action type (navigate, click, type_text, scroll, etc.)")
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Action-specific parameters",
    )
    description: str = Field(
        default="",
        description="Human-readable description of what this action does",
    )

    @field_validator("type")
    @classmethod
    def _type_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Action type must not be empty")
        return v


class ActionResult(BaseModel):
    """Outcome of executing a single browser action."""

    success: bool = Field(description="Whether the action executed without errors")
    message: str = Field(description="Human-readable result message")
    data: dict[str, Any] = Field(
        default_factory=dict,
        description="Any structured data returned by the action",
    )
    screenshot_after: bytes | None = Field(
        default=None,
        description="Screenshot taken immediately after the action",
    )
    error_type: str | None = Field(
        default=None,
        description="Classified error type when action fails",
    )
    retry_count: int = Field(
        default=0,
        description="How many retries were consumed",
        ge=0,
    )
    timestamp: datetime = Field(
        default_factory=datetime.utcnow,
        description="When the action result was recorded",
    )


class AgentDecision(BaseModel):
    """The agent's decision for the next action, including reasoning."""

    thought: str = Field(description="Reasoning behind the decision")
    action: Action = Field(description="The chosen action to execute")
    confidence: float = Field(
        default=1.0,
        description="Confidence in this decision (0.0–1.0)",
        ge=0.0,
        le=1.0,
    )
    needs_verification: bool = Field(
        default=False,
        description="Whether this action should be verified afterwards",
    )


class SecurityDecision(BaseModel):
    """Decision produced by the SecurityLayer for a proposed action."""

    class Verdict(str, Enum):
        """Possible security verdicts."""

        ALLOW = "allow"
        BLOCK = "block"
        ASK_USER = "ask_user"

    verdict: Verdict = Field(description="Security verdict")
    risk_level: str = Field(description="Risk level (low, medium, high, critical)")
    explanation: str = Field(
        default="",
        description="Human-readable explanation of the decision",
    )
    destructive_keywords_found: list[str] = Field(
        default_factory=list,
        description="List of destructive keywords that triggered the decision",
    )
    warning_message: str | None = Field(
        default=None,
        description="Formatted warning message for user display",
    )


class ErrorType(str, Enum):
    """Classification of error types for recovery."""

    TIMEOUT = "timeout"
    SELECTOR_NOT_FOUND = "selector_not_found"
    NAVIGATION_ERROR = "navigation_error"
    NETWORK_ERROR = "network_error"
    RATE_LIMIT = "rate_limit"
    AUTHENTICATION_ERROR = "authentication_error"
    UNKNOWN = "unknown"
    VALIDATION_ERROR = "validation_error"
    LLM_ERROR = "llm_error"
    SECURITY_BLOCKED = "security_blocked"


class RecoveryStrategy(BaseModel):
    """Suggested recovery strategy for a given error."""

    strategy: str = Field(
        description="Strategy name (simple, alternative_selector, scroll_and_retry, ask_user, abort)",
    )
    description: str = Field(
        description="Human-readable description of the recovery approach",
    )
    max_attempts: int = Field(default=3, description="Maximum retry attempts", ge=1)
    backoff_seconds: float = Field(
        default=1.0,
        description="Initial backoff between retries",
        ge=0,
    )


class VerificationResult(BaseModel):
    """Result produced by the Critic sub-agent to verify task progress."""

    is_complete: bool = Field(description="Whether the task appears fully completed")
    is_on_track: bool = Field(description="Whether the agent is still making progress")
    issues: list[str] = Field(
        default_factory=list,
        description="List of problems or concerns",
    )
    suggestions: list[str] = Field(
        default_factory=list,
        description="Suggested next steps or corrections",
    )
    confidence: float = Field(
        default=0.0,
        description="Confidence in the verification (0.0–1.0)",
        ge=0.0,
        le=1.0,
    )


class StepResult(BaseModel):
    """Result of a full observe-reason-act cycle."""

    step_number: int = Field(description="Step number", ge=1)
    state: BrowserState = Field(description="Browser state observed")
    decision: AgentDecision = Field(description="Agent decision made")
    action_result: ActionResult = Field(description="Result of executing the action")
    verification: VerificationResult | None = Field(
        default=None,
        description="Optional critic verification result",
    )
    timestamp: datetime = Field(
        default_factory=datetime.utcnow,
        description="When the step result was recorded",
    )
