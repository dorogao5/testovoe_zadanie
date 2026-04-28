from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ai_browser_agent.agent.core import AgentCore
from ai_browser_agent.agent.models import ActionRecord, AgentState, FinalResult, ModelRole, ToolExecutionResult
from ai_browser_agent.agent.context import ContextManager
from ai_browser_agent.agent.cascade import ModelCascade
from ai_browser_agent.config import AgentConfig
from ai_browser_agent.llm.base import LLMRequest, LLMResponse, ToolCall, ToolDefinition
from ai_browser_agent.llm.rate_limiter import TokenRateLimiter


class _FailingClient:
    async def complete(self, request: LLMRequest):
        raise RuntimeError("Error code: 429 - insufficient_quota current quota billing")


class _CapturingClient:
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest):
        self.requests.append(request.model_copy(deep=True))
        return LLMResponse(
            tool_calls=[
                ToolCall(
                    id="done",
                    name="done",
                    args={
                        "success": False,
                        "summary": "Needs confirmation before destructive action.",
                        "evidence": [],
                        "remaining_risks": ["Awaiting confirmation."],
                    },
                )
            ]
        )


class _NoToolClient:
    async def complete(self, request: LLMRequest):
        return LLMResponse(content="I cannot approve this yet.", tool_calls=[])


class _Logger:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.summary = ""

    def event(self, event_type: str, **kwargs) -> None:
        self.events.append({"type": event_type, **kwargs})

    def write_summary(self, markdown: str) -> None:
        self.summary = markdown


class _Tools:
    browser = SimpleNamespace(resolver=SimpleNamespace(ref_map={}))

    def definitions(self):
        return [
            ToolDefinition(name="observe", description="", input_schema={}),
            ToolDefinition(name="done", description="", input_schema={}),
        ]

    async def execute(self, call: ToolCall, state: AgentState):
        return ToolExecutionResult(ok=True, summary=f"{call.name} ok")


@pytest.mark.asyncio
async def test_run_task_returns_final_result_on_model_provider_error(tmp_path) -> None:
    config = AgentConfig(provider="kimi", max_steps=5)
    logger = _Logger()
    core = AgentCore(
        config=config,
        run_id="r1",
        run_dir=tmp_path,
        cascade=ModelCascade(client=_FailingClient(), config=config),
        tools=_Tools(),
        logger=logger,
        context=ContextManager(),
    )

    final = await core.run_task("Open Gmail and delete one matching email")

    assert not final.success
    assert "model provider failed" in final.summary
    assert "insufficient_quota" in final.summary
    assert any(event["type"] == "model_error" for event in logger.events)


def test_last_step_exposes_only_done_tool() -> None:
    core = AgentCore.__new__(AgentCore)
    core.config = AgentConfig(max_steps=5)
    core.tools = SimpleNamespace(
        definitions=lambda: [
            ToolDefinition(name="observe", description="", input_schema={}),
            ToolDefinition(name="click", description="", input_schema={}),
            ToolDefinition(name="done", description="", input_schema={}),
        ]
    )

    names = {definition.name for definition in core._available_tools_for_step(4)}

    assert names == {"done"}


def test_ask_user_is_hidden_by_default_but_handoff_remains() -> None:
    core = AgentCore.__new__(AgentCore)
    core.config = AgentConfig(max_steps=50)
    core.tools = SimpleNamespace(
        definitions=lambda: [
            ToolDefinition(name="observe", description="", input_schema={}),
            ToolDefinition(name="ask_user", description="", input_schema={}),
            ToolDefinition(name="handoff_to_user", description="", input_schema={}),
            ToolDefinition(name="done", description="", input_schema={}),
        ]
    )

    names = {definition.name for definition in core._available_tools_for_step(1)}

    assert "ask_user" not in names
    assert "handoff_to_user" in names
    assert "done" in names


def test_ask_user_can_be_enabled_explicitly() -> None:
    core = AgentCore.__new__(AgentCore)
    core.config = AgentConfig(max_steps=50, allow_ask_user=True)
    core.tools = SimpleNamespace(
        definitions=lambda: [
            ToolDefinition(name="observe", description="", input_schema={}),
            ToolDefinition(name="ask_user", description="", input_schema={}),
            ToolDefinition(name="done", description="", input_schema={}),
        ]
    )

    names = {definition.name for definition in core._available_tools_for_step(1)}

    assert "ask_user" in names


