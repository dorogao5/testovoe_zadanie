from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from ai_browser_agent.browser.actions import ElementCandidate, ElementCandidates, ElementRef


@dataclass(frozen=True)
class ResolvedTarget:
    locator: Any | None = None
    point: tuple[float, float] | None = None
    element: ElementRef | None = None


class ElementResolver:
    def __init__(self) -> None:
        self.ref_map: dict[str, ElementRef] = {}

    def update_ref_map(self, ref_map: dict[str, ElementRef]) -> None:
        self.ref_map = dict(ref_map)

    async def resolve(self, page: Any, ref: str) -> ResolvedTarget:
        element = self.ref_map.get(ref)
        if not element:
            raise LookupError(f"Unknown element ref {ref!r}; call observe or query_dom again.")

        frame = self._frame_for_element(page, element)
        locator = frame.locator(f'[data-ai-browser-ref="{ref}"]').first
        try:
            if await locator.count() > 0:
                return ResolvedTarget(locator=locator, element=element)
        except Exception:
            pass

        fallback = await self._fallback_locator(frame, element)
        if fallback is not None:
            return ResolvedTarget(locator=fallback, element=element)

        if element.bbox is not None:
            return ResolvedTarget(point=element.bbox.center, element=element)
        raise LookupError(f"Element {ref!r} cannot be resolved and has no usable coordinates.")

    def find_equivalent_ref(self, old: ElementRef) -> str | None:
        for candidate in self.ref_map.values():
            if candidate.signature_hash == old.signature_hash:
                return candidate.ref
        query_parts = [
            old.name or "",
            old.text or "",
            old.placeholder or "",
            old.aria_label or "",
            old.title or "",
        ]
        query = " ".join(part for part in query_parts if part).strip()
        if not query:
            return None
        result = self.query(query, limit=1)
        if result.candidates and result.candidates[0].score >= 2.0:
            return result.candidates[0].ref
        return None

    def _frame_for_element(self, page: Any, element: ElementRef) -> Any:
        frames = list(getattr(page, "frames", []) or [])
        if 0 <= element.frame_index < len(frames):
            return frames[element.frame_index]
        return page

    async def _fallback_locator(self, frame: Any, element: ElementRef) -> Any | None:
        candidates: list[Any] = []
        name = element.name or element.aria_label or element.title
        if element.role and name:
            candidates.append(frame.get_by_role(element.role, name=re.compile(re.escape(name), re.I)).first)
        if element.placeholder:
            candidates.append(frame.get_by_placeholder(element.placeholder).first)
        if element.text and element.tag not in {"input", "textarea"}:
            candidates.append(frame.get_by_text(element.text, exact=False).first)

        for locator in candidates:
            try:
                if await locator.count() > 0:
                    return locator
            except Exception:
                continue
        return None

    def query(self, query: str, *, limit: int = 10) -> ElementCandidates:
        scored: list[ElementCandidate] = []
        for element in self.ref_map.values():
            score, evidence = self._score(query, element, all_elements=list(self.ref_map.values()))
            if score > 0:
                scored.append(
                    ElementCandidate(
                        ref=element.ref,
                        score=round(score, 3),
                        evidence=evidence,
                        element=element,
                    )
                )
        scored.sort(key=lambda item: item.score, reverse=True)
        candidates = scored[:limit]
        ambiguous = len(candidates) > 1 and math.isclose(candidates[0].score, candidates[1].score, rel_tol=0.12)
        return ElementCandidates(query=query, candidates=candidates, ambiguous=ambiguous)

    def _score(
        self,
        query: str,
        element: ElementRef,
        *,
        all_elements: list[ElementRef] | None = None,
    ) -> tuple[float, str]:
        query_tokens = _tokens(query)
        haystacks = {
            "name": element.name or "",
            "text": element.text or "",
            "placeholder": element.placeholder or "",
            "role": element.role or "",
            "title": element.title or "",
            "parent": " ".join(element.parent_chain),
            "href": element.href or "",
        }
        score = 0.0
        evidence_parts: list[str] = []
        for field, value in haystacks.items():
            value_tokens = _tokens(value)
            if not value_tokens:
                continue
            overlap = query_tokens & value_tokens
            if overlap:
                weight = {
                    "name": 4.0,
                    "placeholder": 3.5,
                    "text": 2.5,
                    "role": 2.0,
                    "title": 2.0,
                    "parent": 1.2,
                    "href": 0.8,
                }[field]
                score += weight * len(overlap)
                evidence_parts.append(f"{field}: {', '.join(sorted(overlap))}")
            value_norm = _norm(value)
            query_norm = _norm(query)
            if query_norm and query_norm in value_norm:
                score += 2.5
                evidence_parts.append(f"{field} contains query")

        if element.in_viewport:
            score += 0.4
        if element.enabled:
            score += 0.2
        if element.focused:
            score += 0.4
        action_label = _norm(" ".join(part for part in [element.name, element.text] if part))
        wants_action = bool(query_tokens & {"click", "press", "button", "add", "select", "type", "open"})
        if wants_action and element.role in {"button", "link", "textbox", "combobox"}:
            score += 3.0
            evidence_parts.append("interactive action target")
        if wants_action and element.tag in {"section", "aside", "article", "form", "main"}:
            score -= 3.0
            evidence_parts.append("penalized non-interactive container")
        wants_container = bool(
            query_tokens
            & {
                "section",
                "region",
                "area",
                "panel",
                "list",
                "items",
                "item",
                "quantity",
                "quantities",
                "contents",
                "current",
                "currently",
            }
        )
        if wants_container and element.tag in {"section", "aside", "article", "form", "main"}:
            score += 5.0
            evidence_parts.append("semantic container")
        if wants_container and element.role == "button" and action_label in {"add to cart", "add cart"}:
            score -= 8.0
            evidence_parts.append("penalized generic add-to-cart action")
        if (
            "cart" in query_tokens
            and "add" not in query_tokens
            and element.role == "button"
            and action_label in {"add to cart", "add cart"}
        ):
            score -= 6.0
            evidence_parts.append("penalized cart action label")
        name_norm = _norm(element.name or "")
        text_norm = _norm(element.text or "")
        if "cart" in query_tokens and name_norm == "cart":
            score += 10.0
            evidence_parts.append("exact cart container label")
        if "cart" in query_tokens and element.tag == "article" and "add to cart" in text_norm:
            score -= 8.0
            evidence_parts.append("penalized product card for cart query")
        siblings = [candidate for candidate in (all_elements or []) if candidate is not element]
        if siblings:
            duplicate_label_count = sum(
                1
                for candidate in siblings
                if (candidate.name or candidate.text) == (element.name or element.text)
            )
            if duplicate_label_count:
                query_norm = _norm(query)
                parent_norm = _norm(" ".join(element.parent_chain))
                unique_parent_tokens = query_tokens & _tokens(parent_norm)
                if unique_parent_tokens:
                    score += 6.0 * len(unique_parent_tokens)
                    evidence_parts.append(
                        f"container context: {', '.join(sorted(unique_parent_tokens))}"
                    )
                if query_norm and query_norm in parent_norm:
                    score += 8.0
                    evidence_parts.append("container contains query")
                if query_tokens and not unique_parent_tokens and {"add", "cart", "to"} & query_tokens:
                    score -= 1.0

        return score, "; ".join(evidence_parts) or "weak lexical match"


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower()).strip()


def _tokens(value: str) -> set[str]:
    words = re.findall(r"[a-zа-я0-9][a-zа-я0-9_-]{1,}", value.lower(), flags=re.I)
    stop = {"the", "and", "for", "with", "this", "that", "you", "your", "на", "для", "или", "это"}
    return {word for word in words if word not in stop}
