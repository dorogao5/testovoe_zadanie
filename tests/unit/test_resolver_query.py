from ai_browser_agent.browser.actions import BoundingBox, ElementRef
from ai_browser_agent.browser.resolver import ElementResolver


def element(ref: str, *, name: str, role: str = "button") -> ElementRef:
    return ElementRef(
        ref=ref,
        role=role,
        tag="button",
        name=name,
        text=name,
        bbox=BoundingBox(x=0, y=0, width=100, height=30),
        signature_hash=ref,
    )


def test_query_dom_ranks_matching_ref() -> None:
    resolver = ElementResolver()
    resolver.update_ref_map(
        {
            "e1": element("e1", name="Open settings"),
            "e2": element("e2", name="Search catalog"),
        }
    )

    result = resolver.query("search", limit=3)

    assert result.candidates[0].ref == "e2"
    assert result.candidates[0].score > 0