def test_kimi_thinking_is_enabled_only_for_strong_role() -> None:
    core = AgentCore.__new__(AgentCore)
    core.config = AgentConfig(
        provider="kimi",
        kimi_thinking="disabled",
        kimi_strong_thinking="enabled",
    )

    assert core._thinking_mode(ModelRole.fast) == "disabled"
    assert core._thinking_mode(ModelRole.primary) == "disabled"
    assert core._thinking_mode(ModelRole.vision) == "disabled"
    assert core._thinking_mode(ModelRole.strong) == "enabled"


def test_extract_tool_is_temporarily_hidden_after_low_value_repeats() -> None:
    core = AgentCore.__new__(AgentCore)
    core.config = AgentConfig(max_steps=50)
    core.tools = SimpleNamespace(
        definitions=lambda: [
            ToolDefinition(name="extract", description="", input_schema={}),
            ToolDefinition(name="query_dom", description="", input_schema={}),
            ToolDefinition(name="click", description="", input_schema={}),
            ToolDefinition(name="done", description="", input_schema={}),
        ]
    )
    state = AgentState(task="Review latest emails", run_id="r1", step=12)
    for step in range(1, 4):
        state.add_action(
            ActionRecord(
                step=step,
                tool="extract",
                args={"query": f"emails {step}"},
                ok=True,
                summary="Extracted 122 chars for query 'emails'.",
            )
        )

    names = {definition.name for definition in core._available_tools_for_step(12, state)}

    assert "extract" not in names
    assert {"query_dom", "click", "done"} <= names


def test_rejected_successful_completion_becomes_loop_feedback(tmp_path) -> None:
    core = AgentCore.__new__(AgentCore)
    core.run_dir = tmp_path
    core.logger = _Logger()
    state = AgentState(task="Find 10 jobs", run_id="r1", step=9)
    proposed = FinalResult(success=True, summary="Found some jobs", evidence=["7 jobs"])
    verified = FinalResult(
        success=False,
        summary="Only 7 jobs are visible; task asked for 10.",
        remaining_risks=["Scroll or paginate to collect more jobs."],
    )

    feedback = core._rejected_completion_feedback(
        state=state,
        proposed=proposed,
        verified=verified,
    )
    note = core._rejected_completion_note(feedback)

    assert feedback["tool"] == "completion_rejected"
    assert feedback["ok"] is False
    assert state.failures[-1].error_class == "completion_rejected_by_verifier"
    assert "Continue with a concrete browser action" in note
    assert any(event["type"] == "completion_rejected" for event in core.logger.events)


@pytest.mark.asyncio
async def test_success_verification_without_tool_rejects_completion(tmp_path) -> None:
    config = AgentConfig(provider="kimi", max_steps=10)
    logger = _Logger()
    core = AgentCore(
        config=config,
        run_id="r1",
        run_dir=tmp_path,
        cascade=ModelCascade(client=_NoToolClient(), config=config),
        tools=_Tools(),
        logger=logger,
        context=ContextManager(),
    )
    core.token_limiter = TokenRateLimiter(limit_per_minute=0)
    state = AgentState(task="Find jobs", run_id="r1", step=5)
    proposed = FinalResult(success=True, summary="Found 7 jobs", evidence=["7 jobs"])

    verified = await core._verify_final_with_strong(state, proposed)

    assert not verified.success
    assert "did not return an explicit done approval" in verified.summary
    assert "Continue the browser task" in verified.remaining_risks[0]


@pytest.mark.asyncio
async def test_no_tool_fallback_for_kimi_is_fast_and_non_thinking(tmp_path) -> None:
    config = AgentConfig(provider="kimi", max_steps=5)
    client = _CapturingClient()
    logger = _Logger()
    core = AgentCore(
        config=config,
        run_id="r1",
        run_dir=tmp_path,
        cascade=ModelCascade(client=client, config=config),
        tools=_Tools(),
        logger=logger,
        context=ContextManager(),
    )
    core.token_limiter = TokenRateLimiter(limit_per_minute=0)
    request = LLMRequest(
        system="system",
        messages=[{"role": "user", "content": "state"}],
        tools=[ToolDefinition(name="done", description="", input_schema={})],
        model="kimi-k2.6",
        max_tokens=2048,
        prompt_cache_key="cache",
        thinking="disabled",
    )
    state = AgentState(task="Review email", run_id="r1", step=18)

    response = await core._fallback_to_strong_if_possible(
        request,
        state=state,
        previous_role=ModelRole.primary,
        previous_content="Запрашиваю подтверждение перед пометкой как спам.",
    )

    assert response is not None
    assert client.requests[0].thinking == "disabled"
    assert client.requests[0].max_tokens == 512
    assert any(event["type"] == "model_request" and event.get("fallback") for event in logger.events)


