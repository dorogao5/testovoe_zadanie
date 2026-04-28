from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ai_browser_agent.browser.actions import (
    BrowserActionResult,
    BrowserState,
    BrowserTab,
    ScreenshotArtifact,
    SnapshotMode,
)
from ai_browser_agent.browser.resolver import ElementResolver
from ai_browser_agent.browser.snapshot import SnapshotEngine


class BrowserController:
    def __init__(
        self,
        *,
        viewport: dict[str, int] | None = None,
        artifacts_dir: Path | None = None,
        action_timeout_ms: int = 10_000,
    ) -> None:
        self.viewport = viewport or {"width": 1280, "height": 900}
        self.artifacts_dir = artifacts_dir or Path("runs/manual")
        self.action_timeout_ms = action_timeout_ms
        self.playwright: Any | None = None
        self.context: Any | None = None
        self.page: Any | None = None
        self._connected_over_cdp = False
        self.snapshot_engine = SnapshotEngine()
        self.resolver = ElementResolver()
        self._trace_enabled = False
        self._screenshot_count = 0

    @property
    def launched(self) -> bool:
        return self.context is not None and self.page is not None

    async def launch(
        self,
        profile_dir: Path | None = None,
        *,
        headless: bool = False,
        browser_channel: str | None = None,
        cdp_url: str | None = None,
        record_video: bool = False,
        trace: bool = False,
    ) -> None:
        from playwright.async_api import async_playwright

        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.playwright = await async_playwright().start()
        browser_args = ["--disable-extensions"]
        video_dir = self.artifacts_dir / "video" if record_video else None
        if video_dir:
            video_dir.mkdir(parents=True, exist_ok=True)

        launch_options = {
            "headless": headless,
            "viewport": self.viewport,
            "args": browser_args,
            "accept_downloads": True,
        }
        if video_dir:
            launch_options["record_video_dir"] = str(video_dir)
            launch_options["record_video_size"] = self.viewport
        if browser_channel:
            launch_options["channel"] = browser_channel

        if cdp_url:
            browser = await self.playwright.chromium.connect_over_cdp(cdp_url)
            self._connected_over_cdp = True
            self.context = browser.contexts[0] if browser.contexts else await browser.new_context(
                viewport=self.viewport,
                accept_downloads=True,
                record_video_dir=str(video_dir) if video_dir else None,
                record_video_size=self.viewport if video_dir else None,
            )
            self.context.set_default_timeout(self.action_timeout_ms)
            self.context.on("page", self._on_page)
            self.page = await self._select_initial_page()
            if trace:
                await self.context.tracing.start(screenshots=True, snapshots=True, sources=True)
                self._trace_enabled = True
            return

        if profile_dir is not None:
            profile_dir.mkdir(parents=True, exist_ok=True)
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(profile_dir),
                **launch_options,
            )
        else:
            launch_kwargs: dict[str, Any] = {"headless": headless, "args": browser_args}
            if browser_channel:
                launch_kwargs["channel"] = browser_channel
            browser = await self.playwright.chromium.launch(**launch_kwargs)
            self.context = await browser.new_context(
                viewport=self.viewport,
                accept_downloads=True,
                record_video_dir=str(video_dir) if video_dir else None,
                record_video_size=self.viewport if video_dir else None,
            )

        self.context.set_default_timeout(self.action_timeout_ms)
        self.context.on("page", self._on_page)
        self.page = await self._select_initial_page()
        if trace:
            await self.context.tracing.start(screenshots=True, snapshots=True, sources=True)
            self._trace_enabled = True

    async def close(self) -> None:
        if self.context is not None:
            if self._trace_enabled:
                trace_path = self.artifacts_dir / "trace.zip"
                try:
                    await self.context.tracing.stop(path=str(trace_path))
                except Exception:
                    pass
            if not self._connected_over_cdp:
                await self.context.close()
            self.context = None
        if self.playwright is not None:
            await self.playwright.stop()
            self.playwright = None
        self._connected_over_cdp = False
        self.page = None

    def _on_page(self, page: Any) -> None:
        if self._is_automation_noise_url(getattr(page, "url", "")):
            return
        self.page = page

    async def _select_initial_page(self) -> Any:
        if self.context is None:
            raise RuntimeError("Browser context is not launched.")
        pages = list(self.context.pages)
        for page in reversed(pages):
            if not self._is_automation_noise_url(getattr(page, "url", "")):
                return page
        return await self.context.new_page()

    def _is_automation_noise_url(self, url: str) -> bool:
        lowered = (url or "").lower()
        if any(marker in lowered for marker in ("adblock", "ublock", "adguard")):
            return True
        return lowered.startswith(
            (
                "chrome-extension://",
                "chrome://",
                "edge://",
                "devtools://",
                "about:extensions",
            )
        )

    def _require_page(self) -> Any:
        if self.page is None:
            raise RuntimeError("Browser is not launched.")
        return self.page

    async def navigate(self, url: str) -> BrowserActionResult:
        page = self._require_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await self.wait_for_stable(timeout_ms=3000)
            return await self._action_ok(f"Navigated to {url}")
        except Exception as exc:
            return self._action_error("navigation_failed", exc, "Check the URL or try again after waiting.")

    async def go_back(self) -> BrowserActionResult:
        page = self._require_page()
        try:
            await page.go_back(wait_until="domcontentloaded", timeout=15_000)
            await self.wait_for_stable(timeout_ms=3000)
            return await self._action_ok("Went back")
        except Exception as exc:
            return self._action_error("navigation_failed", exc, "Try observing the page and navigate manually.")

    async def click(self, ref: str, *, button: str = "left") -> BrowserActionResult:
        old_element = self.resolver.ref_map.get(ref)
        try:
            return await self._click_once(ref, button=button)
        except LookupError as exc:
            recovered = await self._recover_ref(old_element)
            if recovered and recovered != ref:
                try:
                    result = await self._click_once(recovered, button=button)
                    result.summary = f"{result.summary} after stale-ref recovery from {ref}"
                    result.data["recovered_from"] = ref
                    result.data["recovered_ref"] = recovered
                    return result
                except Exception:
                    pass
            return BrowserActionResult.failure(
                "ref_not_found",
                str(exc),
                "Call observe or query_dom, then retry with a current ref.",
            )
        except Exception as exc:
            recovered = await self._recover_ref(old_element)
            if recovered and recovered != ref:
                try:
                    result = await self._click_once(recovered, button=button)
                    result.summary = f"{result.summary} after stale-ref recovery from {ref}"
                    result.data["recovered_from"] = ref
                    result.data["recovered_ref"] = recovered
                    return result
                except Exception:
                    pass
            return self._action_error("click_failed", exc, "Wait, close overlays, or query the element again.")

    async def type_text(self, ref: str, text: str, *, clear: bool = True) -> BrowserActionResult:
        old_element = self.resolver.ref_map.get(ref)
        try:
            return await self._type_once(ref, text, clear=clear)
        except LookupError as exc:
            recovered = await self._recover_ref(old_element)
            if recovered and recovered != ref:
                try:
                    result = await self._type_once(recovered, text, clear=clear)
                    result.summary = f"{result.summary} after stale-ref recovery from {ref}"
                    result.data["recovered_from"] = ref
                    result.data["recovered_ref"] = recovered
                    return result
                except Exception:
                    pass
            return BrowserActionResult.failure(
                "ref_not_found",
                str(exc),
                "Call observe or query_dom and type into a textbox-like ref.",
            )
        except Exception as exc:
            recovered = await self._recover_ref(old_element)
            if recovered and recovered != ref:
                try:
                    result = await self._type_once(recovered, text, clear=clear)
                    result.summary = f"{result.summary} after stale-ref recovery from {ref}"
                    result.data["recovered_from"] = ref
                    result.data["recovered_ref"] = recovered
                    return result
                except Exception:
                    pass
            return self._action_error("type_failed", exc, "Ensure the ref is editable and retry.")

    async def press_key(self, key: str) -> BrowserActionResult:
        page = self._require_page()
        try:
            await page.keyboard.press(key)
            await self.wait_for_stable(timeout_ms=1500)
            return await self._action_ok(f"Pressed {key}")
        except Exception as exc:
            return self._action_error("key_failed", exc, "Ensure the page is focused and retry.")

    async def scroll(
        self,
        direction: str,
        *,
        amount: int | None = None,
        ref: str | None = None,
    ) -> BrowserActionResult:
        page = self._require_page()
        amount = amount or 650
        dx, dy = {
            "up": (0, -amount),
            "down": (0, amount),
            "left": (-amount, 0),
            "right": (amount, 0),
        }.get(direction, (0, amount))
        try:
            if ref:
                target = await self.resolver.resolve(page, ref)
                if target.locator is not None:
                    await target.locator.hover(timeout=3000)
            await page.mouse.wheel(dx, dy)
            await self.wait_for_stable(timeout_ms=1000)
            return await self._action_ok(f"Scrolled {direction}", amount=amount)
        except Exception as exc:
            return self._action_error("scroll_failed", exc, "Try a smaller scroll or observe again.")

    async def select_option(self, ref: str, value: str) -> BrowserActionResult:
        old_element = self.resolver.ref_map.get(ref)
        try:
            return await self._select_once(ref, value)
        except LookupError as exc:
            recovered = await self._recover_ref(old_element)
            if recovered and recovered != ref:
                try:
                    result = await self._select_once(recovered, value)
                    result.summary = f"{result.summary} after stale-ref recovery from {ref}"
                    result.data["recovered_from"] = ref
                    result.data["recovered_ref"] = recovered
                    return result
                except Exception:
                    pass
            return BrowserActionResult.failure(
                "ref_not_found",
                str(exc),
                "Call observe or query_dom and select a current select-like ref.",
            )
        except Exception as exc:
            recovered = await self._recover_ref(old_element)
            if recovered and recovered != ref:
                try:
                    result = await self._select_once(recovered, value)
                    result.summary = f"{result.summary} after stale-ref recovery from {ref}"
                    result.data["recovered_from"] = ref
                    result.data["recovered_ref"] = recovered
                    return result
                except Exception:
                    pass
            return self._action_error("select_failed", exc, "Check available options or query the select again.")

    async def wait_for_stable(self, *, timeout_ms: int = 5000) -> BrowserActionResult:
        page = self._require_page()
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 2500))
            except Exception:
                pass
            await asyncio.sleep(0.15)
            return BrowserActionResult.success("Page is stable enough")
        except Exception as exc:
            return self._action_error("timeout_loading", exc, "Wait again or continue from the visible state.")

    async def current_url_title(self) -> tuple[str, str]:
        page = self._require_page()
        return page.url, await page.title()

    async def current_state(self, mode: SnapshotMode = SnapshotMode.visible) -> BrowserState:
        page = self._require_page()
        tabs = await self._tabs()
        state = await self.snapshot_engine.snapshot(page, mode, tabs=tabs)
        self.resolver.update_ref_map(self.snapshot_engine.ref_map)
        return state

    async def screenshot(
        self,
        *,
        annotated: bool = False,
        reason: str | None = None,
    ) -> ScreenshotArtifact:
        page = self._require_page()
        screenshots_dir = self.artifacts_dir / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        self._screenshot_count += 1
        suffix = "annotated" if annotated else "raw"
        path = screenshots_dir / f"{self._screenshot_count:04d}-{suffix}.png"
        cleanup_needed = False
        if annotated and self.resolver.ref_map:
            await self._add_ref_overlays(page)
            cleanup_needed = True
        try:
            await page.screenshot(path=str(path), full_page=False)
        finally:
            if cleanup_needed:
                await page.evaluate(
                    "() => document.querySelectorAll('[data-ai-browser-overlay]').forEach((node) => node.remove())"
                )
        return ScreenshotArtifact(
            path=path,
            annotated=annotated,
            url=page.url,
            title=await page.title(),
            reason=reason,
        )

    async def _click_once(self, ref: str, *, button: str = "left") -> BrowserActionResult:
        page = self._require_page()
        target = await self.resolver.resolve(page, ref)
        if target.locator is not None:
            try:
                await target.locator.scroll_into_view_if_needed(timeout=3000)
                await target.locator.click(button=button, timeout=self.action_timeout_ms)
            except Exception:
                if target.element is None or target.element.bbox is None:
                    raise
                await page.mouse.click(*target.element.bbox.center, button=button)
                await self.wait_for_stable(timeout_ms=2500)
                return await self._action_ok(
                    f"Clicked {ref} by coordinates after locator click failed",
                    fallback="coordinate_click",
                )
        elif target.point is not None:
            await page.mouse.click(target.point[0], target.point[1], button=button)
        await self.wait_for_stable(timeout_ms=2500)
        return await self._action_ok(f"Clicked {ref}")

    async def _type_once(self, ref: str, text: str, *, clear: bool = True) -> BrowserActionResult:
        page = self._require_page()
        target = await self.resolver.resolve(page, ref)
        if target.locator is None:
            raise LookupError(f"Ref {ref!r} resolved only to coordinates; cannot type safely.")
        await target.locator.scroll_into_view_if_needed(timeout=5000)
        if clear:
            await target.locator.fill(text, timeout=self.action_timeout_ms)
        else:
            await target.locator.type(text, timeout=self.action_timeout_ms)
        await self.wait_for_stable(timeout_ms=1500)
        return await self._action_ok(f"Typed into {ref}", chars=len(text))

    async def _select_once(self, ref: str, value: str) -> BrowserActionResult:
        page = self._require_page()
        target = await self.resolver.resolve(page, ref)
        if target.locator is None:
            raise LookupError(f"Ref {ref!r} resolved only to coordinates; cannot select safely.")
        await target.locator.select_option(value=value, timeout=self.action_timeout_ms)
        await self.wait_for_stable(timeout_ms=1500)
        return await self._action_ok(f"Selected option on {ref}", value=value)

    async def _recover_ref(self, old_element: Any | None) -> str | None:
        if old_element is None:
            return None
        try:
            await self.current_state(SnapshotMode.visible)
        except Exception:
            return None
        return self.resolver.find_equivalent_ref(old_element)

    async def _add_ref_overlays(self, page: Any) -> None:
        overlays = [
            {
                "ref": ref,
                "x": element.bbox.x,
                "y": element.bbox.y,
                "width": element.bbox.width,
                "height": element.bbox.height,
            }
            for ref, element in self.resolver.ref_map.items()
            if element.bbox is not None and element.in_viewport
        ][:80]
        await page.evaluate(
            """
            (items) => {
              document.querySelectorAll('[data-ai-browser-overlay]').forEach((node) => node.remove());
              for (const item of items) {
                const box = document.createElement('div');
                box.setAttribute('data-ai-browser-overlay', '1');
                box.style.position = 'fixed';
                box.style.left = `${item.x}px`;
                box.style.top = `${item.y}px`;
                box.style.width = `${item.width}px`;
                box.style.height = `${item.height}px`;
                box.style.border = '2px solid #ff2d55';
                box.style.background = 'rgba(255, 45, 85, 0.08)';
                box.style.pointerEvents = 'none';
                box.style.zIndex = '2147483647';
                const label = document.createElement('div');
                label.textContent = item.ref;
                label.style.position = 'absolute';
                label.style.left = '0';
                label.style.top = '-18px';
                label.style.padding = '1px 4px';
                label.style.background = '#ff2d55';
                label.style.color = 'white';
                label.style.font = '12px sans-serif';
                box.appendChild(label);
                document.body.appendChild(box);
              }
            }
            """,
            overlays,
        )

    async def _tabs(self) -> list[BrowserTab]:
        if self.context is None:
            return []
        active = self.page
        tabs: list[BrowserTab] = []
        for index, page in enumerate(self.context.pages):
            try:
                tabs.append(
                    BrowserTab(
                        index=index,
                        url=page.url,
                        title=await page.title(),
                        active=page == active,
                    )
                )
            except Exception:
                continue
        return tabs

    async def _action_ok(self, summary: str, **data: Any) -> BrowserActionResult:
        result = BrowserActionResult.success(summary, **data)
        result.url, result.title = await self._safe_page_metadata()
        return result

    def _action_error(self, error_class: str, exc: Exception, suggested_recovery: str) -> BrowserActionResult:
        return BrowserActionResult.failure(
            error_class,
            f"{type(exc).__name__}: {exc}",
            suggested_recovery,
        )

    async def _safe_page_metadata(self) -> tuple[str | None, str | None]:
        page = self._require_page()
        url: str | None = None
        title: str | None = None
        for attempt in range(4):
            try:
                url = page.url
            except Exception:
                url = None
            try:
                title = await page.title()
                return url, title
            except Exception:
                if attempt == 3:
                    return url, title
                await asyncio.sleep(0.15)
