from __future__ import annotations

import asyncio
import json
import re
import hashlib
from pathlib import Path
from typing import Any

from ai_browser_agent.agent.cascade import ModelCascade
from ai_browser_agent.agent.context import ContextManager
from ai_browser_agent.agent.models import (
    ActionRecord,
    AgentState,
    ArtifactRef,
    FailureRecord,
    FinalResult,
    ModelRole,
    PlanStatus,
)
from ai_browser_agent.agent.subagents import (
    CriticAgent,
    ExecutorAgent,
    ExplorerAgent,
    ExtractorAgent,
    PlannerAgent,
    SafetyReviewerAgent,
)
from ai_browser_agent.agent.tools import ToolDispatcher
from ai_browser_agent.config import AgentConfig
from ai_browser_agent.llm.base import LLMRequest, ToolCall
from ai_browser_agent.llm.rate_limiter import TokenRateLimiter
from ai_browser_agent.observability.logger import RunLogger
from ai_browser_agent.recovery.handler import ErrorHandler


class AgentCore:
    def __init__(
        self,
        *,
        config: AgentConfig,
        run_id: str,
        run_dir: Path,
        cascade: ModelCascade,
        tools: ToolDispatcher,
        logger: RunLogger,
        context: ContextManager | None = None,
    ) -> None:
        self.config = config
        self.run_id = run_id
        self.run_dir = run_dir
        self.cascade = cascade
        self.tools = tools
        self.logger = logger
        self.context = context or ContextManager(budget_tokens=config.context_budget_tokens)
        self.planner = PlannerAgent()
        self.explorer = ExplorerAgent()
        self.executor = ExecutorAgent()
        self.extractor = ExtractorAgent()
        self.critic = CriticAgent()
        self.safety_reviewer = SafetyReviewerAgent()
        self.recovery = ErrorHandler()
        self.token_limiter = TokenRateLimiter(
            limit_per_minute=config.llm_tpm_limit,
            safety_factor=config.llm_tpm_safety_factor,
        )

    async def run_task(self, task: str) -> FinalResult:
        state = AgentState(task=task, run_id=self.run_id)
        state.plan = self.planner.initial_plan(task)
        latest_tool_result: dict[str, Any] | None = None

        self.logger.event("run_started", step=0, task=task, run_dir=str(self.run_dir))
        await self._execute_and_record(
            ToolCall(id="bootstrap-observe", name="observe", args={"mode": "visible"}),
            state,
            model="internal",
        )

        while state.step < self.config.max_steps:
            repeated_failures = self._consecutive_failures(state)
            if repeated_failures >= self.config.max_consecutive_failures:
                final = FinalResult(
                    success=False,
                    summary="Stopped after repeated failures.",
                    evidence=[failure.message for failure in state.failures[-3:]],
                    remaining_risks=["The page flow needs human inspection or a stronger recovery path."],
                )
                await self._finish(state, final)
                return final

            role = self.cascade.select_role(step=state.step, repeated_failures=repeated_failures)
            messages = self.context.build_messages(state, latest_tool_result)
            self._append_control_message(state, messages, latest_tool_result)
            tool_definitions = self._available_tools_for_step(state.step, state)
            request = LLMRequest(
                system=self.context.system_prompt(),
                messages=messages,
                tools=tool_definitions,
                model=self.cascade.model_for(role),
                prompt_cache_key=self._prompt_cache_key(),
                safety_identifier=self._safety_identifier(),
                thinking=self._thinking_mode(role),
            )
            estimated_tokens = self.context.estimate_tokens(messages)
            throttled_tokens = self.token_limiter.estimate_request_tokens(
                estimated_tokens,
                request.max_tokens,
            )
            await self._throttle_model_request(
                state=state,
                role=role,
                model=request.model,
                estimated_tokens=estimated_tokens,
                reserved_tokens=throttled_tokens,
            )
            self.logger.event(
                "model_request",
                step=state.step,
                role=role.value,
                model=request.model,
                estimated_tokens=estimated_tokens,
                tpm_reserved_tokens=throttled_tokens if self.config.llm_tpm_limit > 0 else None,
                prompt_cache_key=request.prompt_cache_key,
                thinking=request.thinking,
            )
            try:
                response = await self.cascade.complete(request, role=role)
            except Exception as exc:
                final = FinalResult(
                    success=False,
                    summary=f"Stopped because the model provider failed: {_short_exception(exc)}",
                    evidence=[f"Last completed step: {state.step}"],
                    remaining_risks=[
                        "The browser task did not finish because the LLM API returned an error.",
                        "For Kimi insufficient_quota/rate-limit errors, check balance or lower token limits.",
                    ],
                )
                self.logger.event(
                    "model_error",
                    step=state.step,
                    role=role.value,
                    model=request.model,
                    error=_short_exception(exc),
                )
                await self._finish(state, final)
                return final
            self.logger.event(
                "model_response",
                step=state.step,
                role=role.value,
                model=request.model,
                content=response.content,
                tool_calls=[_redacted_tool_call(call) for call in response.tool_calls],
                usage=response.usage.model_dump(mode="json") if response.usage else None,
            )
            self._record_model_usage(response.usage, fallback_tokens=throttled_tokens)

            if not response.tool_calls:
                fallback_response = await self._fallback_to_strong_if_possible(
                    request,
                    state=state,
                    previous_role=role,
                    previous_content=response.content,
                )
                if fallback_response is not None:
                    response = fallback_response
                else:
                    final = FinalResult(
                        success=False,
                        summary=response.content or "Model returned no tool call.",
                        evidence=[],
                        remaining_risks=[
                            "No tool call was produced, so the run cannot continue autonomously."
                        ],
                    )
                    await self._finish(state, final)
                    return final

            if not response.tool_calls:
                final = FinalResult(
                    success=False,
                    summary=response.content or "Model returned no tool call.",
                    evidence=[],
                    remaining_risks=["No tool call was produced, so the run cannot continue autonomously."],
                )
                await self._finish(state, final)
                return final

            response.tool_calls = self._single_tool_call(response.tool_calls, state)

            for call in response.tool_calls:
                before_fingerprint = state.last_observation.fingerprint if state.last_observation else None
                result = await self._execute_and_record(call, state, model=request.model)
                latest_tool_result = result
                if result.get("stop"):
                    final_data = result.get("final")
                    if isinstance(final_data, dict):
                        final = FinalResult.model_validate(final_data)
                    else:
                        final = FinalResult(
                            success=False,
                            summary=result.get("summary", "Stopped."),
                            evidence=[],
                            remaining_risks=["Run stopped before completion."],
                        )
                    if self.critic.done_requires_strong_verification(final.success):
                        proposed_final = final
                        final = await self._verify_final_with_strong(state, proposed_final)
                        if proposed_final.success and not final.success:
                            latest_tool_result = self._rejected_completion_feedback(
                                state=state,
                                proposed=proposed_final,
                                verified=final,
                            )
                            continue
                    await self._finish(state, final)
                    return final

                if self.executor.should_observe_after(call.name):
                    observe_result = await self._execute_and_record(
                        ToolCall(id=f"auto-observe-{state.step}", name="observe", args={"mode": "visible"}),
                        state,
                        model="internal",
                    )
                    after_fingerprint = state.last_observation.fingerprint if state.last_observation else None
                    if (
                        self.executor.should_check_progress(call.name)
                        and result.get("ok")
                        and before_fingerprint
                        and after_fingerprint == before_fingerprint
                    ):
                        warning = {
                            "tool": call.name,
                            "args": call.args,
                            "fingerprint": after_fingerprint,
                            "suggested_recovery": "If no visible progress was expected, continue; otherwise re-query, scroll, or replan.",
                        }
                        self.logger.event("no_progress_warning", step=state.step, **warning)
                        observe_result.setdefault("data", {})
                        observe_result["data"]["no_progress_warning"] = warning
                    latest_tool_result = self._combine_auto_observe_result(
                        action_result=result,
                        observe_result=observe_result,
                        action_tool=call.name,
                        action_args=call.args,
                    )

        final = FinalResult(
            success=False,
            summary=f"Stopped after reaching max_steps={self.config.max_steps}.",
            evidence=[],
            remaining_risks=["Increase max steps or narrow the task."],
        )
        await self._finish(state, final)
        return final

    async def _execute_and_record(
        self,
        call: ToolCall,
        state: AgentState,
        *,
        model: str,
    ) -> dict[str, Any]:
        state.step += 1
        self._advance_plan(state)
        target_context = self._target_context(call.name, call.args)
        self.logger.event(
            "tool_call",
            step=state.step,
            model=model,
            tool=call.name,
            args=self._redact_tool_args(call.name, call.args),
            target=target_context,
        )
        result = await self.tools.execute(call, state)
        result_data = result.model_dump(mode="json")
        result_data["tool"] = call.name
        if target_context is not None:
            result_data.setdefault("data", {})
            result_data["data"]["action_target"] = target_context
        self.logger.event(
            "tool_result",
            step=state.step,
            tool=call.name,
            ok=result.ok,
            summary=result.summary,
            data=result.data,
        )
        fingerprint = state.last_observation.fingerprint if state.last_observation else None
        record = ActionRecord(
            step=state.step,
            tool=call.name,
            args=self._redact_tool_args(call.name, call.args),
            ok=result.ok,
            summary=result.summary,
            target=target_context,
            page_fingerprint=fingerprint,
        )
        state.add_action(record)
        self.context.remember_tool_result(state, record)
        if not result.ok:
            error_class = str(
                result.data.get("error_class")
                or result.data.get("error", {}).get("error_class")
                or "tool_error"
            )
            repeated = self._same_action_repeats(state, call)
            recovery = self.recovery.choose(error_class, repeated_failures=repeated)
            state.add_failure(
                FailureRecord(
                    step=state.step,
                    tool=call.name,
                    error_class=error_class,
                    message=result.summary,
                    recovery=recovery.instruction,
                )
            )
            result_data.setdefault("data", {})
            result_data["data"]["recovery"] = recovery.model_dump(mode="json")
            artifact = await self._capture_failure_screenshot(state, call)
            if artifact is not None:
                result_data["data"]["failure_screenshot"] = artifact.path.as_posix()
        self._write_state(state)
        return result_data

    async def _fallback_to_strong_if_possible(
        self,
        request: LLMRequest,
        *,
        state: AgentState,
        previous_role: ModelRole,
        previous_content: str,
    ) -> Any | None:
        if previous_role == ModelRole.strong:
            return None
        strong_model = self.cascade.model_for(ModelRole.strong)
        fallback_messages = [
            *request.messages,
            {
                "role": "user",
                "content": (
                    "Fallback escalation: the previous model did not return a tool call. "
                    "Return exactly one valid tool call from the available tools. "
                    "If the previous text is already a useful report or a request for user "
                    "confirmation before a destructive action, call done with success=false and "
                    "preserve the report/candidates in summary/evidence. Do not continue analysis. "
                    f"Previous text response: {previous_content}"
                ),
            },
        ]
        fallback_request = LLMRequest(
            system=request.system,
            messages=fallback_messages,
            tools=request.tools,
            model=strong_model,
            max_tokens=min(request.max_tokens, 512),
            temperature=request.temperature,
            prompt_cache_key=request.prompt_cache_key,
            safety_identifier=request.safety_identifier,
            thinking="disabled" if self.config.provider == "kimi" else self._thinking_mode(ModelRole.strong),
        )
        self.logger.event(
            "model_fallback",
            step=state.step,
            from_role=previous_role.value,
            to_role=ModelRole.strong.value,
            model=strong_model,
            reason="No tool call returned by weaker model.",
        )
        try:
            estimated_tokens = self.context.estimate_tokens(fallback_messages)
            throttled_tokens = self.token_limiter.estimate_request_tokens(
                estimated_tokens,
                fallback_request.max_tokens,
            )
            await self._throttle_model_request(
                state=state,
                role=ModelRole.strong,
                model=strong_model,
                estimated_tokens=estimated_tokens,
                reserved_tokens=throttled_tokens,
            )
            self.logger.event(
                "model_request",
                step=state.step,
                role=ModelRole.strong.value,
                model=strong_model,
                estimated_tokens=estimated_tokens,
                tpm_reserved_tokens=throttled_tokens if self.config.llm_tpm_limit > 0 else None,
                prompt_cache_key=fallback_request.prompt_cache_key,
                thinking=fallback_request.thinking,
                fallback=True,
            )
            response = await asyncio.wait_for(
                self.cascade.complete(fallback_request, role=ModelRole.strong),
                timeout=45,
            )
        except Exception as exc:
            self.logger.event(
                "model_error",
                step=state.step,
                role=ModelRole.strong.value,
                model=strong_model,
                error=_short_exception(exc),
            )
            return None
        self.logger.event(
            "model_response",
            step=state.step,
            role=ModelRole.strong.value,
            model=strong_model,
            content=response.content,
            tool_calls=[_redacted_tool_call(call) for call in response.tool_calls],
            usage=response.usage.model_dump(mode="json") if response.usage else None,
        )
        self._record_model_usage(response.usage, fallback_tokens=throttled_tokens)
        return response if response.tool_calls else None

    async def _throttle_model_request(
        self,
        *,
        state: AgentState,
        role: ModelRole,
        model: str,
        estimated_tokens: int,
        reserved_tokens: int,
    ) -> None:
        if self.config.llm_tpm_limit <= 0:
            return
        usage_before = self.token_limiter.current_usage()
        if usage_before + min(reserved_tokens, self.config.llm_tpm_limit) <= self.config.llm_tpm_limit:
            return
        planned_wait = self.token_limiter.required_wait_seconds(reserved_tokens)
        self.logger.event(
            "model_throttle",
            step=state.step,
            role=role.value,
            model=model,
            wait_seconds=round(planned_wait, 3),
            limit_per_minute=self.config.llm_tpm_limit,
            usage_before=usage_before,
            estimated_tokens=estimated_tokens,
            reserved_tokens=reserved_tokens,
        )
        await self.token_limiter.wait_for_capacity(reserved_tokens)

    def _record_model_usage(self, usage: Any, *, fallback_tokens: int) -> None:
        total = getattr(usage, "total_tokens", None) if usage is not None else None
        if total is None and usage is not None:
            input_tokens = getattr(usage, "input_tokens", None) or 0
            output_tokens = getattr(usage, "output_tokens", None) or 0
            total = input_tokens + output_tokens if input_tokens or output_tokens else None
        self.token_limiter.record_usage(total or fallback_tokens)

    def _available_tools_for_step(self, step: int, state: AgentState | None = None) -> list[Any]:
        definitions = self.tools.definitions()
        if not self.config.allow_ask_user:
            definitions = [definition for definition in definitions if definition.name != "ask_user"]
        if state is not None and self._should_pause_extract_tool(state):
            definitions = [definition for definition in definitions if definition.name != "extract"]
        if step >= self.config.max_steps - 1:
            return [definition for definition in definitions if definition.name == "done"]
        if (
            state is not None
            and state.recent_actions
            and state.recent_actions[-1].tool == "observe"
            and state.recent_actions[-1].ok
            and state.last_observation is not None
        ):
            return [definition for definition in definitions if definition.name != "observe"]
        return definitions

    def _should_pause_extract_tool(self, state: AgentState) -> bool:
        recent_extracts = [record for record in state.recent_actions[-8:] if record.tool == "extract"]
        if len(recent_extracts) < 3:
            return False
        poor_or_cached = [
            record
            for record in recent_extracts[-3:]
            if record.ok
            and (
                "Reused cached extraction" in record.summary
                or re.search(r"\bExtracted\s+(?:0|[1-9]\d?)\s+chars\b", record.summary)
                or re.search(r"\bExtracted\s+1[0-9]{2}\s+chars\b", record.summary)
            )
        ]
        return len(poor_or_cached) >= 2

    def _single_tool_call(self, tool_calls: list[ToolCall], state: AgentState) -> list[ToolCall]:
        if len(tool_calls) <= 1:
            return tool_calls
        self.logger.event(
            "multi_tool_call_truncated",
            step=state.step,
            requested_count=len(tool_calls),
            kept=_redacted_tool_call(tool_calls[0]),
            dropped=[_redacted_tool_call(call) for call in tool_calls[1:]],
            reason="Browser state can change after each action; execute one tool call per model turn.",
        )
        return tool_calls[:1]

    def _redact_tool_args(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        redacted = dict(args)
        if tool == "type_text" and "text" in redacted:
            text = str(redacted.get("text", ""))
            intent = str(redacted.get("intent", ""))
            ref = str(redacted.get("ref", ""))
            element = self.tools.browser.resolver.ref_map.get(ref)
            element_context = " ".join(
                str(part or "")
                for part in [
                    getattr(element, "input_type", None),
                    getattr(element, "name", None),
                    getattr(element, "aria_label", None),
                    getattr(element, "placeholder", None),
                ]
            )
            if _looks_secret_text(f"{intent} {element_context} {text}"):
                redacted["text"] = "[redacted-sensitive-text]"
        if tool == "ask_user" and _looks_secret_text(
            f"{redacted.get('question', '')} {redacted.get('reason', '')}"
        ):
            redacted["question"] = "[redacted-sensitive-question]"
            redacted["reason"] = "[redacted-sensitive-reason]"
        return redacted

    def _target_context(self, tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
        if tool not in {"click", "type_text", "select_option", "scroll"}:
            return None
        ref = args.get("ref")
        if not ref:
            return None
        element = self.tools.browser.resolver.ref_map.get(str(ref))
        if element is None:
            return {"ref": str(ref), "warning": "ref was not present in the current resolver map"}
        return element.model_facing_dict()

    def _append_control_message(
        self,
        state: AgentState,
        messages: list[dict[str, Any]],
        latest_tool_result: dict[str, Any] | None = None,
    ) -> None:
        notes: list[str] = []
        if state.step >= max(1, int(self.config.max_steps * 0.75)):
            remaining = max(0, self.config.max_steps - state.step)
            notes.append(
                f"Step budget warning: {state.step}/{self.config.max_steps} steps used, "
                f"{remaining} remaining. If the task is complete, call done. If it is blocked "
                "or only partially complete, call done with success=false and precise remaining risks."
            )
        if state.step >= self.config.max_steps - 1:
            notes.append(
                "Last-step constraint: the only available tool is done. Do not claim success unless "
                "the current browser state and observed evidence satisfy the trusted task."
            )
        loop_note = self._loop_control_note(state)
        if loop_note:
            notes.append(loop_note)
        destructive_note = self._singular_destructive_completion_note(state)
        if destructive_note:
            notes.append(destructive_note)
        extraction_note = self._extraction_completion_note(latest_tool_result)
        if extraction_note:
            notes.append(extraction_note)
        rejected_completion_note = self._rejected_completion_note(latest_tool_result)
        if rejected_completion_note:
            notes.append(rejected_completion_note)
        if notes:
            messages.append(
                {
                    "role": "user",
                    "content": json.dumps({"control_notes": notes}, ensure_ascii=False, indent=2),
                }
            )

    def _extraction_completion_note(self, latest_tool_result: dict[str, Any] | None) -> str | None:
        if not latest_tool_result or latest_tool_result.get("tool") != "extract":
            return None
        if not latest_tool_result.get("ok"):
            return None
        data = latest_tool_result.get("data")
        if not isinstance(data, dict):
            return None
        evidence = data.get("evidence")
        content = str(data.get("content") or "")
        cache_hit = bool(data.get("cache_hit"))
        uncertainty = str(data.get("uncertainty") or "")
        if not evidence and not content:
            return None
        if cache_hit or len(content.strip()) < 180 or uncertainty:
            return (
                "Extraction is not adding useful new information. Do not repeat extract with a "
                "near-identical query. Use the current observation elements/query_dom candidates and "
                "their sender/subject/snippet text directly, or take one grounded browser action if a "
                "specific missing field is essential. For email-list review, visible row snippets are "
                "acceptable evidence for spam candidates before any destructive confirmation."
            )
        return (
            "Extraction evidence is available from the latest tool result. If it satisfies the "
            "trusted task, call done now with that evidence. Do not repeat extract with a paraphrased "
            "query unless a concrete required field is still missing."
        )

    def _rejected_completion_note(self, latest_tool_result: dict[str, Any] | None) -> str | None:
        if not latest_tool_result or latest_tool_result.get("tool") != "completion_rejected":
            return None
        data = latest_tool_result.get("data")
        if not isinstance(data, dict):
            return None
        verified = data.get("verified_final")
        remaining = []
        summary = ""
        if isinstance(verified, dict):
            summary = str(verified.get("summary") or "")
            raw_remaining = verified.get("remaining_risks")
            if isinstance(raw_remaining, list):
                remaining = [str(item) for item in raw_remaining]
        missing = "; ".join([summary, *remaining]).strip("; ")
        return (
            "Strong verifier rejected the previous done(success=true), so the task is not complete. "
            f"Verifier feedback: {missing}. Continue with a concrete browser action that can satisfy "
            "the missing requirement. Do not call done again until the verifier's objection is resolved."
        )

    def _loop_control_note(self, state: AgentState) -> str | None:
        recent = state.recent_actions[-8:]
        if len(recent) < 4:
            return None
        non_observe = [record for record in recent if record.tool != "observe"]
        if len(non_observe) >= 3:
            signatures = [
                json.dumps({"tool": record.tool, "args": record.args}, sort_keys=True, default=str)
                for record in non_observe[-3:]
            ]
            if len(set(signatures)) == 1:
                return (
                    "Loop warning: the same action has repeated without resolving the task. "
                    "Do not repeat it. Re-observe, query a more specific element with surrounding context, "
                    "or call done if the current state already satisfies the task."
                )
        fingerprints = [record.page_fingerprint for record in recent if record.page_fingerprint]
        if len(fingerprints) >= 5 and len(set(fingerprints[-5:])) == 1:
            return (
                "Stagnation warning: recent observations show the same page state. "
                "Avoid exploratory repeats; choose a different grounded action, ask the user if blocked, "
                "or call done with accurate success/evidence."
            )
        return None

    def _singular_destructive_completion_note(self, state: AgentState) -> str | None:
        if _task_allows_multiple_destructive_actions(state.task):
            return None
        recent_successful_destructive = [
            record
            for record in state.recent_actions[-8:]
            if record.ok
            and record.tool in {"click", "press_key", "select_option"}
            and _looks_destructive_text(
                " ".join(
                    [
                        str(record.args.get("intent", "")),
                        str(record.args.get("element_name", "")),
                        str(record.args.get("element_text", "")),
                        record.summary,
                    ]
                )
            )
        ]
        if not recent_successful_destructive:
            return None
        latest = recent_successful_destructive[-1]
        return (
            f"Singular destructive-action guard: step {latest.step} already completed a "
            "destructive action for a task that does not ask for multiple items. Do not delete, "
            "archive, trash, send, submit, or otherwise mutate another item. If the page shows "
            "success or the target is gone, call done. If verification is needed, use only "
            "non-destructive observe/query/extract actions."
        )

    def _combine_auto_observe_result(
        self,
        *,
        action_result: dict[str, Any],
        observe_result: dict[str, Any],
        action_tool: str,
        action_args: dict[str, Any],
    ) -> dict[str, Any]:
        combined = dict(observe_result)
        combined["summary"] = (
            f"Auto-observed after {action_tool}. Previous action result is preserved: "
            f"{action_result.get('summary', '')}"
        )
        data = dict(combined.get("data") or {})
        data["previous_action"] = {
            "tool": action_tool,
            "args": action_args,
            "result": action_result,
        }
        combined["data"] = data
        return combined

    def _rejected_completion_feedback(
        self,
        *,
        state: AgentState,
        proposed: FinalResult,
        verified: FinalResult,
    ) -> dict[str, Any]:
        summary = (
            "Strong verification rejected the attempted successful completion. "
            "The agent must continue working instead of ending the run."
        )
        data = {
            "proposed_final": proposed.model_dump(mode="json"),
            "verified_final": verified.model_dump(mode="json"),
            "instruction": (
                "Do not call done again until the missing requirements are satisfied. Choose a "
                "browser action that can make progress toward the verifier's remaining risks, such "
                "as scrolling, querying the page, opening more results, navigating pagination, or "
                "extracting targeted evidence."
            ),
        }
        self.logger.event(
            "completion_rejected",
            step=state.step,
            summary=summary,
            proposed=proposed.model_dump(mode="json"),
            verified=verified.model_dump(mode="json"),
        )
        state.add_failure(
            FailureRecord(
                step=state.step,
                tool="done",
                error_class="completion_rejected_by_verifier",
                message=verified.summary,
                recovery=data["instruction"],
            )
        )
        self._write_state(state)
        return {
            "ok": False,
            "summary": summary,
            "data": data,
            "tool": "completion_rejected",
        }

    async def _verify_final_with_strong(self, state: AgentState, final: FinalResult) -> FinalResult:
        if self.config.provider == "fake":
            return final
        done_tools = [definition for definition in self.tools.definitions() if definition.name == "done"]
        request = LLMRequest(
            system=(
                "You are a strict browser-agent completion critic. Verify whether the proposed final "
                "answer is fully supported by the trusted task, current browser state, tool results, "
                "and recent actions. Return exactly one done tool call. Set success=false if any "
                "required part is missing, uncertain, or only inferred from memory."
            ),
            messages=[
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "trusted_user_task": state.task,
                            "proposed_final": final.model_dump(mode="json"),
                            "trusted_action_trace": _action_trace(state.recent_actions[-12:]),
                            "current_browser_state": _verification_browser_state(state),
                            "recent_actions": [
                                record.model_dump(mode="json") for record in state.recent_actions[-12:]
                            ],
                            "instruction": (
                                "Confirm or correct the final result. Use success=true only when the "
                                "observed state satisfies the user task. Refs are ephemeral across observations: "
                                "when evaluating a past action, trust trusted_action_trace and "
                                "recent_actions[].target as the element snapshot at action time. Current browser "
                                "refs are intentionally omitted from current_browser_state to avoid comparing a "
                                "historical ref with a different current element. Do not use site-specific assumptions."
                            ),
                        },
                        ensure_ascii=False,
                        indent=2,
                        default=str,
                    ),
                }
            ],
            tools=done_tools,
            model=self.config.strong_model,
            max_tokens=1024,
            prompt_cache_key=self._prompt_cache_key(),
            safety_identifier=self._safety_identifier(),
            thinking="disabled" if self.config.provider == "kimi" else self._thinking_mode(ModelRole.strong),
        )
        self.logger.event(
            "verification",
            step=state.step,
            model=self.config.strong_model,
            summary="Strong model verifies final success against current evidence.",
        )
        try:
            estimated_tokens = self.context.estimate_tokens(request.messages)
            throttled_tokens = self.token_limiter.estimate_request_tokens(
                estimated_tokens,
                request.max_tokens,
            )
            await self._throttle_model_request(
                state=state,
                role=ModelRole.strong,
                model=self.config.strong_model,
                estimated_tokens=estimated_tokens,
                reserved_tokens=throttled_tokens,
            )
            response = await asyncio.wait_for(
                self.cascade.complete(request, role=ModelRole.strong),
                timeout=self.config.final_verification_timeout_seconds,
            )
        except Exception as exc:
            self.logger.event(
                "model_error",
                step=state.step,
                role=ModelRole.strong.value,
                model=self.config.strong_model,
                error=_short_exception(exc),
            )
            if final.success:
                return FinalResult(
                    success=False,
                    summary=(
                        "Strong verification failed before approving the proposed success: "
                        f"{_short_exception(exc)}"
                    ),
                    evidence=final.evidence,
                    remaining_risks=[
                        "Successful completion was not verified. Continue the browser task and gather stronger evidence.",
                    ],
                )
            verified = final.model_copy(deep=True)
            verified.remaining_risks.append(
                f"Strong verification skipped because the model provider failed: {_short_exception(exc)}"
            )
            return verified
        self.logger.event(
            "verification_response",
            step=state.step,
            model=self.config.strong_model,
            content=response.content,
            tool_calls=[_redacted_tool_call(call) for call in response.tool_calls],
            usage=response.usage.model_dump(mode="json") if response.usage else None,
        )
        self._record_model_usage(response.usage, fallback_tokens=throttled_tokens)
        for call in response.tool_calls:
            if call.name == "done":
                result = await self.tools.execute(call, state)
                if result.final is not None:
                    return result.final
        if final.success:
            return FinalResult(
                success=False,
                summary=(
                    "Strong verification did not return an explicit done approval for the proposed success."
                ),
                evidence=final.evidence,
                remaining_risks=[
                    "Successful completion was not verified. Continue the browser task and gather stronger evidence.",
                ],
            )
        verified = final.model_copy(deep=True)
        verified.remaining_risks.append("Strong verification did not return a valid done tool call.")
        return verified

    async def _capture_failure_screenshot(
        self,
        state: AgentState,
        call: ToolCall,
    ) -> ArtifactRef | None:
        try:
            artifact = await self.tools.browser.screenshot(
                annotated=True,
                reason=f"Failure after {call.name}",
            )
        except Exception:
            return None
        ref = ArtifactRef(kind="failure_screenshot", path=artifact.path, note=f"step {state.step} {call.name}")
        state.artifacts.append(ref)
        self.logger.event(
            "artifact",
            step=state.step,
            kind=ref.kind,
            path=str(ref.path),
            note=ref.note,
        )
        return ref

    def _advance_plan(self, state: AgentState) -> None:
        for index, item in enumerate(state.plan):
            if item.status == PlanStatus.current and state.step > 1:
                item.status = PlanStatus.done
                if index + 1 < len(state.plan):
                    state.plan[index + 1].status = PlanStatus.current
                break

    def _same_action_repeats(self, state: AgentState, call: ToolCall) -> int:
        signature = json.dumps({"tool": call.name, "args": call.args}, sort_keys=True, default=str)
        repeats = 0
        for record in reversed(state.recent_actions):
            current = json.dumps({"tool": record.tool, "args": record.args}, sort_keys=True, default=str)
            if current == signature:
                repeats += 1
            else:
                break
        return repeats

    def _consecutive_failures(self, state: AgentState) -> int:
        count = 0
        for record in reversed(state.recent_actions):
            if record.ok:
                break
            count += 1
        return count

    def _prompt_cache_key(self) -> str | None:
        if self.config.provider != "kimi":
            return self.config.prompt_cache_key
        if self.config.prompt_cache_key:
            return self.config.prompt_cache_key
        return f"ai-browser-agent:{self.run_id}"

    def _safety_identifier(self) -> str | None:
        if self.config.safety_identifier:
            return self.config.safety_identifier
        if self.config.provider != "kimi":
            return None
        digest = hashlib.sha256(str(self.config.profile_dir.resolve()).encode("utf-8")).hexdigest()
        return f"profile:{digest[:24]}"

    def _thinking_mode(self, role: ModelRole) -> str | None:
        if self.config.provider != "kimi":
            return None
        mode = (
            self.config.kimi_strong_thinking
            if role == ModelRole.strong
            else self.config.kimi_thinking
        ).strip().lower()
        if mode in {"enabled", "disabled"}:
            return mode
        return "enabled" if role == ModelRole.strong else "disabled"

    async def _finish(self, state: AgentState, final: FinalResult) -> None:
        if state.plan:
            state.plan[-1].status = PlanStatus.done if final.success else state.plan[-1].status
        final.artifacts.append(str(self.run_dir))
        self.logger.event(
            "final",
            step=state.step,
            success=final.success,
            summary=final.summary,
            evidence=final.evidence,
            remaining_risks=final.remaining_risks,
            artifacts=final.artifacts,
        )
        self.logger.write_summary(_summary_markdown(state, final))
        self._write_state(state)

    def _write_state(self, state: AgentState) -> None:
        (self.run_dir / "state.json").write_text(
            state.model_dump_json(indent=2),
            encoding="utf-8",
        )


