"""AgentCore — the main observe-reason-act-verify loop for the AI Browser Agent.

This module implements the central control loop that drives browser automation.
It coordinates observations from :class:`BrowserController`, reasoning via the
LLM (:class:`KimiClient`), security checks, error recovery, and sub-agent
delegation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any

from browser_controller import BrowserController
from kimi_client import KimiClient, create_default_tools
from models import (
    Action,
    ActionResult,
    AgentDecision,
    AgentResult,
    BrowserState,
    ErrorType,
    RecoveryStrategy,
    SecurityDecision,
    Step,
    StepResult,
    ToolCall,
    VerificationResult,
)
from prompts import SYSTEM_PROMPT
from utils import truncate_string

logger = logging.getLogger("agent_core")

# ---------------------------------------------------------------------------
# Graceful fallback classes for modules that may be empty stubs
# ---------------------------------------------------------------------------

try:
    from context_manager import ContextManager as _CtxMgrReal
except Exception:  # pragma: no cover
    _CtxMgrReal = None  # type: ignore[misc, assignment]


try:
    from security import SecurityLayer as _SecReal
except Exception:  # pragma: no cover
    _SecReal = None  # type: ignore[misc, assignment]


try:
    from error_handler import ErrorHandler as _ErrReal
except Exception:  # pragma: no cover
    _ErrReal = None  # type: ignore[misc, assignment]


try:
    from sub_agents import SubAgentOrchestrator as _SubReal
except Exception:  # pragma: no cover
    _SubReal = None  # type: ignore[misc, assignment]


class _ContextManagerFallback:
    """Minimal stand-in when context_manager.py is not yet implemented."""

    def __init__(self, max_tokens: int = 6000, model: str = "kimi-latest") -> None:
        self._messages: list[dict[str, Any]] = []
        self.max_tokens = max_tokens
        self.model = model

    def add_message(self, role: str, content: str) -> None:
        self._messages.append({"role": role, "content": content})

    def add_tool_result(self, tool_call_id: str, role: str, content: str) -> None:
        self._messages.append(
            {"role": role, "tool_call_id": tool_call_id, "content": content}
        )

    def get_messages(self) -> list[dict[str, Any]]:
        return list(self._messages)

    def get_token_count(self) -> int:
        return 0

    def trim_history(self) -> None:
        pass

    def add_system_prompt(self, prompt: str) -> None:
        # Insert at front, replacing any existing system message
        self._messages = [m for m in self._messages if m.get("role") != "system"]
        self._messages.insert(0, {"role": "system", "content": prompt})

    def clear(self) -> None:
        self._messages.clear()

    def summarize_old_messages(self, messages: list[dict[str, Any]]) -> str:
        return "Summary not available."


class _SecurityLayerFallback:
    """Minimal stand-in when security.py is not yet implemented."""

    def __init__(self, auto_approve: list[str] | None = None) -> None:
        self.auto_approve = auto_approve or []

    def check_action(self, action_type: str, action_params: dict[str, Any]) -> SecurityDecision:
        return SecurityDecision(
            verdict=SecurityDecision.Verdict.ALLOW,
            risk_level="low",
            explanation="No security layer configured; defaulting to allow.",
        )

    def is_destructive(self, action_type: str, params: dict[str, Any]) -> bool:
        return False

    def get_risk_level(self, action_type: str, params: dict[str, Any]) -> str:
        return "low"


class _ErrorHandlerFallback:
    """Minimal stand-in when error_handler.py is not yet implemented."""

    def __init__(self, max_retries: int = 3) -> None:
        self.max_retries = max_retries

    async def execute_with_retry(
        self,
        action: Any,
        context: dict[str, Any],
        strategy: str = "simple",
    ) -> ActionResult:
        try:
            result = await action()
            return ActionResult(success=True, message=str(result) if result else "OK")
        except Exception as exc:
            return ActionResult(success=False, message=f"Error: {exc}")

    def classify_error(self, error: Exception) -> ErrorType:
        return ErrorType.UNKNOWN

    def suggest_recovery(self, error_type: ErrorType) -> RecoveryStrategy:
        return RecoveryStrategy(strategy="simple", description="Retry the action.")


class _SubAgentOrchestratorFallback:
    """Minimal stand-in when sub_agents.py is not yet implemented."""

    def __init__(self, llm: KimiClient) -> None:
        self._llm = llm

    async def plan_task(self, task: str, current_state: BrowserState) -> list[Step]:
        return []

    async def explore_page(self, state: BrowserState) -> Any:
        from models import PageAnalysis
        return PageAnalysis(page_type="unknown")

    async def verify_task(
        self, task: str, state: BrowserState, history: list[Step]
    ) -> VerificationResult:
        return VerificationResult(is_complete=False, is_on_track=True, confidence=0.5)

    async def summarize_history(self, history: list[Step]) -> str:
        return "Summary not available."


# Resolve real vs fallback types at import time
ContextManager = _CtxMgrReal or _ContextManagerFallback  # type: ignore[misc]
SecurityLayer = _SecReal or _SecurityLayerFallback  # type: ignore[misc]
ErrorHandler = _ErrReal or _ErrorHandlerFallback  # type: ignore[misc]
SubAgentOrchestrator = _SubReal or _SubAgentOrchestratorFallback  # type: ignore[misc]

# ---------------------------------------------------------------------------
# AgentCore
# ---------------------------------------------------------------------------

class AgentCore:
    """Main agent loop: observe → reason → act → verify.

    Parameters
    ----------
    browser:
        Playwright controller.
    llm:
        Kimi API client.
    context:
        Conversation history / token budget manager.
    security:
        Destructive-action gate.
    error_handler:
        Retry and recovery logic.
    sub_agents:
        Planner / Explorer / Critic / Summarizer delegation.
    max_steps:
        Hard ceiling on loop iterations for a single task.
    """

    def __init__(
        self,
        browser: BrowserController,
        llm: KimiClient,
        context: Any,
        security: Any,
        error_handler: Any,
        sub_agents: Any,
        max_steps: int = 50,
    ) -> None:
        self.browser = browser
        self.llm = llm
        self.context = context
        self.security = security
        self.error_handler = error_handler
        self.sub_agents = sub_agents
        self.max_steps = max_steps

        # Runtime state
        self._current_task: str = ""
        self._step_history: list[Step] = []
        self._last_actions: list[tuple[str, str]] = []  # (action_type, result_msg)
        self._current_step: int = 0
        self._done: bool = False
        self._final_result: str = ""
        self._last_failure_signature: str | None = None
        self._consecutive_failures: int = 0
        self._max_consecutive_failures: int = 4

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_task(self, task: str) -> AgentResult:
        """Execute *task* from start to finish (or until *max_steps*).

        The loop follows the OODA pattern:
        1. Observe browser state
        2. Add observation to context
        3. Reason (LLM call with tools)
        4. Security check
        5. Act (execute tool with error-handler retry)
        6. Add result to context
        7. Detect stuck / done conditions
        """
        start_time = time.time()
        self._current_task = task
        self._step_history.clear()
        self._last_actions.clear()
        self._current_step = 0
        self._done = False
        self._final_result = ""

        # Initialise context
        self.context.clear()
        system_prompt = SYSTEM_PROMPT.format(task=task)
        self.context.add_system_prompt(system_prompt)
        logger.info("AgentCore starting task: %s", truncate_string(task, 120))

        for step_num in range(1, self.max_steps + 1):
            self._current_step = step_num
            logger.info("--- Step %d/%d ---", step_num, self.max_steps)

            try:
                step_result = await self.step()
            except Exception as exc:
                logger.exception("Unhandled exception in step %d", step_num)
                self._step_history.append(
                    Step(
                        number=step_num,
                        thought="Unhandled exception occurred.",
                        action=ToolCall(id="exc", name="error", arguments={}),
                        result=f"Exception: {exc}",
                    )
                )
                break

            # If the agent declared done, finish immediately
            if self._done:
                break

            # Hard stop to avoid runaway token burn.
            token_counter = getattr(self.context, "get_token_count", None)
            max_tokens = getattr(self.context, "max_tokens", None)
            if callable(token_counter) and isinstance(max_tokens, int):
                current_tokens = token_counter()
                if current_tokens > int(max_tokens * 1.2):
                    self._final_result = (
                        "Stopped early due to context growth safety guard "
                        f"({current_tokens} tokens > {int(max_tokens * 1.2)})."
                    )
                    logger.error(self._final_result)
                    break

            # Stuck detection
            if self._is_stuck():
                logger.warning("Stuck detected at step %d", step_num)
                await self._handle_stuck()

            # Optional: periodic verification every 5 steps
            if step_num % 5 == 0 and step_num < self.max_steps:
                try:
                    current_state = await self._observe()
                    verification = await self._verify(current_state)
                    if verification.is_complete and not self._done:
                        self._done = True
                        self._final_result = (
                            f"Task verified complete by critic (confidence={verification.confidence})."
                        )
                        break
                except Exception:
                    logger.warning("Periodic verification failed, continuing…")

        total_time = time.time() - start_time
        success = self._done or (
            self._step_history
            and self._step_history[-1].action.name == "done"
        )

        if not self._final_result:
            self._final_result = (
                f"Task ended after {self._current_step} steps. "
                f"Success={success}."
            )

        return AgentResult(
            success=success,
            task=task,
            steps=list(self._step_history),
            final_answer=self._final_result,
            total_steps=self._current_step,
            total_time_seconds=total_time,
        )

    async def step(self) -> StepResult:
        """Run one full observe-reason-act cycle and return the result."""
        step_num = self._current_step

        # a. Observe
        state = await self._observe()

        # b. Add observation to context
        observation = self._format_observation(state)
        self.context.add_message("user", observation)
        logger.debug("Observation added (%d chars)", len(observation))

        # c. Reason
        decision = await self._reason(state)

        # d. Security check (before execution)
        sec = self.security.check_action(
            decision.action.type, decision.action.params
        )
        if sec.verdict == SecurityDecision.Verdict.BLOCK:
            action_result = ActionResult(
                success=False,
                message=f"Security blocked: {sec.explanation}",
            )
            self._add_step(step_num, decision, action_result)
            self.context.add_message(
                "user", f"Action blocked by security: {sec.explanation}"
            )
            return StepResult(
                step_number=step_num,
                state=state,
                decision=decision,
                action_result=action_result,
            )

        if sec.verdict == SecurityDecision.Verdict.ASK_USER:
            user_ok = await self._tool_ask_user(
                f"Security check ({sec.risk_level} risk): {sec.explanation}\n"
                f"Destructive keywords found: {sec.destructive_keywords_found}\n"
                f"Proceed with '{decision.action.type}'? (yes/no)"
            )
            if user_ok.lower() not in ("yes", "y", "true", "1", "ok"):
                action_result = ActionResult(
                    success=False,
                    message="User denied security prompt.",
                )
                self._add_step(step_num, decision, action_result)
                self.context.add_message("user", "User denied the security prompt.")
                return StepResult(
                    step_number=step_num,
                    state=state,
                    decision=decision,
                    action_result=action_result,
                )

        # e. Execute action
        action_result = await self._act(decision)

        # Guardrail: if the same failed action repeats several times, stop early
        # instead of burning tokens in a loop.
        failure_signature = self._build_failure_signature(decision.action, action_result)
        if action_result.success:
            self._last_failure_signature = None
            self._consecutive_failures = 0
        else:
            if failure_signature == self._last_failure_signature:
                self._consecutive_failures += 1
            else:
                self._last_failure_signature = failure_signature
                self._consecutive_failures = 1

        # f. Add result to context
        self.context.add_message(
            "user", f"Action result ({decision.action.type}): {action_result.message}"
        )

        # g. Track for stuck detection
        self._track_action(decision.action, action_result)

        # h. Record step
        self._add_step(step_num, decision, action_result)

        # i. Done check
        if decision.action.type == "done":
            self._done = True
            self._final_result = decision.action.params.get("result", "")
        elif (
            not action_result.success
            and self._consecutive_failures >= self._max_consecutive_failures
        ):
            self._done = True
            self._final_result = (
                "Stopped after repeated identical failures to avoid token burn. "
                f"Last action: {decision.action.type}. "
                f"Last error: {truncate_string(action_result.message, 240)}"
            )
            logger.error(self._final_result)

        return StepResult(
            step_number=step_num,
            state=state,
            decision=decision,
            action_result=action_result,
        )

    # ------------------------------------------------------------------
    # Internal phases
    # ------------------------------------------------------------------

    async def _observe(self) -> BrowserState:
        """Capture the current browser state."""
        url = await self.browser.get_current_url()
        title = await self.browser.get_page_title()
        distilled_dom = await self.browser.get_distilled_dom()
        # Screenshot is optional — only capture when not under heavy load
        screenshot: bytes | None = None
        try:
            screenshot = await self.browser.get_screenshot(full_page=False)
        except Exception as exc:
            logger.warning("Screenshot capture failed: %s", exc)

        return BrowserState(
            url=url,
            title=title,
            distilled_dom=distilled_dom,
            screenshot=screenshot,
            timestamp=datetime.utcnow(),
        )

    async def _reason(self, state: BrowserState) -> AgentDecision:
        """Call the LLM with current context + tools and parse the response."""
        messages = self.context.get_messages()

        # If the last message is not the observation, prepend a lightweight state reminder
        if not messages or messages[-1].get("role") != "user":
            messages.append(
                {"role": "user", "content": self._format_observation(state)}
            )

        # Ensure token budget is respected
        self.context.trim_history()
        messages = self.context.get_messages()

        tools = create_default_tools()
        raw_response = await self.llm.chat(
            messages=messages,
            tools=tools,
            temperature=0.3,
            max_tokens=4096,
        )

        content = self.llm.extract_content(raw_response)
        tool_calls = self.llm.parse_tool_calls(raw_response)

        if tool_calls:
            tc = tool_calls[0]  # Enforce single-tool policy
            action = Action(
                type=tc.name,
                params=tc.arguments,
                description=f"LLM called tool '{tc.name}'",
            )
            thought = content or f"Execute {tc.name} with {tc.arguments}"
            return AgentDecision(
                thought=thought,
                action=action,
                confidence=1.0,
                needs_verification=tc.name in ("click", "type_text", "navigate"),
            )

        # No tool calls — the model produced free-form text
        # Treat as a thought and emit a lightweight wait action so the loop continues
        logger.debug("No tool calls from LLM; treating response as thought.")
        return AgentDecision(
            thought=content or "(no reasoning provided)",
            action=Action(
                type="wait",
                params={"seconds": 1},
                description="No explicit tool call; waiting before next observation.",
            ),
            confidence=0.5,
            needs_verification=False,
        )

    async def _act(self, decision: AgentDecision) -> ActionResult:
        """Dispatch *decision.action* to the correct tool implementation."""
        action = decision.action
        tool_name = action.type
        params = action.params

        # Helper to wrap a coroutine with the error handler
        async def _run(coro: Any) -> ActionResult:
            async def _call_with_ignored_context(**_kwargs: Any) -> Any:
                return await coro()

            raw_result = await self.error_handler.execute_with_retry(
                action_func=_call_with_ignored_context,
                context={},
                strategy="simple",
            )
            if isinstance(raw_result, ActionResult):
                return raw_result

            # error_handler may return its own ActionResult model; normalise it
            # to the canonical models.ActionResult expected by StepResult.
            result_data = getattr(raw_result, "data", {}) or {}
            if not isinstance(result_data, dict):
                result_data = {"raw_data": result_data}
            return ActionResult(
                success=bool(getattr(raw_result, "success", False)),
                message=str(getattr(raw_result, "message", "")),
                data=result_data,
            )

        try:
            if tool_name == "navigate":
                return await _run(
                    lambda: self._tool_navigate(params.get("url", ""))
                )
            elif tool_name == "click":
                return await _run(
                    lambda: self._tool_click(
                        params.get("element_description", ""),
                        params.get("reason", ""),
                    )
                )
            elif tool_name == "type_text":
                return await _run(
                    lambda: self._tool_type_text(
                        params.get("element_description", ""),
                        params.get("text", ""),
                        params.get("submit_after", False),
                    )
                )
            elif tool_name == "scroll":
                return await _run(
                    lambda: self._tool_scroll(
                        params.get("direction", "down"),
                        params.get("amount", 500),
                    )
                )
            elif tool_name == "find_information":
                return await _run(
                    lambda: self._tool_find_information(params.get("query", ""))
                )
            elif tool_name == "ask_user":
                # User interaction bypasses the error handler — it's inherently interactive
                return ActionResult(
                    success=True,
                    message=await self._tool_ask_user(params.get("question", "")),
                )
            elif tool_name == "done":
                return ActionResult(
                    success=True,
                    message=await self._tool_done(params.get("result", "")),
                )
            elif tool_name == "wait":
                return ActionResult(
                    success=True,
                    message=await self._tool_wait(params.get("seconds", 1)),
                )
            elif tool_name == "press_key":
                return await _run(
                    lambda: self._tool_press_key(params.get("key", ""))
                )
            else:
                return ActionResult(
                    success=False,
                    message=f"Unknown tool: {tool_name}",
                )
        except Exception as exc:
            logger.exception("Tool execution failed: %s", tool_name)
            return ActionResult(
                success=False,
                message=f"Tool '{tool_name}' failed: {exc}",
            )

    async def _verify(self, state: BrowserState | None = None) -> VerificationResult:
        """Invoke the Critic sub-agent to verify progress."""
        if state is None:
            state = await self._observe()
        try:
            return await self.sub_agents.verify_task(
                self._current_task, state, list(self._step_history)
            )
        except Exception as exc:
            logger.warning("Verification failed: %s", exc)
            return VerificationResult(
                is_complete=False, is_on_track=True, confidence=0.0
            )

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    async def _tool_navigate(self, url: str) -> str:
        """Navigate the browser to *url*."""
        if not url:
            raise ValueError("URL is required for navigate")
        await self.browser.navigate(url)
        return f"Navigated to {url}"

    async def _tool_click(self, element_description: str, reason: str) -> str:
        """Resolve *element_description* and click the element."""
        if not element_description:
            raise ValueError("element_description is required for click")
        info = await self.browser.find_element(element_description)
        selector = info.get("selector")
        if not selector:
            raise RuntimeError(f"No selector resolved for: {element_description}")
        await self.browser.click(selector)
        return f"Clicked '{element_description}' ({selector}). Reason: {reason}"

    async def _tool_type_text(
        self,
        element_description: str,
        text: str,
        submit_after: bool = False,
    ) -> str:
        """Resolve *element_description* and type *text* into it."""
        if not element_description:
            raise ValueError("element_description is required for type_text")
        info = await self.browser.find_element(element_description)
        selector = info.get("selector")
        if not selector:
            raise RuntimeError(f"No selector resolved for: {element_description}")
        await self.browser.type_text(selector, text, clear_first=True)
        result = f"Typed into '{element_description}' ({selector})."
        if submit_after:
            await self.browser.press_key("Enter")
            result += " Submitted with Enter."
        return result

    async def _tool_scroll(self, direction: str, amount: int = 500) -> str:
        """Scroll the page."""
        await self.browser.scroll(direction=direction, amount=amount)
        return f"Scrolled {direction} by {amount}px"

    async def _tool_find_information(self, query: str) -> str:
        """Search for *query* in the distilled DOM of the current page."""
        dom = await self.browser.get_distilled_dom()
        query_lower = query.lower()
        lines = dom.splitlines()
        matches: list[str] = []
        for line in lines:
            if query_lower in line.lower():
                matches.append(line.strip())
        if matches:
            return f"Found {len(matches)} match(es):\n" + "\n".join(matches[:20])
        return f"No matches found for '{query}' on the current page."

    async def _tool_ask_user(self, question: str) -> str:
        """Pause execution and prompt the user on the CLI."""
        print(f"\n🤖 Agent asks: {question}")
        try:
            response = input("Your answer: ").strip()
        except (EOFError, KeyboardInterrupt):
            response = ""
        logger.info("User response: %s", truncate_string(response, 200))
        return response

    async def _tool_done(self, result: str) -> str:
        """Mark the task as complete."""
        self._done = True
        self._final_result = result
        logger.info("Agent declared task done: %s", truncate_string(result, 200))
        return f"Task complete: {result}"

    async def _tool_wait(self, seconds: int) -> str:
        """Wait for *seconds* seconds."""
        await asyncio.sleep(seconds)
        return f"Waited {seconds}s"

    async def _tool_press_key(self, key: str) -> str:
        """Press a special key."""
        if not key:
            raise ValueError("key is required for press_key")
        await self.browser.press_key(key)
        return f"Pressed key: {key}"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _format_observation(self, state: BrowserState) -> str:
        """Build a concise observation string for the context manager."""
        lines = [
            f"Current URL: {state.url}",
            f"Page Title: {state.title}",
            "--- Distilled DOM ---",
            state.distilled_dom[:3000],  # cap to keep token count reasonable
        ]
        if state.screenshot:
            lines.append(f"[Screenshot captured: {len(state.screenshot)} bytes]")
        return "\n".join(lines)

    def _track_action(self, action: Action, result: ActionResult) -> None:
        """Record the latest action for stuck detection."""
        normalized = result.message.strip().lower()[:160]
        self._last_actions.append((action.type, normalized))
        # Keep only the last 5 entries
        if len(self._last_actions) > 5:
            self._last_actions.pop(0)

    @staticmethod
    def _build_failure_signature(action: Action, result: ActionResult) -> str:
        """Build a stable signature for repeated-failure detection."""
        params_repr = json.dumps(action.params, sort_keys=True, ensure_ascii=False)
        message_repr = result.message.strip().lower()[:200]
        return f"{action.type}|{params_repr}|{message_repr}"

    def _is_stuck(self) -> bool:
        """Detect when the agent repeats the same failing action 3+ times."""
        if len(self._last_actions) < 3:
            return False
        # Check the last 3 entries
        recent = self._last_actions[-3:]
        first_type, first_msg = recent[0]
        return all(
            a_type == first_type and msg == first_msg
            for a_type, msg in recent[1:]
        )

    async def _handle_stuck(self) -> None:
        """Recover from a stuck state by exploring the page or asking the user."""
        logger.info("Attempting stuck recovery via ExplorerAgent…")
        try:
            state = await self._observe()
            analysis = await self.sub_agents.explore_page(state)
            summary = (
                f"Stuck recovery — page analysis:\n"
                f"  page_type: {analysis.page_type}\n"
                f"  available_actions: {analysis.available_actions}\n"
                f"  navigation_options: {analysis.navigation_options}\n"
                f"  key_elements: {analysis.key_elements[:5]}"
            )
            self.context.add_message("user", summary)
        except Exception as exc:
            logger.warning("ExplorerAgent failed during stuck recovery: %s", exc)
            self.context.add_message(
                "user",
                "The agent appears to be stuck. Consider providing guidance.",
            )

    def _add_step(self, step_num: int, decision: AgentDecision, result: ActionResult) -> None:
        """Append a :class:`Step` to the internal history."""
        tool_call = ToolCall(
            id=f"step_{step_num}",
            name=decision.action.type,
            arguments=decision.action.params,
        )
        step = Step(
            number=step_num,
            thought=decision.thought,
            action=tool_call,
            result=result.message,
        )
        self._step_history.append(step)
        logger.debug(
            "Recorded step %d: %s -> %s",
            step_num,
            decision.action.type,
            truncate_string(result.message, 80),
        )
