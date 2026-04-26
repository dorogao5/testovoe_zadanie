"""
BrowserController — Low-level browser automation via Playwright.

This module provides an async wrapper around Playwright that handles:
- Browser launch (persistent or ephemeral contexts)
- Navigation and page state queries
- Natural-language element resolution
- Click, type, scroll, and key-press actions
- DOM distillation (token-efficient extraction of interactive/semantic elements)
- Session state (cookies + localStorage) persistence
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

logger = logging.getLogger(__name__)


class BrowserController:
    """
    Async Playwright controller for an AI browser automation agent.

    Provides methods for navigating pages, resolving elements via natural-language
    descriptions, interacting with them, and extracting distilled DOM representations
    suitable for LLM consumption.
    """

    # Locator strategies for find_element (ordered by priority)
    _FIND_STRATEGIES = [
        "get_by_text_exact",
        "get_by_text_contains",
        "get_by_label",
        "get_by_placeholder",
        "get_by_title",
        "get_by_role_button",
        "get_by_role_link",
        "get_by_role_textbox",
        "css_selector",
        "js_fuzzy",
    ]

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def __init__(self) -> None:
        self._playwright: Any | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._user_data_dir: str | None = None
        self._headless: bool = False

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def launch(
        self,
        headless: bool = False,
        user_data_dir: str | None = None,
    ) -> None:
        """
        Launch a Chromium browser instance.

        Args:
            headless: Run browser without a visible window.
            user_data_dir: Path to a directory for persistent session data
                (cookies, localStorage, etc.). If None, an ephemeral context
                is created instead.

        Raises:
            PlaywrightError: If the browser fails to launch.
        """
        self._headless = headless
        self._user_data_dir = user_data_dir

        try:
            self._playwright = await async_playwright().start()
            pw = self._playwright

            if user_data_dir:
                logger.info(
                    "Launching persistent Chromium context (user_data_dir=%s)",
                    user_data_dir,
                )
                self._context = await pw.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    headless=headless,
                    args=[
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-default-apps",
                    ],
                )
                # Persistent context already has one page
                pages = self._context.pages
                self._page = pages[0] if pages else await self._context.new_page()
            else:
                logger.info("Launching ephemeral Chromium browser")
                self._browser = await pw.chromium.launch(
                    headless=headless,
                    args=[
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-default-apps",
                    ],
                )
                self._context = await self._browser.new_context(
                    viewport={"width": 1280, "height": 900},
                )
                self._page = await self._context.new_page()

        except Exception as exc:
            logger.error("Browser launch failed: %s", exc)
            await self._safe_cleanup()
            raise

    async def close(self) -> None:
        """
        Perform a clean shutdown of the browser, context, and Playwright.
        Safe to call multiple times.
        """
        await self._safe_cleanup()

    async def navigate(self, url: str) -> None:
        """
        Navigate the current page to ``url`` and wait for the ``load`` event.

        Args:
            url: The absolute URL to navigate to.

        Raises:
            PlaywrightError: If navigation fails.
        """
        page = self._require_page()
        logger.info("Navigating to %s", url)
        try:
            await page.goto(url, wait_until="load", timeout=30000)
        except PlaywrightTimeoutError as exc:
            logger.warning("Navigation timed out for %s: %s", url, exc)
            # Still proceed — partial load is often usable
        except PlaywrightError as exc:
            logger.error("Navigation failed for %s: %s", url, exc)
            raise

    async def get_current_url(self) -> str:
        """Return the current page URL."""
        page = self._require_page()
        return page.url

    async def get_page_title(self) -> str:
        """Return the current page title."""
        page = self._require_page()
        for attempt in range(3):
            try:
                return await page.title()
            except PlaywrightError as exc:
                message = str(exc).lower()
                is_nav_race = "execution context was destroyed" in message
                if is_nav_race and attempt < 2:
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                logger.warning("Failed to get page title: %s", exc)
                return "<title-unavailable>"
        return "<title-unavailable>"

    async def find_element(self, description: str) -> dict:
        """
        Resolve a natural-language element description to a concrete selector.

        Tries multiple strategies in order of preference:
            1. Exact text match (`get_by_text` exact)
            2. Partial text match (`get_by_text` with substring)
            3. ARIA label (`get_by_label`)
            4. Placeholder (`get_by_placeholder`)
            5. Title attribute (`get_by_title`)
            6. Role-based buttons, links, textboxes
            7. CSS selector (if description looks like one)
            8. JavaScript fuzzy search across visible elements

        Args:
            description: Natural language description of the element, e.g.
                "button with text 'Submit'", "Search input", "#search-box".

        Returns:
            A dict with keys:
                - ``selector`` (str): A Playwright locator string.
                - ``tag`` (str): The HTML tag name.
                - ``text`` (str): Visible text content (truncated).
                - ``aria_label`` (str | None): ARIA label if present.
                - ``placeholder`` (str | None): Placeholder if present.
                - ``href`` (str | None): Href for links.
                - ``input_type`` (str | None): Input type attribute.
                - ``role`` (str | None): ARIA role.
                - ``confidence`` (str): One of ``high``, ``medium``, ``low``.

        Raises:
            RuntimeError: If no matching element is found after exhausting all strategies.
        """
        page = self._require_page()
        description = description.strip()

        # Strategy helper: attempt a locator and verify it resolves to >=1 visible element
        async def _try_locator(
            locator: Locator,
            confidence: str,
        ) -> dict | None:
            try:
                count = await locator.count()
                if count == 0:
                    return None
                # Prefer the first visible element
                for i in range(min(count, 5)):
                    elm = locator.nth(i)
                    visible = await elm.is_visible()
                    if visible:
                        info = await self._extract_element_info(elm)
                        info["confidence"] = confidence
                        return info
                # Fallback: none visible, return first anyway with lower confidence
                elm = locator.first
                info = await self._extract_element_info(elm)
                info["confidence"] = "low"
                return info
            except PlaywrightError:
                return None

        # 1) Exact text match
        try:
            result = await _try_locator(
                page.get_by_text(description, exact=True), confidence="high"
            )
            if result:
                return result
        except PlaywrightError:
            pass

        # 2) Partial / substring text match
        try:
            result = await _try_locator(
                page.get_by_text(description, exact=False), confidence="high"
            )
            if result:
                return result
        except PlaywrightError:
            pass

        # 3) ARIA label
        try:
            result = await _try_locator(
                page.get_by_label(description, exact=False), confidence="high"
            )
            if result:
                return result
        except PlaywrightError:
            pass

        # 4) Placeholder
        try:
            result = await _try_locator(
                page.get_by_placeholder(description, exact=False), confidence="high"
            )
            if result:
                return result
        except PlaywrightError:
            pass

        # 5) Title attribute
        try:
            result = await _try_locator(
                page.get_by_title(description, exact=False), confidence="medium"
            )
            if result:
                return result
        except PlaywrightError:
            pass

        # 6) Role-based locators (button, link, textbox)
        role_attempts = [
            ("button", f"button named '{description}'"),
            ("link", f"link named '{description}'"),
            ("textbox", f"textbox named '{description}'"),
        ]
        for role, _ in role_attempts:
            try:
                result = await _try_locator(
                    page.get_by_role(role, name=description, exact=False),
                    confidence="medium",
                )
                if result:
                    return result
            except PlaywrightError:
                pass

        # 7) CSS selector — if the description looks like a selector
        if self._looks_like_css_selector(description):
            try:
                result = await _try_locator(
                    page.locator(description), confidence="high"
                )
                if result:
                    return result
            except PlaywrightError:
                pass

        # 8) JavaScript fuzzy search across all visible interactive elements
        js_result = await self._js_find_element(page, description)
        if js_result:
            return js_result

        raise RuntimeError(
            f"Could not find element matching description: '{description}'"
        )

    async def click(self, selector: str) -> None:
        """
        Click an element identified by ``selector``.

        Args:
            selector: Playwright locator string (e.g. ``text=Submit``).

        Raises:
            RuntimeError: If the element is not found or not clickable.
        """
        page = self._require_page()
        locator = page.locator(selector)

        try:
            # Wait for element to be attached & visible, then click
            await locator.wait_for(state="visible", timeout=10000)
            await locator.click(timeout=10000)
            logger.info("Clicked element: %s", selector)
        except PlaywrightTimeoutError as exc:
            raise RuntimeError(
                f"Element not clickable (timeout): {selector}"
            ) from exc
        except PlaywrightError as exc:
            raise RuntimeError(
                f"Failed to click element: {selector}"
            ) from exc

    async def type_text(
        self,
        selector: str,
        text: str,
        clear_first: bool = True,
    ) -> None:
        """
        Type ``text`` into an input/textarea element.

        Args:
            selector: Playwright locator string.
            text: The text to type.
            clear_first: If True, clear the field before typing.

        Raises:
            RuntimeError: If the field cannot be focused or typed into.
        """
        page = self._require_page()
        locator = page.locator(selector).first

        try:
            await locator.wait_for(state="visible", timeout=10000)
            tag_name = await locator.evaluate("el => el.tagName.toLowerCase()")

            # Some selectors resolve to container/form nodes (e.g. #search-form).
            # In that case, find the first editable control inside.
            if tag_name == "form":
                input_locator = locator.locator(
                    "input:not([type='hidden']):not([disabled]), "
                    "textarea:not([disabled]), "
                    "[contenteditable='true']"
                ).first
                await input_locator.wait_for(state="visible", timeout=10000)
                locator = input_locator

            await locator.focus()

            if clear_first:
                # Select all then type to clear reliably
                await locator.fill(text)
            else:
                await locator.type(text, delay=5)

            logger.info("Typed text into element: %s", selector)
        except PlaywrightError as exc:
            raise RuntimeError(
                f"Failed to type into element: {selector}"
            ) from exc

    async def press_key(self, key: str) -> None:
        """
        Press a special key (e.g. ``Enter``, ``Escape``, ``Tab``).

        Args:
            key: The key name (case-insensitive). Common values:
                Enter, Escape, Tab, Backspace, ArrowDown, ArrowUp,
                ArrowLeft, ArrowRight, Home, End, PageDown, PageUp.
        """
        page = self._require_page()
        key = key.strip()
        # Normalise common synonyms
        key_map = {
            "return": "Enter",
            "esc": "Escape",
            "space": " ",
        }
        key = key_map.get(key.lower(), key)
        try:
            await page.keyboard.press(key)
            logger.info("Pressed key: %s", key)
        except PlaywrightError as exc:
            raise RuntimeError(f"Failed to press key: {key}") from exc

    async def scroll(self, direction: str = "down", amount: int = 500) -> None:
        """
        Scroll the page vertically.

        Args:
            direction: ``up`` or ``down``.
            amount: Pixels to scroll.
        """
        page = self._require_page()
        delta = -amount if direction.lower() == "up" else amount
        try:
            await page.evaluate(f"window.scrollBy(0, {delta})")
            # Small wait so any lazy-load content can appear
            await asyncio.sleep(0.3)
            logger.info("Scrolled %s by %d px", direction, amount)
        except PlaywrightError as exc:
            raise RuntimeError(f"Scroll failed: {exc}") from exc

    async def get_screenshot(self, full_page: bool = False) -> bytes:
        """
        Capture a screenshot of the current page.

        Args:
            full_page: If True, capture the entire scrollable page.

        Returns:
            PNG image bytes.
        """
        page = self._require_page()
        try:
            return await page.screenshot(
                full_page=full_page,
                type="png",
            )
        except PlaywrightError as exc:
            raise RuntimeError(f"Screenshot failed: {exc}") from exc

    async def get_distilled_dom(self) -> str:
        """
        Extract a token-efficient structured representation of the page.

        Includes only interactive / semantic elements:
        ``<a>``, ``<button>``, ``<input>``, ``<textarea>``, ``<select>``,
        elements with ``onclick`` or ``role="button"``, and key structural
        elements (headings, labels).

        Hidden elements (``display:none``, ``visibility:hidden``) are excluded.
        ``<script>`` and ``<style>`` tags are removed.

        Returns:
            A clean structured text representation, e.g.::

                [0] <a> text="Home" href="/" visible=True
                [1] <input> placeholder="Search..." type="text" aria-label="Search" visible=True
                [2] <button> text="Submit" aria-label="Submit form" visible=True
        """
        page = self._require_page()
        elements: list[dict[str, Any]] = []
        for attempt in range(3):
            try:
                elements = await page.evaluate(_DISTILL_JS)
                break
            except PlaywrightError as exc:
                message = str(exc).lower()
                is_nav_race = "execution context was destroyed" in message
                if is_nav_race and attempt < 2:
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                logger.error("DOM distillation failed: %s", exc)
                return f"<distillation-error: {exc}>"

        lines: list[str] = []
        lines.append(f"URL: {page.url}")
        title = await self.get_page_title()
        lines.append(f"Title: {title}")
        lines.append("---")

        for idx, el in enumerate(elements):
            tag = el.get("tag", "unknown")
            text = (el.get("text") or "").strip()
            if text:
                text = text[:100]  # truncate

            parts = [f"[{idx}] <{tag}>"]
            if text:
                parts.append(f'text="{text}"')
            for attr in ["aria_label", "placeholder", "title", "href", "input_type", "role"]:
                val = el.get(attr)
                if val:
                    parts.append(f"{attr}='{val}'")
            parts.append(f"visible={el.get('visible', True)}")
            lines.append("  ".join(parts))

        if not elements:
            lines.append("<no interactive elements found>")

        return "\n".join(lines)

    async def get_full_dom(self) -> str:
        """
        Return the raw outer HTML of the current page for debugging.

        Returns:
            Full HTML document string.
        """
        page = self._require_page()
        try:
            return await page.content()
        except PlaywrightError as exc:
            raise RuntimeError(f"Failed to get page content: {exc}") from exc

    async def wait_for_load(self) -> None:
        """
        Wait until the page reaches the ``networkidle`` state.
        Useful after actions that trigger background requests.
        """
        page = self._require_page()
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeoutError:
            logger.warning("wait_for_load timed out; continuing anyway")

    async def save_session_state(self) -> dict:
        """
        Save the current session state (cookies + localStorage).

        Returns:
            A dict with ``cookies`` (list) and ``local_storage`` (dict).
        """
        page = self._require_page()
        context = self._require_context()

        cookies: list[dict] = []
        local_storage: dict[str, str] = {}

        try:
            cookies = await context.cookies()
        except PlaywrightError as exc:
            logger.warning("Failed to save cookies: %s", exc)

        try:
            local_storage = await page.evaluate(
                "() => { let r={}; for (let k in localStorage) { r[k]=localStorage[k]; } return r; }"
            )
        except PlaywrightError as exc:
            logger.warning("Failed to save localStorage: %s", exc)

        return {"cookies": cookies, "local_storage": local_storage}

    async def restore_session_state(self, state: dict) -> None:
        """
        Restore cookies and localStorage from a previously saved state.

        Args:
            state: Dict returned by :meth:`save_session_state`.
        """
        page = self._require_page()
        context = self._require_context()

        cookies = state.get("cookies", [])
        local_storage = state.get("local_storage", {})

        if cookies:
            try:
                await context.add_cookies(cookies)
                logger.info("Restored %d cookie(s)", len(cookies))
            except PlaywrightError as exc:
                logger.warning("Failed to restore cookies: %s", exc)

        if local_storage:
            try:
                # Navigate to about:blank first so we have a frame to execute JS
                # (localStorage is origin-scoped, so this is a no-op unless we are
                # on the target domain; real restoration therefore requires the
                # caller to navigate to the right origin first.)
                for key, value in local_storage.items():
                    await page.evaluate(
                        f"(kv) => {{ try {{ localStorage.setItem(kv[0], kv[1]); }} catch(e) {{}} }}",
                        [key, value],
                    )
                logger.info("Restored %d localStorage item(s)", len(local_storage))
            except PlaywrightError as exc:
                logger.warning("Failed to restore localStorage: %s", exc)

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _require_page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Browser not launched. Call launch() first.")
        return self._page

    def _require_context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("Browser not launched. Call launch() first.")
        return self._context

    async def _safe_cleanup(self) -> None:
        """Close page → context → browser → playwright, swallowing errors."""
        for attr in ("_page", "_context", "_browser"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    await obj.close()
                except Exception:
                    pass
                setattr(self, attr, None)
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    async def _extract_element_info(self, locator: Locator) -> dict:
        """Build a metadata dict for a Playwright Locator."""
        try:
            handle = await locator.element_handle()
            tag = await handle.evaluate("el => el.tagName.toLowerCase()")
            text = await handle.evaluate(
                "el => el.textContent?.trim().substring(0, 100) || ''"
            )
            attrs = await handle.evaluate(
                """el => ({
                    aria_label: el.getAttribute('aria-label') || '',
                    placeholder: el.getAttribute('placeholder') || '',
                    title: el.getAttribute('title') || '',
                    href: el.getAttribute('href') || '',
                    input_type: el.getAttribute('type') || '',
                    role: el.getAttribute('role') || '',
                })"""
            )
            # Build a deterministic selector string for re-use
            selector = await self._build_selector(handle, tag, text, attrs)
            return {
                "selector": selector,
                "tag": tag,
                "text": text,
                "aria_label": attrs.get("aria_label") or None,
                "placeholder": attrs.get("placeholder") or None,
                "title": attrs.get("title") or None,
                "href": attrs.get("href") or None,
                "input_type": attrs.get("input_type") or None,
                "role": attrs.get("role") or None,
            }
        except Exception as exc:
            logger.debug("_extract_element_info failed: %s", exc)
            # Fallback: use the raw Playwright locator string
            return {
                "selector": str(locator),
                "tag": "unknown",
                "text": "",
                "aria_label": None,
                "placeholder": None,
                "title": None,
                "href": None,
                "input_type": None,
                "role": None,
            }

    async def _build_selector(
        self,
        handle: Any,
        tag: str,
        text: str,
        attrs: dict,
    ) -> str:
        """Build a simple, robust Playwright locator string from an element."""
        # Prefer text-based selector when text is short and unique-ish
        if text and len(text) < 60:
            return f'text="{text}"'
        if attrs.get("aria_label"):
            return f"aria-label={attrs['aria_label']}"
        if attrs.get("placeholder"):
            return f"placeholder={attrs['placeholder']}"
        # Fallback to nth-of-type on tag (brittle but functional)
        index = await handle.evaluate(
            "el => Array.from(document.querySelectorAll(el.tagName)).indexOf(el)"
        )
        return f"{tag} >> nth={index}"

    def _looks_like_css_selector(self, description: str) -> bool:
        """Heuristic: does the description look like a CSS selector?"""
        css_indicators = ("#", ".", "[", ">", " ", ":", "::", "*")
        return any(ind in description for ind in css_indicators)

    async def _js_find_element(self, page: Page, description: str) -> dict | None:
        """
        Fallback JavaScript fuzzy finder across visible interactive elements.
        """
        desc_lower = description.lower()
        try:
            result = await page.evaluate(
                """(search) => {
                    const keywords = search.toLowerCase().split(/\\s+/).filter(Boolean);
                    const interactive = new Set([
                        'A','BUTTON','INPUT','TEXTAREA','SELECT','OPTION',
                        'DETAILS','SUMMARY','LABEL'
                    ]);
                    const all = document.querySelectorAll('*');
                    let best = null;
                    let bestScore = 0;
                    for (const el of all) {
                        if (el.closest && el.closest('script,style,noscript,head')) continue;
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden') continue;
                        const tag = el.tagName;
                        const isInteractive = interactive.has(tag) || el.onclick ||
                            el.getAttribute('role') === 'button' ||
                            el.getAttribute('role') === 'link' ||
                            el.getAttribute('role') === 'textbox' ||
                            el.getAttribute('role') === 'searchbox' ||
                            el.getAttribute('tabindex') !== null;
                        if (!isInteractive) continue;

                        const text = (el.textContent || '').trim().toLowerCase();
                        const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                        const placeholder = (el.getAttribute('placeholder') || '').toLowerCase();
                        const title = (el.getAttribute('title') || '').toLowerCase();
                        const label = (el.getAttribute('for')
                            ? (document.querySelector('label[for="'+el.id+'"]')?.textContent || '')
                            : '');

                        let score = 0;
                        for (const kw of keywords) {
                            if (text.includes(kw)) score += 3;
                            if (aria.includes(kw)) score += 4;
                            if (placeholder.includes(kw)) score += 4;
                            if (title.includes(kw)) score += 3;
                            if (label.toLowerCase().includes(kw)) score += 2;
                        }
                        // Boost exact matches
                        const exact = text === search.toLowerCase();
                        if (exact) score += 10;

                        if (score > bestScore) {
                            bestScore = score;
                            best = el;
                        }
                    }

                    if (!best) return null;

                    // Build a Playwright-style selector
                    let sel = best.tagName.toLowerCase();
                    const btext = (best.textContent || '').trim();
                    const bid = best.id;
                    const bclass = best.className;
                    const baria = best.getAttribute('aria-label') || '';
                    const bplaceholder = best.getAttribute('placeholder') || '';
                    const btitle = best.getAttribute('title') || '';

                    if (bid) {
                        sel = '#' + bid;
                    } else if (btext && btext.length < 60) {
                        sel = 'text="' + btext.replace(/"/g, '\\"') + '"';
                    } else if (baria) {
                        sel = 'aria-label=' + baria;
                    } else if (bplaceholder) {
                        sel = 'placeholder=' + bplaceholder;
                    } else if (btitle) {
                        sel = 'title=' + btitle;
                    } else if (bclass) {
                        sel = sel + '.' + bclass.split(/\\s+/)[0];
                    }

                    return {
                        selector: sel,
                        tag: best.tagName.toLowerCase(),
                        text: (best.textContent || '').trim().substring(0, 100),
                        aria_label: best.getAttribute('aria-label') || null,
                        placeholder: best.getAttribute('placeholder') || null,
                        title: best.getAttribute('title') || null,
                        href: best.getAttribute('href') || null,
                        input_type: best.getAttribute('type') || null,
                        role: best.getAttribute('role') || null,
                        confidence: bestScore >= 10 ? 'high' : (bestScore >= 5 ? 'medium' : 'low'),
                    };
                }""",
                description,
            )
            return result
        except PlaywrightError:
            return None


# -------------------------------------------------------------------------- #
# Injected JavaScript for DOM distillation
# -------------------------------------------------------------------------- #

_DISTILL_JS = """
() => {
    const interactiveTags = new Set([
        'A','BUTTON','INPUT','TEXTAREA','SELECT','OPTION',
        'DETAILS','SUMMARY','LABEL'
    ]);
    const interactiveRoles = new Set([
        'button','link','textbox','searchbox','checkbox',
        'radio','combobox','menuitem','tab','switch'
    ]);

    const all = document.querySelectorAll('*');
    const out = [];

    for (const el of all) {
        // Skip hidden / non-rendered
        if (el.closest && el.closest('script,style,noscript,head')) continue;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') continue;

        const tag = el.tagName;
        const role = el.getAttribute('role') || '';
        const hasOnclick = el.hasAttribute('onclick');
        const isInteractive = interactiveTags.has(tag)
            || interactiveRoles.has(role)
            || hasOnclick;

        if (!isInteractive) continue;

        const text = (el.textContent || '').trim().substring(0, 100);
        const visible = el.offsetParent !== null;

        out.push({
            tag: tag.toLowerCase(),
            text: text,
            aria_label: el.getAttribute('aria-label') || null,
            placeholder: el.getAttribute('placeholder') || null,
            title: el.getAttribute('title') || null,
            href: el.getAttribute('href') || null,
            input_type: el.getAttribute('type') || null,
            role: role || null,
            visible: visible,
        });
    }

    return out;
}
"""


# -------------------------------------------------------------------------- #
# Self-test helper
# -------------------------------------------------------------------------- #

async def _self_test() -> None:
    """Quick sanity check — navigates to example.com and prints distilled DOM."""
    logging.basicConfig(level=logging.INFO)
    ctrl = BrowserController()
    await ctrl.launch(headless=True)
    try:
        await ctrl.navigate("https://example.com")
        print("URL:", await ctrl.get_current_url())
        print("Title:", await ctrl.get_page_title())
        dom = await ctrl.get_distilled_dom()
        print("\n--- Distilled DOM ---\n")
        print(dom)

        # Test element resolution
        result = await ctrl.find_element("More information")
        print("\n--- Found element ---")
        print(result)
    finally:
        await ctrl.close()


if __name__ == "__main__":
    asyncio.run(_self_test())
