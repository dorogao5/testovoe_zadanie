from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path


class ArtifactManager:
    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = runs_dir

    def create_run_dir(self, task: str) -> tuple[str, Path]:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        slug = _slugify(task)[:48] or "task"
        run_id = f"{timestamp}-{slug}"
        run_dir = self.runs_dir / run_id
        (run_dir / "screenshots").mkdir(parents=True, exist_ok=True)
        return run_id, run_dir


def _slugify(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return re.sub(r"-{2,}", "-", value)

