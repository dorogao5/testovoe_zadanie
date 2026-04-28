from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from ai_browser_agent.agent.models import AgentState, FinalResult, ToolExecutionResult
from ai_browser_agent.browser.actions import ExtractResult, SnapshotMode
from ai_browser_agent.browser.controller import BrowserController
from ai_browser_agent.llm.base import ToolCall, ToolDefinition
from ai_browser_agent.safety.classifier import SecurityLayer
from ai_browser_agent.safety.policy import SecurityAction, summarize_action

AskUserCallback = Callable[[str], str | Awaitable[str]]
SafetyEventCallback = Callable[[dict[str, Any]], None]


class ObserveArgs(BaseModel):
    mode: Literal["visible", "focused", "full_light"] = "visible"


class QueryDomArgs(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=10, ge=1, le=25)


class TakeScreenshotArgs(BaseModel):
    annotated: bool = True
    reason: str = Field(min_length=1)


class NavigateArgs(BaseModel):
    url: str = Field(min_length=1)
    intent: str = "navigate as part of the user task"


class ClickArgs(BaseModel):
    ref: str = Field(min_length=1)
    intent: str = Field(min_length=1)
    button: Literal["left", "right", "middle"] = "left"


class TypeTextArgs(BaseModel):
    ref: str = Field(min_length=1)
    text: str
    clear: bool = True
    intent: str = Field(min_length=1)


class PressKeyArgs(BaseModel):
    key: str = Field(min_length=1)
    intent: str = Field(min_length=1)


class ScrollArgs(BaseModel):
    direction: Literal["up", "down", "left", "right"]
    amount: int | None = Field(default=None, ge=50, le=3000)
    ref: str | None = None


class SelectOptionArgs(BaseModel):
    ref: str = Field(min_length=1)
    value: str = Field(min_length=1)
    intent: str = Field(min_length=1)


class ExtractArgs(BaseModel):
    query: str = Field(min_length=1)
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")
    scope: Literal["visible", "page", "selected"] = "visible"


class WaitArgs(BaseModel):
    seconds: float = Field(ge=0.1, le=30)
    reason: str = Field(min_length=1)


class AskUserArgs(BaseModel):
    question: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class HandoffToUserArgs(BaseModel):
    reason: str = Field(min_length=1)
    expected_user_action: str = Field(min_length=1)


class DoneArgs(BaseModel):
    success: bool
    summary: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)
    remaining_risks: list[str] = Field(default_factory=list)


TOOL_SCHEMAS: dict[str, type[BaseModel]] = {
    "observe": ObserveArgs,
    "query_dom": QueryDomArgs,
    "take_screenshot": TakeScreenshotArgs,
    "navigate": NavigateArgs,
    "click": ClickArgs,
    "type_text": TypeTextArgs,
    "press_key": PressKeyArgs,
    "scroll": ScrollArgs,
    "select_option": SelectOptionArgs,
    "extract": ExtractArgs,
    "wait": WaitArgs,
    "ask_user": AskUserArgs,
    "handoff_to_user": HandoffToUserArgs,
    "done": DoneArgs,
}


TOOL_DESCRIPTIONS: dict[str, str] = {
    "observe": "Return a compact trusted browser-state packet with current refs. Does not expose raw full HTML.",
    "query_dom": "Search current observation for elements by natural language and return candidate refs.",
    "take_screenshot": "Save a screenshot artifact, optionally annotated with current refs.",
    "navigate": "Navigate the visible browser to a URL. Include the intent.",
    "click": "Click an element by current ref. Include the user-goal intent.",
    "type_text": "Type text into an editable element by current ref. Include the user-goal intent.",
    "press_key": "Press a keyboard key in the page. Include the user-goal intent.",
    "scroll": "Scroll the page or an element area.",
    "select_option": "Select an option in a select-like control by current ref.",
    "extract": "Extract query-specific text/facts from visible or page text without putting raw HTML into context.",
    "wait": "Wait briefly for loading, animation, or user-visible state changes.",
    "ask_user": "Ask the user for missing information or confirmation that cannot be inferred safely.",
    "handoff_to_user": "Pause and let the user complete a sensitive/browser-blocked action, then continue.",
    "done": "Finish the task with a grounded summary, evidence and remaining risks.",
}


