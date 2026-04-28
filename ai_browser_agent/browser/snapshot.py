from __future__ import annotations

import hashlib
import json
from typing import Any

from ai_browser_agent.browser.actions import (
    BoundingBox,
    BrowserState,
    BrowserTab,
    ElementRef,
    PageStats,
    ScrollState,
    SnapshotMode,
)


DOM_SNAPSHOT_SCRIPT = r"""
async ({ mode, maxElements, maxTextChunks, refPrefix }) => {
  const suspiciousPatterns = [
    /ignore (all )?(previous|prior) instructions/i,
    /system prompt/i,
    /developer message/i,
    /send (money|payment|password|token)/i,
    /delete all/i,
    /urgent.{0,40}(pay|transfer|wire|delete|submit)/i
  ];

  const viewport = {
    width: window.innerWidth || document.documentElement.clientWidth || 0,
    height: window.innerHeight || document.documentElement.clientHeight || 0
  };

  const scroll = {
    x: Math.round(window.scrollX || 0),
    y: Math.round(window.scrollY || 0),
    max_x: Math.max(0, Math.round(document.documentElement.scrollWidth - viewport.width)),
    max_y: Math.max(0, Math.round(document.documentElement.scrollHeight - viewport.height))
  };

  function cleanText(value, limit = 220) {
    if (value === null || value === undefined) return "";
    return String(value)
      .replace(/[\u200B-\u200D\uFEFF\u2800]+/g, " ")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, limit);
  }

  function isVisible(el) {
    if (!el || !el.isConnected) return false;
    const style = window.getComputedStyle(el);
    if (style.visibility === "hidden" || style.display === "none" || Number(style.opacity) === 0) {
      return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function inViewport(rect) {
    return rect.bottom >= 0 && rect.right >= 0 && rect.top <= viewport.height && rect.left <= viewport.width;
  }

  function implicitRole(el) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (tag === "button") return "button";
    if (tag === "a" && el.getAttribute("href")) return "link";
    if (tag === "select") return "combobox";
    if (tag === "textarea") return "textbox";
    if (tag === "input") {
      if (["button", "submit", "reset"].includes(type)) return "button";
      if (["checkbox"].includes(type)) return "checkbox";
      if (["radio"].includes(type)) return "radio";
      if (["range"].includes(type)) return "slider";
      return "textbox";
    }
    if (/^h[1-6]$/.test(tag)) return "heading";
    if (tag === "summary") return "button";
    return el.getAttribute("role") || null;
  }

  function isInteractive(el) {
    const tag = el.tagName.toLowerCase();
    const role = implicitRole(el);
    if (["button", "link", "textbox", "checkbox", "radio", "combobox", "slider", "menuitem", "tab"].includes(role)) {
      return true;
    }
    if (["button", "a", "input", "textarea", "select", "summary", "option"].includes(tag)) return true;
    if (el.hasAttribute("contenteditable")) return true;
    const tabIndex = el.getAttribute("tabindex");
    if (tabIndex !== null && Number(tabIndex) >= 0) return true;
    if (typeof el.onclick === "function") return true;
    return false;
  }

  function isSemanticContainer(el) {
    const tag = el.tagName.toLowerCase();
    if (!["article", "aside", "section", "form"].includes(tag)) return false;
    const label = containerLabel(el, tag, implicitRole(el));
    if (!label) return false;
    const text = cleanText(el.innerText || el.textContent || "", 180);
    return text && text.length > label.length;
  }

  function safeHref(el) {
    const href = el.getAttribute("href");
    if (!href) return null;
    try {
      const url = new URL(href, window.location.href);
      return `${url.origin}${url.pathname}`.slice(0, 180);
    } catch (_) {
      return href.slice(0, 120);
    }
  }

  function containerLabel(el, tag, role) {
    if (role === "heading") return cleanText(el.innerText || el.textContent || "", 80);
    const explicit = cleanText(el.getAttribute("aria-label") || el.getAttribute("title"), 80);
    if (explicit) return explicit;
    if (tag === "article" || tag === "aside" || tag === "form" || currentLooksLikeItem(el)) {
      const heading = el.querySelector("h1,h2,h3,h4,h5,h6,[role='heading']");
      const headingText = heading ? cleanText(heading.innerText || heading.textContent || "", 80) : "";
      if (headingText) return headingText;
      return cleanText(el.innerText || el.textContent || "", 100);
    }
    return "";
  }

  function currentLooksLikeItem(el) {
    return el.getAttribute("role") === "listitem" ||
      /\b(card|item|product|result|row)\b/i.test(el.className || "");
  }

  function parentChain(el) {
    const chain = [];
    let current = el.parentElement;
    while (current && chain.length < 4) {
      const role = implicitRole(current);
      const tag = current.tagName.toLowerCase();
      const label = containerLabel(current, tag, role);
      chain.push(label ? `${tag}:${label}` : tag);
      current = current.parentElement;
    }
    return chain;
  }

  function accessibleName(el) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (tag === "input" && ["password"].includes(type)) return "";
    const aria = el.getAttribute("aria-label");
    if (aria) return cleanText(aria, 160);
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const text = labelledBy.split(/\s+/).map((id) => document.getElementById(id)?.innerText || "").join(" ");
      if (cleanText(text)) return cleanText(text, 160);
    }
    if (el.getAttribute("alt")) return cleanText(el.getAttribute("alt"), 160);
    if (el.getAttribute("title")) return cleanText(el.getAttribute("title"), 160);
    if (tag === "input" && ["button", "submit", "reset"].includes(type)) {
      return cleanText(el.getAttribute("value"), 160);
    }
    return cleanText(el.innerText || el.textContent || el.getAttribute("value") || "", 160);
  }

  function elementRecord(el, ref) {
    const rect = el.getBoundingClientRect();
    const tag = el.tagName.toLowerCase();
    const role = implicitRole(el);
    const type = (el.getAttribute("type") || "").toLowerCase() || null;
    const isPassword = tag === "input" && type === "password";
    let text = isPassword ? "[redacted-password-field]" : cleanText(el.innerText || el.textContent || "", 220);
    if (tag === "input" || tag === "textarea") {
      text = isPassword ? "[redacted-password-field]" : cleanText(el.value || "", 120);
    }
    try {
      el.setAttribute("data-ai-browser-ref", ref);
    } catch (_) {}
    return {
      ref,
      role,
      tag,
      name: accessibleName(el) || null,
      text: text || null,
      placeholder: isPassword ? null : cleanText(el.getAttribute("placeholder"), 120) || null,
      aria_label: cleanText(el.getAttribute("aria-label"), 120) || null,
      title: cleanText(el.getAttribute("title"), 120) || null,
      input_type: type,
      href: safeHref(el),
      bbox: {
        x: Math.round(rect.left),
        y: Math.round(rect.top),
        width: Math.round(rect.width),
        height: Math.round(rect.height)
      },
      visible: isVisible(el),
      in_viewport: inViewport(rect),
      enabled: !(el.disabled || el.getAttribute("aria-disabled") === "true"),
      focused: document.activeElement === el,
      checked: typeof el.checked === "boolean" ? el.checked : null,
      expanded: el.getAttribute("aria-expanded") === null ? null : el.getAttribute("aria-expanded") === "true",
      parent_chain: parentChain(el),
      signature: [
        tag,
        role || "",
        accessibleName(el) || "",
        cleanText(text, 80),
        safeHref(el) || "",
        parentChain(el).join("/")
      ].join("|")
    };
  }

  function priority(el) {
    const role = implicitRole(el) || "";
    const rect = el.getBoundingClientRect();
    let score = 0;
    if (isInteractive(el)) score += 1000;
    if (inViewport(rect)) score += 500;
    if (["button", "link", "textbox", "combobox"].includes(role)) score += 120;
    if (document.activeElement === el) score += 80;
    const area = Math.min(80, (rect.width * rect.height) / 1000);
    score += area;
    score -= Math.max(0, Math.abs(rect.top) - viewport.height) / 20;
    return score;
  }

  const all = Array.from(document.querySelectorAll("*"));
  const visibleElements = all.filter(isVisible);
  const interactive = visibleElements.filter(isInteractive);
  const semanticContainers = visibleElements.filter(isSemanticContainer);
  const headingLike = visibleElements.filter((el) => {
    const role = implicitRole(el);
    const tag = el.tagName.toLowerCase();
    return role === "heading" || /^h[1-6]$/.test(tag);
  });

  let candidates = [...new Set([...interactive, ...semanticContainers])];
  if (mode === "full_light") {
    candidates = [...new Set([...interactive, ...semanticContainers, ...headingLike])];
  } else if (mode === "focused" && document.activeElement) {
    const container = document.activeElement.closest("form, dialog, [role='dialog'], main, section, article") || document.body;
    candidates = Array.from(container.querySelectorAll("*")).filter((el) => isVisible(el) && isInteractive(el));
  } else if (mode === "visible") {
    const viewportCandidates = candidates.filter((el) => inViewport(el.getBoundingClientRect()));
    if (viewportCandidates.length) candidates = viewportCandidates;
  }
  const candidateCountBeforeSlice = candidates.length;
  candidates = candidates
    .sort((a, b) => priority(b) - priority(a))
    .slice(0, maxElements);

  const elements = candidates.map((el, index) => elementRecord(el, `${refPrefix || ""}e${index + 1}`));

  const textNodes = visibleElements
    .filter((el) => {
      const tag = el.tagName.toLowerCase();
      if (["script", "style", "svg", "path", "noscript"].includes(tag)) return false;
      if (isInteractive(el)) return false;
      const text = cleanText(el.innerText || el.textContent || "", 260);
      if (!text || text.length < 18) return false;
      const rect = el.getBoundingClientRect();
      if (mode !== "full_light" && !inViewport(rect)) return false;
      return true;
    })
    .map((el) => cleanText(el.innerText || el.textContent || "", 260))
    .filter((text, index, arr) => arr.indexOf(text) === index)
    .slice(0, maxTextChunks);

  const hiddenSuspicious = all
    .filter((el) => !isVisible(el))
    .map((el) => cleanText(el.textContent || "", 300))
    .filter((text) => text && suspiciousPatterns.some((pattern) => pattern.test(text)))
    .slice(0, 8);

  const modalHints = visibleElements
    .filter((el) => {
      const role = el.getAttribute("role");
      const style = window.getComputedStyle(el);
      const z = Number(style.zIndex || 0);
      return role === "dialog" || role === "alertdialog" || (style.position === "fixed" && z > 10);
    })
    .map((el) => cleanText(el.getAttribute("aria-label") || el.innerText || el.textContent || el.tagName, 140))
    .filter(Boolean)
    .slice(0, 8);

  const stats = {
    links: all.filter((el) => el.tagName.toLowerCase() === "a" && el.getAttribute("href")).length,
    buttons: all.filter((el) => implicitRole(el) === "button").length,
    inputs: all.filter((el) => ["input", "textarea", "select"].includes(el.tagName.toLowerCase())).length,
    forms: all.filter((el) => el.tagName.toLowerCase() === "form").length,
    iframes: all.filter((el) => el.tagName.toLowerCase() === "iframe").length,
    modals: modalHints.length,
    text_length: cleanText(document.body?.innerText || "", 1000000).length,
    hidden_suspicious_nodes: hiddenSuspicious.length
  };

  const securityWarnings = hiddenSuspicious.map((text) => `Hidden suspicious page text: ${text.slice(0, 160)}`);

  return {
    url: window.location.href,
    title: document.title || "",
    viewport,
    scroll,
    stats,
    elements,
    text_chunks: textNodes,
    modal_hints: modalHints,
    security_warnings: securityWarnings,
    truncated: candidateCountBeforeSlice > elements.length || interactive.length > maxElements
  };
}
"""


