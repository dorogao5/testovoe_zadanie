import pytest
from pydantic import ValidationError

from ai_browser_agent.agent.tools import TOOL_SCHEMAS, ToolDispatcher
from ai_browser_agent.agent.models import ActionRecord, AgentState
from ai_browser_agent.browser.actions import ElementRef
from ai_browser_agent.browser.controller import BrowserController
from ai_browser_agent.llm.base import ToolCall
from ai_browser_agent.safety.classifier import SecurityLayer


def test_side_effecting_tool_requires_intent() -> None:
    with pytest.raises(ValidationError):
        TOOL_SCHEMAS["click"].model_validate({"ref": "e1"})


def test_tool_definitions_are_json_schema() -> None:
    dispatcher = ToolDispatcher(
        browser=BrowserController(),
        safety=SecurityLayer(),
        ask_user=lambda prompt: "deny",
    )

    definitions = dispatcher.definitions()

    assert {definition.name for definition in definitions} >= {"observe", "click", "done"}
    assert definitions[0].input_schema["type"] == "object"


@pytest.mark.asyncio
async def test_repeated_destructive_action_blocked_for_singular_task() -> None:
    dispatcher = ToolDispatcher(
        browser=BrowserController(),
        safety=SecurityLayer(),
        ask_user=lambda prompt: "approve",
    )
    state = AgentState(task="Delete the latest email from Cerebral Valley", run_id="test")
    state.add_action(
        ActionRecord(
            step=1,
            tool="click",
            args={"ref": "e1", "intent": "Delete the selected email from Cerebral Valley"},
            ok=True,
            summary="Clicked delete",
        )
    )

    result = await dispatcher.execute(
        ToolCall(
            id="second-delete",
            name="click",
            args={"ref": "e2", "intent": "Delete another selected email from Cerebral Valley"},
        ),
        state,
    )

    assert not result.ok
    assert "repeated destructive action" in result.summary
    assert result.data["retryable"] is False


@pytest.mark.asyncio
async def test_ask_user_secret_request_becomes_handoff() -> None:
    prompts: list[str] = []
    dispatcher = ToolDispatcher(
        browser=BrowserController(),
        safety=SecurityLayer(),
        ask_user=lambda prompt: prompts.append(prompt) or "",
    )
    state = AgentState(task="Log in", run_id="test")

    result = await dispatcher.execute(
        ToolCall(
            id="ask-password",
            name="ask_user",
            args={"question": "What is your password?", "reason": "Need password to sign in"},
        ),
        state,
    )

    assert result.ok
    assert result.data["handoff"] is True
    assert result.data["answer"] == "[redacted-sensitive-handoff]"
    assert "Do not paste passwords" in prompts[0]


@pytest.mark.asyncio
async def test_done_normalizes_string_remaining_risks() -> None:
    dispatcher = ToolDispatcher(
        browser=BrowserController(),
        safety=SecurityLayer(),
        ask_user=lambda prompt: "",
    )
    state = AgentState(task="Finish", run_id="test")

    result = await dispatcher.execute(
        ToolCall(
            id="done",
            name="done",
            args={
                "success": True,
                "summary": "Complete",
                "evidence": "Verified on page",
                "remaining_risks": "None. Task completed successfully.",
            },
        ),
        state,
    )

    assert result.ok
    assert result.final is not None
    assert result.final.evidence == ["Verified on page"]
    assert result.final.remaining_risks == []


@pytest.mark.asyncio
async def test_extract_accepts_schema_as_json_string(monkeypatch) -> None:
    dispatcher = ToolDispatcher(
        browser=BrowserController(),
        safety=SecurityLayer(),
        ask_user=lambda prompt: "",
    )
    state = AgentState(task="Extract data", run_id="test")

    async def fake_extract(query, scope, schema, state):
        assert schema == {"type": "object", "properties": {"title": {"type": "string"}}}
        from ai_browser_agent.browser.actions import ExtractResult

        return ExtractResult(query=query, scope=scope, content="ok")

    monkeypatch.setattr(dispatcher, "_extract", fake_extract)

    result = await dispatcher.execute(
        ToolCall(
            id="extract",
            name="extract",
            args={
                "query": "title",
                "schema": '{"type":"object","properties":{"title":{"type":"string"}}}',
            },
        ),
        state,
    )

    assert result.ok


@pytest.mark.asyncio
async def test_extract_ignores_non_dict_schema(monkeypatch) -> None:
    dispatcher = ToolDispatcher(
        browser=BrowserController(),
        safety=SecurityLayer(),
        ask_user=lambda prompt: "",
    )
    state = AgentState(task="Extract data", run_id="test")

    async def fake_extract(query, scope, schema, state):
        assert schema is None
        from ai_browser_agent.browser.actions import ExtractResult

        return ExtractResult(query=query, scope=scope, content="ok")

    monkeypatch.setattr(dispatcher, "_extract", fake_extract)

    result = await dispatcher.execute(
        ToolCall(
            id="extract",
            name="extract",
            args={"query": "title", "schema": [{"title": "Job Title", "path": ["text", 0]}]},
        ),
        state,
    )

    assert result.ok