def _summary_markdown(state: AgentState, final: FinalResult) -> str:
    plan = "\n".join(f"- {item.render()}" for item in state.plan)
    evidence = "\n".join(f"- {item}" for item in final.evidence) or "- None"
    risks = "\n".join(f"- {item}" for item in final.remaining_risks) or "- None"
    return (
        f"# Run summary\n\n"
        f"Task: {state.task}\n\n"
        f"Success: {final.success}\n\n"
        f"## Summary\n\n{final.summary}\n\n"
        f"## Plan\n\n{plan}\n\n"
        f"## Evidence\n\n{evidence}\n\n"
        f"## Remaining risks\n\n{risks}\n"
    )


def _verification_browser_state(state: AgentState) -> dict[str, Any] | None:
    if state.last_observation is None:
        return None
    summary = state.last_observation.to_model_summary(max_elements=50, max_text_chunks=10)
    elements = summary.get("elements")
    if isinstance(elements, list):
        sanitized = []
        for element in elements:
            if not isinstance(element, dict):
                continue
            item = dict(element)
            item.pop("ref", None)
            sanitized.append(item)
        summary["elements"] = sanitized
        summary["refs_note"] = (
            "Current refs are omitted in verification. Use recent_actions[].target for historical actions."
        )
    return summary


def _looks_destructive_text(text: str) -> bool:
    return bool(
        re.search(
            r"\b(delete|remove|trash|archive|mark as spam|unsubscribe|destroy|wipe|удал|корзин|архив)\b",
            text.lower(),
            re.I,
        )
    )


