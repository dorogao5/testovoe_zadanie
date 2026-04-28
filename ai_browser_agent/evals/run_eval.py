from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from ai_browser_agent.browser.actions import SnapshotMode
from ai_browser_agent.browser.controller import BrowserController
from ai_browser_agent.evals.fixtures.server import FixtureHandler


def main() -> None:
    parser = argparse.ArgumentParser(description="Start local fixtures and print eval commands.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--provider", default="anthropic", choices=["anthropic", "openai", "fake"])
    parser.add_argument("--run", action="store_true", help="Run the delivery fixture smoke command.")
    parser.add_argument(
        "--browser-smoke",
        action="store_true",
        help="Run deterministic browser/snapshot checks against fixtures without an LLM.",
    )
    args = parser.parse_args()

    if args.browser_smoke:
        asyncio.run(_browser_smoke(args.port))
        return

    server_cmd = [
        sys.executable,
        "-m",
        "ai_browser_agent.evals.fixtures.server",
        "--port",
        str(args.port),
    ]
    server = subprocess.Popen(server_cmd)
    try:
        time.sleep(1)
        start_url = f"http://127.0.0.1:{args.port}/delivery"
        command = [
            "ai-browser-agent",
            "run",
            "--provider",
            args.provider,
            "--task",
            f"Open {start_url}. Add the BBQ burger and French fries to the cart, go to checkout, but stop before final payment.",
        ]
        print("Fixture server started.")
        print("Command:")
        print(" ".join(command))
        if args.run:
            subprocess.check_call(command, cwd=Path.cwd())
    finally:
        server.terminate()
        server.wait(timeout=5)


async def _browser_smoke(port: int) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), FixtureHandler)
    host, actual_port = server.server_address
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    browser = BrowserController(artifacts_dir=Path("runs/eval-browser-smoke"))
    await browser.launch(Path("profiles/eval-browser-smoke"), headless=True)
    try:
        await browser.navigate(f"http://{host}:{actual_port}/delivery")
        delivery = await browser.current_state(SnapshotMode.visible)
        assert delivery.elements, "delivery fixture should expose interactives"
        add_button = browser.resolver.query("add cart", limit=1).candidates[0]
        click = await browser.click(add_button.ref)
        assert click.ok, click

        await browser.navigate(f"http://{host}:{actual_port}/dynamic")
        dynamic = await browser.current_state(SnapshotMode.full_light)
        iframe_refs = [element.ref for element in dynamic.elements if element.frame_index > 0]
        assert iframe_refs, "iframe fixture should expose frame refs"

        await browser.navigate(f"http://{host}:{actual_port}/inbox")
        inbox = await browser.current_state(SnapshotMode.visible)
        assert inbox.security_warnings, "hidden prompt-injection fixture should be detected"
        print(
            "browser smoke ok:",
            {
                "delivery_elements": len(delivery.elements),
                "iframe_refs": iframe_refs[:3],
                "security_warnings": len(inbox.security_warnings),
            },
        )
    finally:
        await browser.close()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
