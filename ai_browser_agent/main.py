"""Main entry point for the AI Browser Automation Agent.

Wires together all components (BrowserController, KimiClient, ContextManager,
SecurityLayer, ErrorHandler, SubAgentOrchestrator, AgentCore) and provides
the CLI with interactive, single-task, and demo modes.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

# ---------------------------------------------------------------------------
# Component imports
# ---------------------------------------------------------------------------

from browser_controller import BrowserController
from kimi_client import KimiClient
from context_manager import ContextManager
from security import SecurityLayer
from error_handler import ErrorHandler
from sub_agents import SubAgentOrchestrator
from agent_core import AgentCore
from models import AgentResult, BrowserState
from cli import AgentCLI

# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------

_DEFAULT_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
_DEFAULT_MODEL = "qwen3.6-max-preview"


def _resolve_api_key(cli_api_key: str | None) -> str | None:
    """Resolve API key from CLI or environment."""
    return (
        cli_api_key
        or os.environ.get("LLM_API_KEY")
        or os.environ.get("KIMI_API_KEY")
    )


def _resolve_base_url() -> str:
    """Resolve base URL from environment with sane default."""
    return (
        os.environ.get("LLM_BASE_URL")
        or os.environ.get("KIMI_BASE_URL")
        or _DEFAULT_BASE_URL
    )


def _resolve_model() -> str:
    """Resolve model from environment with sane default."""
    return (
        os.environ.get("LLM_MODEL")
        or os.environ.get("KIMI_MODEL")
        or _DEFAULT_MODEL
    )


def _parse_bool_env(value: str | None, default: bool = False) -> bool:
    """Parse a boolean environment variable."""
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _resolve_headless(cli_headless: bool) -> bool:
    """Resolve headless flag: CLI overrides env when set true."""
    if cli_headless:
        return True
    return _parse_bool_env(os.environ.get("BROWSER_HEADLESS"), default=False)


def _resolve_profile_dir(cli_profile_dir: str | None) -> str | None:
    """Resolve profile directory from CLI or env."""
    return cli_profile_dir or os.environ.get("BROWSER_PROFILE_DIR")

# ---------------------------------------------------------------------------
# Demo task
# ---------------------------------------------------------------------------

DEMO_TASK = (
    "Navigate to https://en.wikipedia.org/wiki/Main_Page, find the search box, "
    "type 'Artificial intelligence', press Enter, and tell me the first paragraph "
    "of the article."
)


# ===========================================================================
# Agent factory
# ===========================================================================

def create_agent(
    profile_dir: str | None = None,
    headless: bool = False,
    api_key: str | None = None,
) -> AgentCore:
    """Create and wire all agent components.

    Args:
        profile_dir: Path to a persistent browser profile directory.
            If None, an ephemeral browser context is used.
        headless: Launch the browser without a visible window.
        api_key: API key. If None, reads from ``LLM_API_KEY`` env var
            (fallback: ``KIMI_API_KEY``).

    Returns:
        A fully initialised :class:`AgentCore` ready to run tasks.
    """
    # Resolve API key
    resolved_key = _resolve_api_key(api_key)
    resolved_base_url = _resolve_base_url()
    resolved_model = _resolve_model()
    if not resolved_key:
        raise RuntimeError(
            "No API key available. Set LLM_API_KEY (or KIMI_API_KEY) "
            "environment variable or pass --api-key."
        )

    # Browser
    browser = BrowserController()

    # LLM client
    llm = KimiClient(
        api_key=resolved_key,
        base_url=resolved_base_url,
        model=resolved_model,
    )

    # Context manager
    context = ContextManager(max_tokens=6000, model=resolved_model)
    context.set_llm_client(llm)

    # Security layer
    security = SecurityLayer(auto_approve=["scroll", "find_information", "done"])

    # Error handler
    error_handler = ErrorHandler(max_retries=3)

    # Sub-agent orchestrator
    sub_agents = SubAgentOrchestrator(llm)

    # Main agent
    agent = AgentCore(
        browser=browser,
        llm=llm,
        context=context,
        security=security,
        error_handler=error_handler,
        sub_agents=sub_agents,
        max_steps=50,
    )

    # Attach profile dir / headless so the runner can launch the browser
    agent._profile_dir = profile_dir  # type: ignore[attr-defined]
    agent._headless = headless  # type: ignore[attr-defined]

    return agent


# ===========================================================================
# Async helpers
# ===========================================================================

async def _launch_browser(agent: AgentCore) -> None:
    """Launch the browser controller using settings attached to *agent*."""
    profile_dir = getattr(agent, "_profile_dir", None)
    headless = getattr(agent, "_headless", False)
    await agent.browser.launch(
        headless=headless,
        user_data_dir=profile_dir,
    )


async def _close_browser(agent: AgentCore) -> None:
    """Safely shut down the browser controller."""
    try:
        await agent.browser.close()
    except Exception:
        pass


# ===========================================================================
# Mode implementations
# ===========================================================================

async def run_interactive(agent: AgentCore) -> None:
    """Run the agent in interactive mode.

    Displays a banner, then loops accepting user tasks and commands:
    ``/quit``, ``/help``, ``/screenshot``, ``/state``, ``/reset``.
    """
    cli = AgentCLI()
    console = cli.console

    cli.display_banner()
    cli.display_help()

    await _launch_browser(agent)
    cli.display_success("Browser launched (visible mode).")

    try:
        while True:
            user_input = cli.get_user_input("You")

            if not user_input:
                continue

            # Commands
            if user_input.lower() in ("/quit", "/q", "quit", "exit"):
                cli.display_info("Shutting down …")
                break

            if user_input.lower() in ("/help", "/h", "help"):
                cli.display_help()
                continue

            if user_input.lower() in ("/screenshot", "/ss"):
                await _cmd_screenshot(agent, cli)
                continue

            if user_input.lower() in ("/state", "/st"):
                await _cmd_state(agent, cli)
                continue

            if user_input.lower() in ("/reset", "/r"):
                agent.context.clear()
                cli.display_success("Agent context and history reset.")
                continue

            # Normal task
            await _run_task_with_ui(agent, user_input, cli)

    finally:
        await _close_browser(agent)
        cli.display_info("Browser closed. Goodbye!")


async def run_single_task(agent: AgentCore, task: str) -> AgentResult:
    """Run a single task and display the result.

    Args:
        agent: The agent core to use.
        task: The task description string.

    Returns:
        The :class:`AgentResult` from execution.
    """
    cli = AgentCLI()
    await _launch_browser(agent)
    try:
        result = await _run_task_with_ui(agent, task, cli, show_intermediate=False)
        return result
    finally:
        await _close_browser(agent)


async def run_demo(agent: AgentCore) -> AgentResult:
    """Run the built-in demo task.

    Args:
        agent: The agent core to use.

    Returns:
        The :class:`AgentResult` from execution.
    """
    cli = AgentCLI()
    cli.display_banner()
    cli.display_info(f"Demo task: {DEMO_TASK}")

    await _launch_browser(agent)
    try:
        result = await _run_task_with_ui(agent, DEMO_TASK, cli, show_intermediate=True)
        return result
    finally:
        await _close_browser(agent)


# ===========================================================================
# Internal command handlers
# ===========================================================================

async def _cmd_screenshot(agent: AgentCore, cli: AgentCLI) -> None:
    """Handle the ``/screenshot`` command."""
    try:
        screenshot_bytes = await agent.browser.get_screenshot(full_page=False)
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        filename = f"screenshot_{timestamp}.png"
        Path(filename).write_bytes(screenshot_bytes)
        cli.display_success(f"Screenshot saved to {filename} ({len(screenshot_bytes)} bytes)")
    except Exception as exc:
        cli.display_error(f"Screenshot failed: {exc}")


async def _cmd_state(agent: AgentCore, cli: AgentCLI) -> None:
    """Handle the ``/state`` command."""
    try:
        url = await agent.browser.get_current_url()
        title = await agent.browser.get_page_title()
        distilled_dom = await agent.browser.get_distilled_dom()
        state = BrowserState(
            url=url,
            title=title,
            distilled_dom=distilled_dom,
        )
        cli.display_browser_state(state)
    except Exception as exc:
        cli.display_error(f"Failed to get browser state: {exc}")


# ===========================================================================
# Task execution with UI
# ===========================================================================

async def _run_task_with_ui(
    agent: AgentCore,
    task: str,
    cli: AgentCLI,
    show_intermediate: bool = True,
) -> AgentResult:
    """Execute *task* via *agent* while rendering a live UI.

    Shows a spinner while the agent works, then renders all steps and
    the final result.

    Args:
        agent: The agent core.
        task: Task description.
        cli: The CLI renderer.
        show_intermediate: Whether to print each step after completion.

    Returns:
        The :class:`AgentResult` produced by the agent.
    """
    console = cli.console

    # Live spinner during execution
    spinner_text = Text(f"Working on: {task[:80]} …", style="dim italic")
    spinner = Spinner("dots", text=spinner_text, style="bright_blue")

    result: AgentResult | None = None

    with Live(spinner, console=console, refresh_per_second=10, transient=True):
        try:
            result = await agent.run_task(task)
        except Exception as exc:
            # Spinner is cleared by Live context manager
            cli.display_error(f"Task execution failed: {exc}")
            return AgentResult(
                success=False,
                task=task,
                final_answer=f"Error: {exc}",
            )

    # Render result (spinner cleared)
    if result is None:
        cli.display_error("No result returned from agent.")
        return AgentResult(success=False, task=task)

    if show_intermediate and result.steps:
        console.print(f"\n[bold bright_white]─ Task: {task[:100]}[/bold bright_white]\n")
        for step in result.steps:
            cli.display_step(step)

    cli.display_final_result(result)
    return result


# ===========================================================================
# Entry point
# ===========================================================================

def main() -> None:
    """Parse CLI arguments and dispatch to the appropriate run mode."""
    # Load .env first so that env vars are available for defaults
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="AI Browser Agent — autonomous web automation powered by Qwen",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                           # Interactive mode
  %(prog)s --task "Go to wikipedia and search for Python"
  %(prog)s --demo                    # Run the built-in demo
  %(prog)s --profile-dir ./profile   # Use persistent browser profile
        """,
    )
    parser.add_argument(
        "--task",
        "-t",
        help="Single task to run (non-interactive mode)",
    )
    parser.add_argument(
        "--profile-dir",
        "-p",
        help="Persistent browser profile directory (or set BROWSER_PROFILE_DIR)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode (or set BROWSER_HEADLESS=true)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run the built-in demo task",
    )
    parser.add_argument(
        "--api-key",
        help="LLM API key (or set LLM_API_KEY env var)",
    )

    args = parser.parse_args()

    # Validate API key presence
    api_key = _resolve_api_key(args.api_key)
    if not api_key:
        print(
            "Error: No API key found.\n"
            "  Set LLM_API_KEY environment variable (or KIMI_API_KEY), create a .env file,\n"
            "  or pass --api-key."
        )
        sys.exit(1)

    # Create agent
    try:
        agent = create_agent(
            profile_dir=_resolve_profile_dir(args.profile_dir),
            headless=_resolve_headless(args.headless),
            api_key=api_key,
        )
    except Exception as exc:
        print(f"Failed to create agent: {exc}")
        sys.exit(1)

    # Dispatch
    if args.demo:
        result = asyncio.run(run_demo(agent))
        sys.exit(0 if result.success else 1)

    if args.task:
        result = asyncio.run(run_single_task(agent, args.task))
        sys.exit(0 if result.success else 1)

    # Default: interactive mode
    asyncio.run(run_interactive(agent))
    sys.exit(0)


if __name__ == "__main__":
    main()
