from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = [ROOT / "ai_browser_agent"]
EXCLUDED_PARTS = {
    "evals",
    "__pycache__",
}

PATTERNS = {
    "real domain yandex": re.compile(r"\byandex\b|яндекс", re.I),
    "real domain hh": re.compile(r"\bhh\.ru\b", re.I),
    "real service lavka": re.compile(r"\blavka\b|лавка", re.I),
    "real service delivery club": re.compile(r"delivery\s+club", re.I),
    "site-specific selector": re.compile(r"data-qa|data-testid|/vacancies|vacancy", re.I),
    "demo food item": re.compile(r"\bbbq\b|\bburger\b", re.I),
}


def should_scan(path: Path) -> bool:
    if path.suffix != ".py":
        return False
    rel = path.relative_to(ROOT)
    return not any(part in EXCLUDED_PARTS for part in rel.parts)


def main() -> int:
    findings: list[str] = []
    for directory in SCAN_DIRS:
        for path in directory.rglob("*.py"):
            if not should_scan(path):
                continue
            text = path.read_text(encoding="utf-8")
            for name, pattern in PATTERNS.items():
                for match in pattern.finditer(text):
                    line = text.count("\n", 0, match.start()) + 1
                    findings.append(f"{path.relative_to(ROOT)}:{line}: {name}: {match.group(0)!r}")
    if findings:
        print("Hardcoded flow audit failed:")
        print("\n".join(findings))
        return 1
    print("No hardcoded real-site flows found in agent source.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