def test_loop_control_message_warns_without_completing_task() -> None:
    core = AgentCore.__new__(AgentCore)
    core.config = AgentConfig(max_steps=50)
    state = AgentState(task="Complete the browser task", run_id="r1", step=10)
    for step in range(1, 5):
        state.add_action(
            ActionRecord(
                step=step,
                tool="click",
                args={"ref": "e1", "intent": "continue", "button": "left"},
                ok=True,
                summary="Clicked e1",
                page_fingerprint="same",
            )
        )
    messages: list[dict] = []

    core._append_control_message(state, messages)

    assert messages
    payload = json.loads(messages[0]["content"])
    joined = " ".join(payload["control_notes"])
    assert "Loop warning" in joined
    assert "call done" in joined


def test_singular_destructive_completion_note_after_delete() -> None:
    core = AgentCore.__new__(AgentCore)
    core.config = AgentConfig(max_steps=50)
    state = AgentState(task="Delete the latest email from Cerebral Valley", run_id="r1", step=4)
    state.add_action(
        ActionRecord(
            step=3,
            tool="click",
            args={"ref": "e50", "intent": "Delete the selected email from Cerebral Valley"},
            ok=True,
            summary="Clicked delete",
            page_fingerprint="after-delete",
        )
    )
    messages: list[dict] = []

    core._append_control_message(state, messages)

    assert messages
    payload = json.loads(messages[0]["content"])
    joined = " ".join(payload["control_notes"])
    assert "Singular destructive-action guard" in joined
    assert "Do not delete" in joined
    assert "call done" in joined


def test_auto_observe_preserves_previous_action_result() -> None:
    core = AgentCore.__new__(AgentCore)

    combined = core._combine_auto_observe_result(
        action_result={"ok": True, "summary": "Clicked delete", "data": {"x": 1}},
        observe_result={"ok": True, "summary": "Observed inbox", "data": {"observation": {}}},
        action_tool="click",
        action_args={"ref": "e50", "intent": "Delete selected email"},
    )

    assert "Clicked delete" in combined["summary"]
    assert combined["data"]["previous_action"]["tool"] == "click"
    assert combined["data"]["previous_action"]["result"]["summary"] == "Clicked delete"


def test_multiple_tool_calls_are_truncated_to_one() -> None:
    events: list[dict] = []
    core = AgentCore.__new__(AgentCore)
    core.logger = SimpleNamespace(event=lambda event_type, **kwargs: events.append({"type": event_type, **kwargs}))
    state = AgentState(task="Do one browser step", run_id="r1", step=2)
    calls = [
        ToolCall(id="1", name="click", args={"ref": "e1", "intent": "open"}),
        ToolCall(id="2", name="click", args={"ref": "e2", "intent": "delete"}),
    ]

    kept = core._single_tool_call(calls, state)

    assert kept == calls[:1]
    assert events[0]["type"] == "multi_tool_call_truncated"
    assert events[0]["requested_count"] == 2


def test_sensitive_type_text_args_are_redacted_for_state() -> None:
    core = AgentCore.__new__(AgentCore)
    core.tools = SimpleNamespace(
        browser=SimpleNamespace(resolver=SimpleNamespace(ref_map={})),
    )

    args = core._redact_tool_args(
        "type_text",
        {"ref": "e1", "text": "secret-password-value", "intent": "type password"},
    )

    assert args["text"] == "[redacted-sensitive-text]"


def test_target_context_uses_element_snapshot_at_action_time() -> None:
    core = AgentCore.__new__(AgentCore)
    target = SimpleNamespace(model_facing_dict=lambda: {"ref": "e3", "name": "Add to cart"})
    core.tools = SimpleNamespace(
        browser=SimpleNamespace(resolver=SimpleNamespace(ref_map={"e3": target})),
    )

    context = core._target_context("click", {"ref": "e3", "intent": "Add BBQ burger"})

    assert context == {"ref": "e3", "name": "Add to cart"}