def _task_allows_multiple_destructive_actions(task: str) -> bool:
    return bool(
        re.search(
            r"\b(all|every|multiple|several|bulk|many|each|все|всю|всех|кажд|несколько|много|массов)\b",
            task.lower(),
            re.I,
        )
    )


def _looks_secret_text(text: str) -> bool:
    return bool(
        re.search(
            r"\b(password|passcode|otp|2fa|mfa|verification code|recovery code|api key|secret|token|парол|код подтверж|одноразов|секрет|токен)\b",
            text.lower(),
            re.I,
        )
    )


def _action_trace(actions: list[ActionRecord]) -> list[str]:
    trace: list[str] = []
    for action in actions:
        target = action.target or {}
        target_bits: list[str] = []
        if target:
            label = target.get("name") or target.get("text") or target.get("aria_label") or target.get("placeholder")
            if label:
                target_bits.append(f"target_at_action={label!r}")
            if target.get("role"):
                target_bits.append(f"role={target['role']!r}")
            if target.get("tag"):
                target_bits.append(f"tag={target['tag']!r}")
            parents = target.get("parent_chain")
            if isinstance(parents, list) and parents:
                target_bits.append(f"parents={parents[:3]!r}")
            if target.get("ref"):
                target_bits.append(f"historical_ref={target['ref']!r}")
        intent = action.args.get("intent")
        intent_text = f" intent={intent!r}" if intent else ""
        target_text = " " + " ".join(target_bits) if target_bits else ""
        trace.append(
            f"step {action.step}: {action.tool}{intent_text} ok={action.ok} "
            f"summary={action.summary!r}{target_text}"
        )
    return trace


def _redacted_tool_call(call: ToolCall) -> dict[str, Any]:
    data = call.model_dump(mode="json")
    args = dict(data.get("args") or {})
    if call.name == "type_text" and _looks_secret_text(
        f"{args.get('intent', '')} {args.get('text', '')}"
    ):
        args["text"] = "[redacted-sensitive-text]"
    if call.name == "ask_user" and _looks_secret_text(
        f"{args.get('question', '')} {args.get('reason', '')}"
    ):
        args["question"] = "[redacted-sensitive-question]"
        args["reason"] = "[redacted-sensitive-reason]"
    data["args"] = args
    return data


def _short_exception(exc: Exception, *, limit: int = 500) -> str:
    text = f"{type(exc).__name__}: {exc}"
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]
