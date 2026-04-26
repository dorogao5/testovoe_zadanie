"""SubAgentOrchestrator and specialized sub-agents for the AI Browser Agent.

Each sub-agent is a focused LLM call with a specialised prompt.  The orchestrator
provides a thin delegation layer so that :class:`AgentCore` can ask for plans,
page analyses, verifications, and summaries without worrying about prompt
crafting or parsing.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from kimi_client import KimiClient
from models import (
    BrowserState,
    PageAnalysis,
    Step,
    ToolCall,
    VerificationResult,
)
from prompts import (
    CRITIC_PROMPT,
    EXPLORER_PROMPT,
    PLANNER_PROMPT,
    SUMMARIZER_PROMPT,
)
from utils import extract_json_objects, retry_with_backoff_async, safe_json_loads

logger = logging.getLogger("sub_agents")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_LLM_RETRIES = 3
"""How many times to retry a sub-agent LLM call when parsing fails."""


# ---------------------------------------------------------------------------
# PlannerAgent
# ---------------------------------------------------------------------------

class PlannerAgent:
    """Breaks a high-level task into an ordered list of :class:`Step` objects."""

    def __init__(self, llm: KimiClient) -> None:
        self._llm = llm

    async def plan(self, task: str, state: BrowserState) -> list[Step]:
        """Ask the LLM to produce a step-by-step plan.

        The prompt instructs the model to return a JSON array of objects with
        fields ``number``, ``thought``, ``action_type``, and ``description``.
        These are converted into :class:`Step` instances with synthetic
        :class:`ToolCall` actions.
        """
        prompt = PLANNER_PROMPT.format(
            url=state.url,
            title=state.title,
            distilled_dom=_truncate_dom(state.distilled_dom),
            task=task,
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "You are a task-planning specialist."},
            {"role": "user", "content": prompt},
        ]

        raw_response = await self._llm.chat(
            messages=messages,
            temperature=0.2,
            max_tokens=2048,
        )
        content = self._llm.extract_content(raw_response)

        # Try to find a JSON array inside the response
        parsed = safe_json_loads(content, default=None)
        if not isinstance(parsed, list):
            # Maybe the model wrapped it in prose – try extracting JSON objects
            objects = extract_json_objects(content)
            if objects and isinstance(objects[0], list):
                parsed = objects[0]
            elif objects:
                # If we only got objects, wrap the ones that look like steps
                parsed = [o for o in objects if isinstance(o, dict) and "number" in o]
            else:
                parsed = []

        steps: list[Step] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            step_number = item.get("number", len(steps) + 1)
            thought = item.get("thought", item.get("rationale", ""))
            action_type = item.get("action_type", "navigate")
            description = item.get("description", "")

            tool_call = ToolCall(
                id=f"plan_{uuid.uuid4().hex[:8]}",
                name=action_type,
                arguments={"description": description},
            )

            steps.append(
                Step(
                    number=step_number,
                    thought=thought,
                    action=tool_call,
                    result="",
                )
            )

        logger.info("PlannerAgent produced %d step(s)", len(steps))
        return steps


# ---------------------------------------------------------------------------
# ExplorerAgent
# ---------------------------------------------------------------------------

class ExplorerAgent:
    """Analyses a page and returns structured metadata about it."""

    def __init__(self, llm: KimiClient) -> None:
        self._llm = llm

    async def explore(self, state: BrowserState) -> PageAnalysis:
        """Send the distilled DOM to the LLM and parse a :class:`PageAnalysis`."""
        prompt = EXPLORER_PROMPT.format(
            url=state.url,
            title=state.title,
            distilled_dom=_truncate_dom(state.distilled_dom),
        )

        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": "You are a page-structure analyst. Respond with JSON only.",
            },
            {"role": "user", "content": prompt},
        ]

        raw_response = await self._llm.chat(
            messages=messages,
            temperature=0.2,
            max_tokens=2048,
        )
        content = self._llm.extract_content(raw_response)

        parsed = safe_json_loads(content, default=None)
        if not isinstance(parsed, dict):
            objects = extract_json_objects(content)
            parsed = objects[0] if objects else {}

        return _dict_to_page_analysis(parsed)


# ---------------------------------------------------------------------------
# CriticAgent
# ---------------------------------------------------------------------------

class CriticAgent:
    """Verifies whether a task is complete and whether the agent is on track."""

    def __init__(self, llm: KimiClient) -> None:
        self._llm = llm

    async def verify(
        self,
        task: str,
        state: BrowserState,
        history: list[Step],
    ) -> VerificationResult:
        """Ask the LLM to judge task completion and progress."""
        history_text = _format_history(history)
        prompt = CRITIC_PROMPT.format(
            task=task,
            history=history_text,
            url=state.url,
            title=state.title,
            distilled_dom=_truncate_dom(state.distilled_dom),
        )

        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": "You are a verification specialist. Respond with JSON only.",
            },
            {"role": "user", "content": prompt},
        ]

        raw_response = await self._llm.chat(
            messages=messages,
            temperature=0.2,
            max_tokens=2048,
        )
        content = self._llm.extract_content(raw_response)

        parsed = safe_json_loads(content, default=None)
        if not isinstance(parsed, dict):
            objects = extract_json_objects(content)
            parsed = objects[0] if objects else {}

        return _dict_to_verification_result(parsed)


# ---------------------------------------------------------------------------
# SummarizerAgent
# ---------------------------------------------------------------------------

class SummarizerAgent:
    """Compresses a long action history into a concise plain-text summary."""

    def __init__(self, llm: KimiClient) -> None:
        self._llm = llm

    async def summarize(self, history: list[Step], task: str = "") -> str:
        """Produce a compact summary of *history* suitable for context trimming."""
        messages_text = _format_history(history)
        max_tokens_hint = 500  # target summary length

        prompt = SUMMARIZER_PROMPT.format(
            max_tokens=max_tokens_hint,
            task=task,
            messages=messages_text,
        )

        response = await self._llm.chat(
            messages=[
                {
                    "role": "system",
                    "content": "You are a conversation summarizer. Be concise.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=1024,
        )
        summary = self._llm.extract_content(response).strip()
        logger.info("SummarizerAgent produced %d-char summary", len(summary))
        return summary


# ---------------------------------------------------------------------------
# SubAgentOrchestrator
# ---------------------------------------------------------------------------

class SubAgentOrchestrator:
    """Thin delegation layer that routes requests to specialised sub-agents.

    Each sub-agent is instantiated lazily and shares the same :class:`KimiClient`.
    Every public method retries up to ``_MAX_LLM_RETRIES`` times when the LLM
    response cannot be parsed into the expected schema.
    """

    def __init__(self, llm: KimiClient) -> None:
        self._llm = llm
        self._planner: PlannerAgent | None = None
        self._explorer: ExplorerAgent | None = None
        self._critic: CriticAgent | None = None
        self._summarizer: SummarizerAgent | None = None

    # -- lazy initialisers --------------------------------------------------

    def _get_planner(self) -> PlannerAgent:
        if self._planner is None:
            self._planner = PlannerAgent(self._llm)
        return self._planner

    def _get_explorer(self) -> ExplorerAgent:
        if self._explorer is None:
            self._explorer = ExplorerAgent(self._llm)
        return self._explorer

    def _get_critic(self) -> CriticAgent:
        if self._critic is None:
            self._critic = CriticAgent(self._llm)
        return self._critic

    def _get_summarizer(self) -> SummarizerAgent:
        if self._summarizer is None:
            self._summarizer = SummarizerAgent(self._llm)
        return self._summarizer

    # -- public API --------------------------------------------------------

    @retry_with_backoff_async(
        max_retries=_MAX_LLM_RETRIES,
        backoff_seconds=1.0,
        exceptions=(ValueError, json.JSONDecodeError, KeyError),
    )
    async def plan_task(self, task: str, current_state: BrowserState) -> list[Step]:
        """Produce an ordered list of planned steps for *task*."""
        return await self._get_planner().plan(task, current_state)

    @retry_with_backoff_async(
        max_retries=_MAX_LLM_RETRIES,
        backoff_seconds=1.0,
        exceptions=(ValueError, json.JSONDecodeError, KeyError),
    )
    async def explore_page(self, state: BrowserState) -> PageAnalysis:
        """Analyse *state* and return structured page metadata."""
        return await self._get_explorer().explore(state)

    @retry_with_backoff_async(
        max_retries=_MAX_LLM_RETRIES,
        backoff_seconds=1.0,
        exceptions=(ValueError, json.JSONDecodeError, KeyError),
    )
    async def verify_task(
        self,
        task: str,
        state: BrowserState,
        history: list[Step],
    ) -> VerificationResult:
        """Verify whether *task* is complete given *history* and *state*."""
        return await self._get_critic().verify(task, state, history)

    @retry_with_backoff_async(
        max_retries=_MAX_LLM_RETRIES,
        backoff_seconds=1.0,
        exceptions=(ValueError, json.JSONDecodeError, KeyError),
    )
    async def summarize_history(self, history: list[Step]) -> str:
        """Return a concise summary of *history*."""
        # Try to infer the task from the first step's thought, otherwise leave blank
        task = history[0].thought if history else ""
        return await self._get_summarizer().summarize(history, task=task)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _truncate_dom(dom: str, max_chars: int = 4000) -> str:
    """Truncate distilled DOM to keep prompt sizes reasonable."""
    if len(dom) <= max_chars:
        return dom
    return dom[:max_chars] + f"\n... ({len(dom) - max_chars} chars truncated)"


def _format_history(history: list[Step]) -> str:
    """Convert a list of steps into a plain-text summary for prompt injection."""
    lines: list[str] = []
    for step in history:
        lines.append(f"Step {step.number}: {step.action.name}({step.action.arguments})")
        lines.append(f"  Thought: {step.thought}")
        lines.append(f"  Result: {step.result}")
        lines.append("")
    return "\n".join(lines)


def _dict_to_page_analysis(data: dict[str, Any]) -> PageAnalysis:
    """Best-effort conversion of a parsed dict into a :class:`PageAnalysis`."""
    page_type = data.get("page_type", "unknown")
    available_actions = data.get("available_actions", [])
    key_elements = data.get("key_elements", [])
    navigation_options = data.get("navigation_options", [])

    # Ensure lists
    if not isinstance(available_actions, list):
        available_actions = []
    if not isinstance(key_elements, list):
        key_elements = []
    if not isinstance(navigation_options, list):
        navigation_options = []

    return PageAnalysis(
        page_type=page_type,
        available_actions=available_actions,
        key_elements=key_elements,
        navigation_options=navigation_options,
    )


def _dict_to_verification_result(data: dict[str, Any]) -> VerificationResult:
    """Best-effort conversion of a parsed dict into a :class:`VerificationResult`."""
    is_complete = bool(data.get("is_complete", False))
    is_on_track = bool(data.get("is_on_track", True))
    issues = data.get("issues", [])
    suggestions = data.get("suggestions", [])
    confidence = float(data.get("confidence", 0.5))

    if not isinstance(issues, list):
        issues = []
    if not isinstance(suggestions, list):
        suggestions = []

    # Clamp confidence
    confidence = max(0.0, min(1.0, confidence))

    return VerificationResult(
        is_complete=is_complete,
        is_on_track=is_on_track,
        issues=issues,
        suggestions=suggestions,
        confidence=confidence,
    )