class ToolDispatcher:
    def __init__(
        self,
        *,
        browser: BrowserController,
        safety: SecurityLayer,
        ask_user: AskUserCallback,
        auto_approve_risky: bool = False,
        on_safety: SafetyEventCallback | None = None,
    ) -> None:
        self.browser = browser
        self.safety = safety
        self.ask_user = ask_user
        self.auto_approve_risky = auto_approve_risky
        self.on_safety = on_safety
        self.extract_cache: dict[str, ExtractResult] = {}

    def definitions(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name=name,
                description=TOOL_DESCRIPTIONS[name],
                input_schema=_compact_json_schema(schema.model_json_schema()),
            )
            for name, schema in TOOL_SCHEMAS.items()
        ]

    async def execute(self, call: ToolCall, state: AgentState) -> ToolExecutionResult:
        schema = TOOL_SCHEMAS.get(call.name)
        if schema is None:
            return ToolExecutionResult(
                ok=False,
                summary=f"Unknown tool {call.name!r}.",
                data={"retryable": True, "suggested_recovery": "Use one of the advertised tools."},
            )
        normalized_args = _normalize_tool_args(call.name, call.args)
        try:
            args_model = schema.model_validate(normalized_args)
        except ValidationError as exc:
            return ToolExecutionResult(
                ok=False,
                summary=f"Invalid arguments for {call.name}: {exc.errors()}",
                data={"retryable": True, "error_class": "tool_validation_error"},
            )

        args = args_model.model_dump(by_alias=True)
        safety_result = await self._check_safety(call.name, args, state)
        if safety_result is not None:
            return safety_result

        try:
            return await self._execute_validated(call.name, args_model, state)
        except Exception as exc:
            return ToolExecutionResult(
                ok=False,
                summary=f"{type(exc).__name__}: {exc}",
                data={"retryable": True, "error_class": "tool_runtime_error"},
            )

    async def _check_safety(
        self,
        tool: str,
        args: dict[str, Any],
        state: AgentState,
    ) -> ToolExecutionResult | None:
        if tool not in {"navigate", "click", "type_text", "press_key", "select_option"}:
            return None
        review_args = self._args_with_element_context(args)
        decision = self.safety.review_tool_call(tool, review_args, user_task=state.task)
        if self.on_safety is not None:
            self.on_safety(
                {
                    "step": state.step,
                    "tool": tool,
                    "action": decision.action.value,
                    "risk": decision.risk.value,
                    "reason": decision.reason,
                    "matched_patterns": decision.matched_patterns,
                    "element": {
                        key: review_args.get(key)
                        for key in ("element_role", "element_name", "element_text")
                        if review_args.get(key)
                    },
                }
            )
        if decision.action == SecurityAction.allow:
            return None
        if decision.action == SecurityAction.confirm and self._is_repeated_singular_destructive_action(
            tool, review_args, state
        ):
            return ToolExecutionResult(
                ok=False,
                summary=(
                    "Blocked a repeated destructive action after one successful destructive action "
                    "for a singular user task."
                ),
                data={
                    "safety": decision.model_dump(mode="json"),
                    "retryable": False,
                    "suggested_recovery": (
                        "Do not delete/archive/trash another item. Verify non-destructively, then call done. "
                        "If the user really wants another item deleted, ask explicitly first."
                    ),
                },
            )

        prompt = (
            f"Safety gate: approve this browser action?\n"
            f"Action: {summarize_action(tool, args)}\n"
            f"Intent: {args.get('intent', '')}\n"
            f"Risk: {decision.risk.value}\n"
            f"Reason: {decision.reason}\n"
            f"Type 'approve' to approve once, 'handoff' to take over, anything else to deny: "
        )
        if self.auto_approve_risky and decision.action == SecurityAction.confirm:
            answer = "approve"
        else:
            answer = await self._ask(prompt)
        normalized = answer.strip().lower()
        if normalized == "approve" and decision.action == SecurityAction.confirm:
            return None
        if normalized == "handoff" or decision.action == SecurityAction.handoff:
            await self._ask("Complete the sensitive step in the browser, then press Enter here to continue: ")
            return ToolExecutionResult(
                ok=True,
                summary="User handoff completed.",
                data={"safety": decision.model_dump(mode="json")},
            )
        return ToolExecutionResult(
            ok=False,
            summary="User denied risky action.",
            data={"safety": decision.model_dump(mode="json"), "retryable": False},
            stop=True,
        )

    def _is_repeated_singular_destructive_action(
        self,
        tool: str,
        args: dict[str, Any],
        state: AgentState,
    ) -> bool:
        if tool not in {"click", "press_key", "select_option"}:
            return False
        if not _looks_destructive_text(
            " ".join(
                [
                    str(args.get("intent", "")),
                    str(args.get("element_name", "")),
                    str(args.get("element_text", "")),
                ]
            )
        ):
            return False
        if _task_allows_multiple_destructive_actions(state.task):
            return False
        return any(
            record.ok
            and record.tool in {"click", "press_key", "select_option"}
            and _looks_destructive_text(
                " ".join(
                    [
                        str(record.args.get("intent", "")),
                        str(record.args.get("element_name", "")),
                        str(record.args.get("element_text", "")),
                    ]
                )
            )
            for record in state.recent_actions
        )

    def _args_with_element_context(self, args: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(args)
        ref = args.get("ref")
        if not ref:
            return enriched
        element = self.browser.resolver.ref_map.get(str(ref))
        if element is None:
            return enriched
        enriched["element_role"] = element.role
        enriched["element_name"] = element.name
        enriched["element_text"] = element.text
        return enriched

    async def _execute_validated(
        self,
        tool: str,
        args: BaseModel,
        state: AgentState,
    ) -> ToolExecutionResult:
        if tool == "observe":
            typed = args  # type: ignore[assignment]
            mode = SnapshotMode(typed.mode)
            browser_state = await self.browser.current_state(mode)
            warnings = self.safety.detect_prompt_injection(browser_state.text_chunks)
            browser_state.security_warnings.extend(
                warning for warning in warnings if warning not in browser_state.security_warnings
            )
            state.last_observation = browser_state
            return ToolExecutionResult(
                ok=True,
                summary=f"Observed {len(browser_state.elements)} elements on {browser_state.title!r}.",
                data={"observation": browser_state.to_model_summary()},
            )

        if tool == "query_dom":
            typed = args  # type: ignore[assignment]
            result = self.browser.resolver.query(typed.query, limit=typed.limit)
            return ToolExecutionResult(
                ok=True,
                summary=f"Found {len(result.candidates)} candidate refs for {typed.query!r}.",
                data={
                    "query": result.query,
                    "ambiguous": result.ambiguous,
                    "candidates": [
                        {
                            "ref": candidate.ref,
                            "score": candidate.score,
                            "evidence": candidate.evidence,
                            "element": candidate.element.model_facing_dict(),
                        }
                        for candidate in result.candidates
                    ],
                },
            )

        if tool == "take_screenshot":
            typed = args  # type: ignore[assignment]
            artifact = await self.browser.screenshot(annotated=typed.annotated, reason=typed.reason)
            return ToolExecutionResult(
                ok=True,
                summary=f"Saved screenshot {artifact.path}.",
                data=artifact.model_dump(mode="json"),
            )

        if tool == "navigate":
            typed = args  # type: ignore[assignment]
            result = await self.browser.navigate(typed.url)
            return _browser_result(result)

        if tool == "click":
            typed = args  # type: ignore[assignment]
            result = await self.browser.click(typed.ref, button=typed.button)
            return _browser_result(result)

        if tool == "type_text":
            typed = args  # type: ignore[assignment]
            result = await self.browser.type_text(typed.ref, typed.text, clear=typed.clear)
            return _browser_result(result)

        if tool == "press_key":
            typed = args  # type: ignore[assignment]
            result = await self.browser.press_key(typed.key)
            return _browser_result(result)

        if tool == "scroll":
            typed = args  # type: ignore[assignment]
            result = await self.browser.scroll(typed.direction, amount=typed.amount, ref=typed.ref)
            return _browser_result(result)

        if tool == "select_option":
            typed = args  # type: ignore[assignment]
            result = await self.browser.select_option(typed.ref, typed.value)
            return _browser_result(result)

        if tool == "extract":
            typed = args  # type: ignore[assignment]
            result = await self._extract(typed.query, typed.scope, typed.schema_, state)
            return ToolExecutionResult(
                ok=True,
                summary=(
                    f"{'Reused cached extraction' if result.cache_hit else 'Extracted'} "
                    f"{len(result.content)} chars for query {typed.query!r}."
                ),
                data=result.model_dump(mode="json"),
            )

        if tool == "wait":
            typed = args  # type: ignore[assignment]
            await asyncio.sleep(typed.seconds)
            result = await self.browser.wait_for_stable(timeout_ms=int(typed.seconds * 1000))
            return _browser_result(result)

        if tool == "ask_user":
            typed = args  # type: ignore[assignment]
            if _looks_secret_request(f"{typed.question} {typed.reason}"):
                await self._ask(
                    f"Sensitive handoff requested: {typed.reason}\n"
                    "Do not paste passwords, OTP codes, recovery codes, API keys, or secrets into chat. "
                    "Complete the sensitive step directly in the visible browser, then press Enter here: "
                )
                return ToolExecutionResult(
                    ok=True,
                    summary="User completed sensitive handoff; no secret was collected.",
                    data={"answer": "[redacted-sensitive-handoff]", "handoff": True},
                )
            answer = await self._ask(f"{typed.question}\nReason: {typed.reason}\n> ")
            return ToolExecutionResult(ok=True, summary="User answered.", data={"answer": answer})

        if tool == "handoff_to_user":
            typed = args  # type: ignore[assignment]
            await self._ask(
                f"Handoff requested: {typed.reason}\n"
                f"Expected user action: {typed.expected_user_action}\n"
                "Press Enter when ready to continue: "
            )
            return ToolExecutionResult(ok=True, summary="User completed handoff.")

        if tool == "done":
            typed = args  # type: ignore[assignment]
            final = FinalResult(
                success=typed.success,
                summary=typed.summary,
                evidence=typed.evidence,
                remaining_risks=typed.remaining_risks,
            )
            return ToolExecutionResult(ok=True, summary=typed.summary, stop=True, final=final)

        raise RuntimeError(f"Unhandled tool {tool!r}.")

    async def _extract(
        self,
        query: str,
        scope: str,
        schema: dict[str, Any] | None,
        state: AgentState,
    ) -> ExtractResult:
        page = self.browser._require_page()
        fingerprint = state.last_observation.fingerprint if state.last_observation else None
        cache_key = self._extract_cache_key(page.url, fingerprint, query, scope, schema)
        cached = self.extract_cache.get(cache_key)
        if cached is not None:
            return cached.model_copy(update={"cache_hit": True})
        if scope == "selected":
            selected_text = await page.evaluate(
                "() => String(window.getSelection ? window.getSelection().toString() : '').trim()"
            )
            blocks = [selected_text] if selected_text else []
        elif scope == "visible":
            blocks = await page.evaluate(
                """
                () => {
                  const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return style.display !== 'none' && style.visibility !== 'hidden' &&
                      rect.width > 0 && rect.height > 0 &&
                      rect.bottom >= 0 && rect.top <= window.innerHeight;
                  };
                  const label = (el) => {
                    const heading = el.querySelector('h1,h2,h3,h4,h5,h6,[role="heading"]');
                    return clean(el.getAttribute('aria-label') || el.getAttribute('title') ||
                      (heading ? heading.innerText || heading.textContent : ''));
                  };
                  const semantic = Array.from(document.body.querySelectorAll(
                    'article, aside, section, form, [role="region"], [role="dialog"]'
                  ))
                    .filter(visible)
                    .map((el) => {
                      const text = clean(el.innerText || el.textContent || '');
                      const heading = label(el);
                      return heading ? `${heading}\\n${text}` : text;
                    })
                    .filter(Boolean);
                  const leaf = Array.from(document.body.querySelectorAll('button,a,input,textarea,select,li,p,h1,h2,h3,h4,h5,h6'))
                    .filter(visible)
                    .map((el) => clean(el.innerText || el.textContent || el.value || el.getAttribute('placeholder') || ''))
                    .filter(Boolean);
                  return [...semantic, ...leaf];
                }
                """
            )
        else:
            text = await page.locator("body").inner_text(timeout=5000)
            blocks = [text]
        normalized_blocks = _dedupe_blocks(str(block) for block in blocks)
        evidence = _rank_extraction_blocks(query, normalized_blocks)[:12]
        content = "\n\n".join(evidence or normalized_blocks[:20])[:8000]
        source_refs = [candidate.ref for candidate in self.browser.resolver.query(query, limit=8).candidates]
        structured_data = _rough_structured_data(schema, evidence or normalized_blocks[:20]) if schema else None
        if scope == "selected" and not normalized_blocks:
            uncertainty = "No selected text was available."
        else:
            uncertainty = None if evidence else "No direct lexical matches; returned leading text."
        result = ExtractResult(
            query=query,
            scope=scope,
            content=content,
            evidence=evidence,
            source_refs=source_refs,
            structured_data=structured_data,
            fingerprint=fingerprint,
            uncertainty=uncertainty,
        )
        self.extract_cache[cache_key] = result
        return result

    def _extract_cache_key(
        self,
        url: str,
        fingerprint: str | None,
        query: str,
        scope: str,
        schema: dict[str, Any] | None,
    ) -> str:
        payload = json.dumps(
            {
                "url": url,
                "fingerprint": fingerprint,
                "query": query,
                "scope": scope,
                "schema": schema,
            },
            ensure_ascii=True,
            sort_keys=True,
            default=str,
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    async def _ask(self, prompt: str) -> str:
        answer = self.ask_user(prompt)
        if inspect.isawaitable(answer):
            return await answer
        return answer


def _browser_result(result: Any) -> ToolExecutionResult:
    return ToolExecutionResult(
        ok=result.ok,
        summary=result.summary,
        data=result.model_dump(mode="json"),
    )


def _compact_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    noisy_keys = {"title", "default"}

    def compact(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: compact(item)
                for key, item in value.items()
                if key not in noisy_keys
            }
        if isinstance(value, list):
            return [compact(item) for item in value]
        return value

    return compact(schema)


def _normalize_tool_args(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(args)
    if tool == "extract":
        schema = normalized.get("schema")
        if isinstance(schema, str):
            try:
                parsed_schema = json.loads(schema)
            except json.JSONDecodeError:
                parsed_schema = schema
            if isinstance(parsed_schema, dict):
                normalized["schema"] = parsed_schema
            else:
                normalized.pop("schema", None)
        elif schema is not None and not isinstance(schema, dict):
            normalized.pop("schema", None)
    if tool == "done":
        for key in ("evidence", "remaining_risks"):
            value = normalized.get(key)
            if value is None:
                normalized[key] = []
            elif isinstance(value, str):
                stripped = value.strip()
                if key == "remaining_risks" and _looks_like_no_risks(stripped):
                    normalized[key] = []
                else:
                    normalized[key] = [stripped] if stripped else []
    return normalized


def _looks_like_no_risks(value: str) -> bool:
    normalized = re.sub(r"[\W_]+", " ", value.lower()).strip()
    return normalized in {
        "",
        "none",
        "no risks",
        "no remaining risks",
        "task completed successfully",
        "none task completed successfully",
        "нет",
        "нет рисков",
    }


def _rough_structured_data(schema: dict[str, Any] | None, lines: list[str]) -> dict[str, Any] | None:
    if not schema:
        return None
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return None
    structured: dict[str, Any] = {}
    for field_name in properties:
        terms = {part.lower() for part in str(field_name).replace("_", " ").split() if len(part) > 2}
        match = next(
            (
                line
                for line in lines
                if not terms or any(term in line.lower() for term in terms)
            ),
            None,
        )
        structured[field_name] = match
    return structured


def _dedupe_blocks(blocks: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for block in blocks:
        normalized = re.sub(r"\s+", " ", str(block)).strip()
        if not normalized or normalized in seen:
            continue
        if any(existing == normalized or normalized in existing for existing in seen):
            continue
        seen.add(normalized)
        result.append(normalized[:1200])
    return result


def _rank_extraction_blocks(query: str, blocks: list[str]) -> list[str]:
    query_terms = _extract_query_terms(query)
    if not query_terms:
        return blocks[:12]
    scored: list[tuple[float, int, str]] = []
    for index, block in enumerate(blocks):
        block_terms = _extract_query_terms(block)
        overlap = query_terms & block_terms
        if not overlap:
            continue
        score = float(len(overlap))
        lower = block.lower()
        if "\n" in block or len(block.split()) > 8:
            score += 1.5
        if query_terms & {"items", "item", "cart", "checkout"}:
            if lower.startswith("cart ") or lower.startswith("cart\n"):
                score += 6.0
            if lower.startswith("checkout ") or lower.startswith("checkout\n"):
                score += 5.0
            if "items:" in lower:
                score += 4.0
            if "add to cart" in lower and not (lower.startswith("cart") or lower.startswith("checkout")):
                score -= 4.0
        if any(term in lower for term in ("items:", "cart", "checkout")) and query_terms & {
            "items",
            "item",
            "cart",
            "checkout",
        }:
            score += 2.0
        if lower in {"add to cart", "search", "go to checkout"}:
            score -= 3.0
        scored.append((score, -index, block[:600]))
    scored.sort(reverse=True)
    if query_terms & {"items", "item", "cart", "checkout"}:
        focused = [
            block
            for score, _index, block in scored
            if block.lower().startswith(("cart ", "cart\n", "checkout ", "checkout\n"))
            or "items:" in block.lower()
        ]
        if focused:
            return focused
    return [block for score, _index, block in scored if score > 0]


def _extract_query_terms(value: str) -> set[str]:
    stop = {
        "the",
        "and",
        "with",
        "currently",
        "current",
        "showing",
        "especially",
        "all",
        "list",
        "what",
        "which",
        "their",
        "there",
        "from",
        "into",
        "для",
        "или",
        "это",
    }
    words = re.findall(r"[a-zа-я0-9][a-zа-я0-9_-]{1,}", value.lower(), flags=re.I)
    return {word for word in words if word not in stop}


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


def _looks_secret_request(text: str) -> bool:
    return bool(
        re.search(
            r"\b(password|passcode|otp|2fa|mfa|verification code|recovery code|api key|secret|token|парол|код подтверж|одноразов|секрет|токен)\b",
            text.lower(),
            re.I,
        )
    )
