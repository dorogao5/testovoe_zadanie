import json

from ai_browser_agent.agent.context import ContextManager
from ai_browser_agent.agent.models import ActionRecord, AgentState
from ai_browser_agent.browser.actions import (
    BrowserState,
    ElementRef,
    PageStats,
    ScrollState,
    SnapshotMode,
)


def test_context_preserves_task_after_compaction() -> None:
    state = AgentState(task="Do the requested browser task", run_id="r1")
    for step in range(40):
        state.add_action(
            ActionRecord(
                step=step,
                tool="observe",
                args={"mode": "visible"},
                ok=True,
                summary="Observed a page with many details " * 20,
            ),
            keep=100,
        )
    manager = ContextManager(budget_tokens=100, recent_steps_limit=5)
    messages = manager.build_messages(state)

    payload = messages[0]["content"]
    assert "trusted_user_task" in payload
    assert "Do the requested browser task" in payload
    assert len(state.recent_actions) <= 5
    assert state.memory


def test_context_attaches_latest_screenshot_path() -> None:
    state = AgentState(task="Use screenshot if needed", run_id="r1")
    manager = ContextManager()

    messages = manager.build_messages(
        state,
        {
            "ok": True,
            "data": {
                "path": "/tmp/browser-shot.png",
                "annotated": True,
            },
        },
    )

    assert messages[0]["images"] == ["/tmp/browser-shot.png"]


def test_context_compacts_large_observation_near_budget() -> None:
    state = AgentState(task="Find one matching result", run_id="r1")
    state.last_observation = BrowserState(
        url="https://mail.google.com/mail/u/0/#inbox",
        title="Inbox",
        mode=SnapshotMode.visible,
        viewport={"width": 1280, "height": 900},
        scroll=ScrollState(),
        stats=PageStats(text_length=100000),
        elements=[
            ElementRef(
                ref=f"e{index}",
                tag="div",
                role="link",
                name="Very long repeated email sender and subject " * 8,
                text="Very long repeated email preview " * 12,
                frame_url="https://mail.google.com/mail/u/0/#inbox",
                signature_hash=str(index),
            )
            for index in range(100)
        ],
        text_chunks=["Large page text " * 80 for _ in range(30)],
        fingerprint="fp",
    )
    manager = ContextManager(budget_tokens=800, recent_steps_limit=4)

    messages = manager.build_messages(state)
    payload = json.loads(messages[0]["content"])
    observation = payload["untrusted_page_content"]

    assert payload["context_budget_note"]
    assert len(observation["elements"]) <= 12
    assert len(observation["text_chunks"]) <= 3


def test_element_model_facing_dict_omits_default_noise() -> None:
    element = ElementRef(
        ref="e1",
        tag="div",
        role="button",
        name="Delete",
        frame_url="https://example.test",
        visible=True,
        in_viewport=True,
        enabled=True,
        signature_hash="sig",
    )

    data = element.model_facing_dict()

    assert "frame_url" not in data
    assert "visible" not in data
    assert "enabled" not in data
    assert data["ref"] == "e1"


def test_context_includes_historical_target_trace() -> None:
    state = AgentState(task="Add BBQ burger", run_id="r1")
    state.add_action(
        ActionRecord(
            step=4,
            tool="click",
            args={"ref": "e3", "intent": "Add BBQ burger to cart"},
            ok=True,
            summary="Clicked e3",
            target={
                "ref": "e3",
                "name": "Add to cart",
                "role": "button",
                "parent_chain": ["article:BBQ burger", "section"],
            },
        )
    )

    payload = json.loads(ContextManager().build_messages(state)[0]["content"])
    trace = " ".join(payload["trusted_action_trace"])

    assert "target_at_action='Add to cart'" in trace
    assert "historical_ref='e3'" in trace
    assert "article:BBQ burger" in trace
