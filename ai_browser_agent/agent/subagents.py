from __future__ import annotations

from ai_browser_agent.agent.models import PlanItem, PlanStatus


class PlannerAgent:
    def initial_plan(self, task: str) -> list[PlanItem]:
        return [
            PlanItem(text="Understand the requested outcome and constraints.", status=PlanStatus.done),
            PlanItem(text="Observe the current browser state or navigate as needed.", status=PlanStatus.current),
            PlanItem(text="Inspect relevant page information with compact observations/extractions."),
            PlanItem(text="Perform low-risk steps toward the task using current refs."),
            PlanItem(text="Pause for confirmation before destructive, external, or payment-like actions."),
            PlanItem(text="Verify the result and report evidence, gaps, and remaining risks."),
        ]


class ExplorerAgent:
    def should_resnapshot(self, failed_count: int) -> bool:
        return failed_count > 0


class ExecutorAgent:
    side_effect_tools = {"navigate", "click", "type_text", "press_key", "scroll", "select_option", "wait"}
    progress_sensitive_tools = {"navigate", "click", "press_key", "select_option"}

    def should_observe_after(self, tool_name: str) -> bool:
        return tool_name in self.side_effect_tools

    def should_check_progress(self, tool_name: str) -> bool:
        return tool_name in self.progress_sensitive_tools


class ExtractorAgent:
    def should_use_extract(self, task_or_query: str, *, visible_text_chars: int) -> bool:
        query = task_or_query.lower()
        long_reading_task = any(
            term in query
            for term in ("summarize", "extract", "compare", "read", "analyze", "summarise")
        )
        return long_reading_task or visible_text_chars > 5000


class CriticAgent:
    def done_requires_strong_verification(self, success: bool) -> bool:
        return success


class SafetyReviewerAgent:
    def needs_review(self, risk: str) -> bool:
        return risk in {"high", "critical"}
