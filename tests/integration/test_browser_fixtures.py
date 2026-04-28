from __future__ import annotations

from http.server import ThreadingHTTPServer
from threading import Thread
from typing import Iterator

import pytest

from ai_browser_agent.agent.models import AgentState
from ai_browser_agent.agent.tools import ToolDispatcher
from ai_browser_agent.browser.actions import SnapshotMode
from ai_browser_agent.browser.controller import BrowserController
from ai_browser_agent.evals.fixtures.server import FixtureHandler
from ai_browser_agent.llm.base import ToolCall
from ai_browser_agent.safety.classifier import SecurityLayer


@pytest.fixture()
def fixture_base_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_snapshot_covers_iframe_and_prompt_injection(fixture_base_url: str, tmp_path) -> None:
    browser = BrowserController(artifacts_dir=tmp_path / "run")
    await browser.launch(tmp_path / "profile", headless=True)
    try:
        await browser.navigate(f"{fixture_base_url}/dynamic")
        dynamic = await browser.current_state(SnapshotMode.full_light)

        assert any(element.frame_index > 0 for element in dynamic.elements)
        assert any(element.ref.startswith("f1:") for element in dynamic.elements)

        await browser.navigate(f"{fixture_base_url}/inbox")
        inbox = await browser.current_state(SnapshotMode.visible)

        assert inbox.security_warnings
    finally:
        await browser.close()


@pytest.mark.asyncio
async def test_query_dom_uses_card_context_for_duplicate_buttons(fixture_base_url: str, tmp_path) -> None:
    browser = BrowserController(artifacts_dir=tmp_path / "run")
    await browser.launch(tmp_path / "profile", headless=True)
    try:
        await browser.navigate(f"{fixture_base_url}/delivery")
        await browser.current_state(SnapshotMode.visible)

        burger = browser.resolver.query("BBQ burger add to cart", limit=1).candidates[0]
        fries = browser.resolver.query("French fries add to cart", limit=1).candidates[0]
        spicy = browser.resolver.query("BBQ burger spicy add to cart", limit=1).candidates[0]

        assert burger.ref != fries.ref
        assert burger.ref != spicy.ref
        assert burger.element.role == "button"
        assert fries.element.role == "button"
        assert any("bbq" in part.lower() for part in burger.element.parent_chain)
        assert any("french fries" in part.lower() for part in fries.element.parent_chain)
    finally:
        await browser.close()


@pytest.mark.asyncio
async def test_query_dom_prefers_cart_container_over_add_buttons(fixture_base_url: str, tmp_path) -> None:
    browser = BrowserController(artifacts_dir=tmp_path / "run")
    await browser.launch(tmp_path / "profile", headless=True)
    try:
        await browser.navigate(f"{fixture_base_url}/delivery")
        await browser.current_state(SnapshotMode.visible)

        result = browser.resolver.query(
            "cart section showing items currently added, especially item names and quantities",
            limit=3,
        )

        assert result.candidates
        assert result.candidates[0].element.tag in {"aside", "section"}
        assert result.candidates[0].element.name == "Cart"
        assert result.candidates[0].element.name != "Add to cart"
    finally:
        await browser.close()


@pytest.mark.asyncio
async def test_extract_preserves_cart_item_lines(fixture_base_url: str, tmp_path) -> None:
    browser = BrowserController(artifacts_dir=tmp_path / "run")
    await browser.launch(tmp_path / "profile", headless=True)
    try:
        await browser.navigate(f"{fixture_base_url}/delivery")
        state = AgentState(task="Inspect cart", run_id="r1")
        state.last_observation = await browser.current_state(SnapshotMode.visible)
        burger_ref = browser.resolver.query("BBQ burger add to cart", limit=1).candidates[0].ref
        fries_ref = browser.resolver.query("French fries add to cart", limit=1).candidates[0].ref
        await browser.click(burger_ref)
        await browser.click(fries_ref)
        state.last_observation = await browser.current_state(SnapshotMode.visible)

        dispatcher = ToolDispatcher(
            browser=browser,
            safety=SecurityLayer(),
            ask_user=lambda prompt: "",
        )
        result = await dispatcher.execute(
            ToolCall(
                id="extract-cart",
                name="extract",
                args={
                    "query": "List all items currently in the cart with their quantities",
                    "scope": "visible",
                },
            ),
            state,
        )

        assert result.ok
        content = result.data["content"]
        assert "Items: BBQ burger, French fries" in content or "BBQ burger French fries" in content
        assert content.count("Add to cart") < 6
    finally:
        await browser.close()


@pytest.mark.asyncio
async def test_stale_ref_recovery_after_dom_rerender(tmp_path) -> None:
    browser = BrowserController(artifacts_dir=tmp_path / "run")
    await browser.launch(tmp_path / "profile", headless=True)
    try:
        page = browser._require_page()
        await page.set_content(
            """
            <main>
              <button onclick="document.body.dataset.clicked = 'old'">Save draft</button>
            </main>
            """
        )
        state = await browser.current_state(SnapshotMode.visible)
        ref = browser.resolver.query("save draft", limit=1).candidates[0].ref
        assert state.elements

        await page.set_content(
            """
            <main>
              <button onclick="document.body.dataset.clicked = 'new'">Save draft</button>
            </main>
            """
        )
        result = await browser.click(ref)

        assert result.ok
        assert await page.evaluate("document.body.dataset.clicked") == "new"
    finally:
        await browser.close()


@pytest.mark.asyncio
async def test_scroll_moves_nested_scrollable_panel(tmp_path) -> None:
    browser = BrowserController(artifacts_dir=tmp_path / "run", viewport={"width": 800, "height": 600})
    await browser.launch(tmp_path / "profile", headless=True)
    try:
        page = browser._require_page()
        await page.set_content(
            """
            <main>
              <section aria-label="Job results" style="height: 180px; overflow-y: auto; border: 1px solid black">
                <a href="#1">Python Developer 1</a>
                <div style="height: 120px"></div>
                <a href="#2">Python Developer 2</a>
                <div style="height: 120px"></div>
                <a href="#3">Python Developer 3</a>
                <div style="height: 120px"></div>
                <a href="#4">Python Developer 4</a>
              </section>
            </main>
            """
        )
        await browser.current_state(SnapshotMode.visible)

        result = await browser.scroll("down", amount=140)

        assert result.ok
        assert result.data["method"] in {"wheel", "dom"}
        assert await page.locator("section").evaluate("(el) => el.scrollTop") > 0
    finally:
        await browser.close()


@pytest.mark.asyncio
async def test_scroll_reports_no_movement_at_boundary(tmp_path) -> None:
    browser = BrowserController(artifacts_dir=tmp_path / "run", viewport={"width": 800, "height": 600})
    await browser.launch(tmp_path / "profile", headless=True)
    try:
        page = browser._require_page()
        await page.set_content("<main><p>No scrolling here</p></main>")

        result = await browser.scroll("down", amount=140)

        assert not result.ok
        assert result.error is not None
        assert result.error.error_class == "scroll_no_movement"
    finally:
        await browser.close()


def test_browser_controller_treats_extension_onboarding_pages_as_noise(tmp_path) -> None:
    browser = BrowserController(artifacts_dir=tmp_path / "run")

    assert browser._is_automation_noise_url("chrome-extension://abc/options.html")
    assert browser._is_automation_noise_url("https://getadblock.com/installed/")
    assert browser._is_automation_noise_url("https://example.com/ublock-origin/welcome")
    assert not browser._is_automation_noise_url("https://mail.google.com/mail/u/0/#inbox")
