from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class SnapshotMode(str, Enum):
    visible = "visible"
    focused = "focused"
    full_light = "full_light"
    extract = "extract"


class BoundingBox(BaseModel):
    x: float
    y: float
    width: float
    height: float

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2, self.y + self.height / 2


class ElementRef(BaseModel):
    ref: str
    frame_index: int = 0
    frame_url: str | None = None
    frame_name: str | None = None
    role: str | None = None
    tag: str
    name: str | None = None
    text: str | None = None
    placeholder: str | None = None
    aria_label: str | None = None
    title: str | None = None
    input_type: str | None = None
    href: str | None = None
    bbox: BoundingBox | None = None
    visible: bool = True
    in_viewport: bool = True
    enabled: bool = True
    focused: bool = False
    checked: bool | None = None
    expanded: bool | None = None
    parent_chain: list[str] = Field(default_factory=list)
    signature_hash: str

    def model_facing_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "ref": self.ref,
            "tag": self.tag,
        }
        if self.frame_index:
            data["frame_index"] = self.frame_index
            if self.frame_url:
                data["frame_url"] = self.frame_url
            if self.frame_name:
                data["frame_name"] = self.frame_name
        if self.role:
            data["role"] = self.role
        for key in ("name", "text", "placeholder", "aria_label", "title", "input_type", "href"):
            value = getattr(self, key)
            if value:
                limit = 140 if key in {"name", "text"} else 100
                data[key] = str(value)[:limit]
        if self.bbox:
            data["bbox"] = self.bbox.model_dump()
        if not self.visible:
            data["visible"] = False
        if not self.in_viewport:
            data["in_viewport"] = False
        if not self.enabled:
            data["enabled"] = False
        if self.focused:
            data["focused"] = True
        if self.checked is not None:
            data["checked"] = self.checked
        if self.expanded is not None:
            data["expanded"] = self.expanded
        if self.parent_chain:
            data["parent_chain"] = [item[:80] for item in self.parent_chain[:2]]
        return data


class PageStats(BaseModel):
    links: int = 0
    buttons: int = 0
    inputs: int = 0
    forms: int = 0
    iframes: int = 0
    modals: int = 0
    text_length: int = 0
    hidden_suspicious_nodes: int = 0


class ScrollState(BaseModel):
    x: int = 0
    y: int = 0
    max_x: int = 0
    max_y: int = 0


class BrowserTab(BaseModel):
    index: int
    url: str
    title: str
    active: bool = False


class BrowserState(BaseModel):
    url: str
    title: str
    mode: SnapshotMode
    viewport: dict[str, int]
    scroll: ScrollState
    stats: PageStats
    elements: list[ElementRef] = Field(default_factory=list)
    text_chunks: list[str] = Field(default_factory=list)
    tabs: list[BrowserTab] = Field(default_factory=list)
    modal_hints: list[str] = Field(default_factory=list)
    security_warnings: list[str] = Field(default_factory=list)
    truncated: bool = False
    fingerprint: str

    def to_model_summary(self, *, max_elements: int = 60, max_text_chunks: int = 12) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "mode": self.mode.value,
            "viewport": self.viewport,
            "scroll": self.scroll.model_dump(),
            "stats": self.stats.model_dump(),
            "elements": [
                element.model_facing_dict() for element in self.elements[:max_elements]
            ],
            "text_chunks": self.text_chunks[:max_text_chunks],
            "tabs": [tab.model_dump() for tab in self.tabs],
            "modal_hints": self.modal_hints,
            "security_warnings": self.security_warnings,
            "truncated": self.truncated or len(self.elements) > max_elements,
            "fingerprint": self.fingerprint,
        }


class ErrorInfo(BaseModel):
    error_class: str
    message: str
    suggested_recovery: str
    retryable: bool = True


class BrowserActionResult(BaseModel):
    ok: bool
    summary: str
    url: str | None = None
    title: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    error: ErrorInfo | None = None

    @classmethod
    def success(cls, summary: str, **data: Any) -> "BrowserActionResult":
        return cls(ok=True, summary=summary, data=data)

    @classmethod
    def failure(
        cls,
        error_class: str,
        message: str,
        suggested_recovery: str,
        *,
        retryable: bool = True,
    ) -> "BrowserActionResult":
        return cls(
            ok=False,
            summary=message,
            error=ErrorInfo(
                error_class=error_class,
                message=message,
                suggested_recovery=suggested_recovery,
                retryable=retryable,
            ),
        )


class ScreenshotArtifact(BaseModel):
    path: Path
    annotated: bool = False
    url: str | None = None
    title: str | None = None
    reason: str | None = None


class ElementCandidate(BaseModel):
    ref: str
    score: float
    evidence: str
    element: ElementRef


class ElementCandidates(BaseModel):
    query: str
    candidates: list[ElementCandidate]
    ambiguous: bool = False


class ExtractResult(BaseModel):
    query: str
    scope: Literal["visible", "page", "selected"]
    content: str
    evidence: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    structured_data: dict[str, Any] | None = None
    fingerprint: str | None = None
    cache_hit: bool = False
    uncertainty: str | None = None