class SnapshotEngine:
    def __init__(self, *, max_elements: int = 160, max_text_chunks: int = 40) -> None:
        self.max_elements = max_elements
        self.max_text_chunks = max_text_chunks
        self.ref_map: dict[str, ElementRef] = {}

    async def snapshot(
        self,
        page: Any,
        mode: SnapshotMode = SnapshotMode.visible,
        tabs: list[BrowserTab] | None = None,
    ) -> BrowserState:
        frame_payloads = await self._collect_frame_payloads(page, mode)
        raw = frame_payloads[0] if frame_payloads else _empty_payload(page)
        elements: list[ElementRef] = []
        text_chunks: list[str] = []
        modal_hints: list[str] = []
        security_warnings: list[str] = []
        stats = PageStats()
        truncated = False

        for payload in frame_payloads:
            frame_index = payload.get("_frame_index", 0)
            frame_url = payload.get("_frame_url")
            frame_name = payload.get("_frame_name")
            elements.extend(
                self._parse_element(item, frame_index=frame_index, frame_url=frame_url, frame_name=frame_name)
                for item in payload.get("elements", [])
            )
            prefix = "" if frame_index == 0 else f"[frame {frame_index}] "
            text_chunks.extend(f"{prefix}{chunk}" for chunk in payload.get("text_chunks", []))
            modal_hints.extend(f"{prefix}{hint}" for hint in payload.get("modal_hints", []))
            security_warnings.extend(payload.get("security_warnings", []))
            payload_stats = PageStats(**payload.get("stats", {}))
            stats.links += payload_stats.links
            stats.buttons += payload_stats.buttons
            stats.inputs += payload_stats.inputs
            stats.forms += payload_stats.forms
            stats.iframes += payload_stats.iframes
            stats.modals += payload_stats.modals
            stats.text_length += payload_stats.text_length
            stats.hidden_suspicious_nodes += payload_stats.hidden_suspicious_nodes
            truncated = truncated or bool(payload.get("truncated", False))

        elements = elements[: self.max_elements]
        text_chunks = _dedupe(text_chunks)[: self.max_text_chunks]
        modal_hints = _dedupe(modal_hints)[:12]
        security_warnings = _dedupe(security_warnings)[:12]
        self.ref_map = {element.ref: element for element in elements}
        fingerprint = self._fingerprint(raw, elements, text_chunks)
        return BrowserState(
            url=raw.get("url", ""),
            title=raw.get("title", ""),
            mode=mode,
            viewport=raw.get("viewport", {"width": 0, "height": 0}),
            scroll=ScrollState(**raw.get("scroll", {})),
            stats=stats,
            elements=elements,
            text_chunks=text_chunks,
            tabs=tabs or [],
            modal_hints=modal_hints,
            security_warnings=security_warnings,
            truncated=truncated or len(elements) > self.max_elements,
            fingerprint=fingerprint,
        )

    async def _collect_frame_payloads(self, page: Any, mode: SnapshotMode) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        frames = list(getattr(page, "frames", []) or [])
        for index, frame in enumerate(frames):
            frame_limit = self.max_elements if index == 0 else min(60, self.max_elements)
            text_limit = self.max_text_chunks if index == 0 else min(16, self.max_text_chunks)
            try:
                payload = await frame.evaluate(
                    DOM_SNAPSHOT_SCRIPT,
                    {
                        "mode": mode.value,
                        "maxElements": frame_limit,
                        "maxTextChunks": text_limit,
                        "refPrefix": "" if index == 0 else f"f{index}:",
                    },
                )
            except Exception:
                continue
            payload["_frame_index"] = index
            payload["_frame_url"] = getattr(frame, "url", None)
            payload["_frame_name"] = getattr(frame, "name", None)
            payloads.append(payload)
        return payloads

    def _parse_element(
        self,
        item: dict[str, Any],
        *,
        frame_index: int = 0,
        frame_url: str | None = None,
        frame_name: str | None = None,
    ) -> ElementRef:
        signature = item.pop("signature", "")
        bbox = item.get("bbox")
        if bbox:
            item["bbox"] = BoundingBox(**bbox)
        item["frame_index"] = frame_index
        item["frame_url"] = frame_url
        item["frame_name"] = frame_name
        item["signature_hash"] = hashlib.sha1(signature.encode("utf-8")).hexdigest()[:16]
        return ElementRef(**item)

    def _fingerprint(self, raw: dict[str, Any], elements: list[ElementRef], text_chunks: list[str]) -> str:
        payload = {
            "url": raw.get("url", "").split("#")[0],
            "title": raw.get("title", ""),
            "scroll": raw.get("scroll", {}),
            "elements": [
                [element.frame_index, element.role, element.tag, element.name, element.text, element.href]
                for element in elements[:40]
            ],
            "text": text_chunks[:12],
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True)
        return hashlib.sha1(encoded.encode("utf-8")).hexdigest()[:20]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _empty_payload(page: Any) -> dict[str, Any]:
    return {
        "url": getattr(page, "url", ""),
        "title": "",
        "viewport": {"width": 0, "height": 0},
        "scroll": {},
        "stats": {},
        "elements": [],
        "text_chunks": [],
        "modal_hints": [],
        "security_warnings": [],
        "truncated": False,
    }
