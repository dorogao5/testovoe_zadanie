from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.pretty import Pretty
    from rich.table import Table
except Exception:  # pragma: no cover - dependency is validated by doctor
    Console = None
    Panel = None
    Pretty = None
    Table = None


class RunLogger:
    def __init__(self, run_id: str, run_dir: Path, *, quiet: bool = False) -> None:
        self.run_id = run_id
        self.run_dir = run_dir
        self.events_path = run_dir / "events.jsonl"
        self.quiet = quiet
        self.console = Console() if Console is not None and not quiet else None

    def event(self, event_type: str, *, step: int, **payload: Any) -> None:
        record = {
            "run_id": self.run_id,
            "step": step,
            "type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **payload,
        }
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._print(record)

    def _print(self, record: dict[str, Any]) -> None:
        if self.quiet:
            return
        if self.console is None:
            print(f"[{record['step']}] {record['type']}: {record}")
            return
        event_type = record["type"]
        if event_type == "tool_call":
            table = Table(title=f"Step {record['step']} tool call")
            table.add_column("Field")
            table.add_column("Value")
            table.add_row("tool", str(record.get("tool", "")))
            table.add_row("model", str(record.get("model", "")))
            table.add_row("args", json.dumps(record.get("args", {}), ensure_ascii=False, indent=2))
            self.console.print(table)
        elif event_type == "tool_result":
            style = "green" if record.get("ok") else "red"
            body = {
                "tool": record.get("tool"),
                "ok": record.get("ok"),
                "summary": record.get("summary"),
                "data": record.get("data", {}),
            }
            self.console.print(Panel(Pretty(body), title=f"Step {record['step']} result", style=style))
        elif event_type == "safety":
            self.console.print(Panel(Pretty(record), title="Safety decision", style="yellow"))
        elif event_type == "final":
            self.console.print(Panel(Pretty(record), title="Final", style="cyan"))
        else:
            self.console.print(f"[dim]{record['timestamp']}[/dim] {event_type}: {record}")

    def write_summary(self, markdown: str) -> Path:
        path = self.run_dir / "summary.md"
        path.write_text(markdown, encoding="utf-8")
        return path


def replay_events(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "events.jsonl"
    if not path.exists():
        raise FileNotFoundError(path)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

