from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from ai_browser_agent.agent.cascade import ModelCascade
from ai_browser_agent.agent.context import ContextManager
from ai_browser_agent.agent.core import AgentCore
from ai_browser_agent.agent.tools import ToolDispatcher
from ai_browser_agent.browser.controller import BrowserController
from ai_browser_agent.config import AgentConfig, missing_provider_keys
from ai_browser_agent.llm.anthropic_client import AnthropicToolClient
from ai_browser_agent.llm.base import FakeLLMClient, LLMClient
from ai_browser_agent.llm.openai_compatible_client import OpenAICompatibleChatClient
from ai_browser_agent.llm.openai_client import OpenAIResponsesToolClient
from ai_browser_agent.observability.artifacts import ArtifactManager
from ai_browser_agent.observability.logger import RunLogger, replay_events
from ai_browser_agent.safety.classifier import SecurityLayer


def main(argv: list[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return
    try:
        result = args.handler(args)
        if asyncio.iscoroutine(result):
            asyncio.run(result)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        raise SystemExit(130)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-browser-agent")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="Run one autonomous browser task.")
    run.add_argument("--task", "-t", help="Task text. If omitted, read from terminal.")
    _common_run_args(run)
    run.set_defaults(handler=_run_command)

    interactive = sub.add_parser("interactive", help="Run tasks from a terminal prompt.")
    _common_run_args(interactive)
    interactive.set_defaults(handler=_interactive_command)

    profile = sub.add_parser("profile", help="Profile management.")
    profile_sub = profile.add_subparsers(dest="profile_command")
    login = profile_sub.add_parser("login", help="Open a persistent profile for manual login.")
    login.add_argument("--profile", "--profile-dir", dest="profile_dir", default=None)
    login.add_argument("--url", required=True)
    login.add_argument("--headless", action="store_true")
    login.add_argument("--browser-channel", default=None, help="Browser channel, e.g. chrome or msedge.")
    login.add_argument("--cdp-url", default=None, help="Connect to an already-running Chrome via CDP.")
    login.set_defaults(handler=_profile_login_command)

    replay = sub.add_parser("replay", help="Print a saved run timeline.")
    replay.add_argument("run_dir", type=Path)
    replay.set_defaults(handler=_replay_command)

    doctor = sub.add_parser("doctor", help="Check environment and browser availability.")
    doctor.add_argument("--provider", choices=["anthropic", "openai", "kimi", "fake"], default=None)
    doctor.add_argument("--skip-browser", action="store_true")
    doctor.add_argument("--headless", action="store_true", help="Use headless browser for the doctor check.")
    doctor.set_defaults(handler=_doctor_command)
    return parser


def _common_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", choices=["anthropic", "openai", "kimi", "fake"], default=None)
    parser.add_argument("--profile-dir", type=Path, default=None)
    parser.add_argument("--runs-dir", type=Path, default=None)
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--browser-channel", default=None, help="Browser channel, e.g. chrome or msedge.")
    parser.add_argument("--cdp-url", default=None, help="Connect to an already-running Chrome via CDP.")
    parser.add_argument("--record-video", action="store_true")
    parser.add_argument("--trace", action="store_true")
    parser.add_argument(
        "--auto-approve-risky",
        action="store_true",
        help="Test-only: auto-approve high-risk confirmations. Critical handoffs still pause.",
    )


async def _run_command(args: argparse.Namespace) -> None:
    task = args.task or input("Task: ").strip()
    if not task:
        raise SystemExit("Task is required.")
    config = _config_from_args(args)
    await _run_one_task(task, config, auto_approve_risky=args.auto_approve_risky)


async def _interactive_command(args: argparse.Namespace) -> None:
    config = _config_from_args(args)
    while True:
        task = input("Task (empty to quit): ").strip()
        if not task:
            return
        await _run_one_task(task, config, auto_approve_risky=args.auto_approve_risky)


async def _run_one_task(task: str, config: AgentConfig, *, auto_approve_risky: bool) -> None:
    missing = missing_provider_keys(config.provider)
    if missing:
        raise SystemExit(
            f"Missing required env var(s) for provider {config.provider}: {', '.join(missing)}"
        )

    artifacts = ArtifactManager(config.runs_dir)
    run_id, run_dir = artifacts.create_run_dir(task)
    logger = RunLogger(run_id, run_dir)
    browser = BrowserController(
        viewport={"width": config.viewport_width, "height": config.viewport_height},
        artifacts_dir=run_dir,
    )
    llm = _build_client(config)
    cascade = ModelCascade(client=llm, config=config)
    tools = ToolDispatcher(
        browser=browser,
        safety=SecurityLayer(),
        ask_user=_terminal_ask,
        auto_approve_risky=auto_approve_risky,
        on_safety=lambda event: logger.event("safety", **event),
    )
    core = AgentCore(
        config=config,
        run_id=run_id,
        run_dir=run_dir,
        cascade=cascade,
        tools=tools,
        logger=logger,
        context=ContextManager(budget_tokens=config.context_budget_tokens),
    )
    await browser.launch(
        config.profile_dir,
        headless=config.headless,
        browser_channel=config.browser_channel,
        cdp_url=config.cdp_url,
        record_video=config.record_video,
        trace=config.trace,
    )
    try:
        final = await core.run_task(task)
        print(f"\nRun directory: {run_dir}")
        print(f"Success: {final.success}")
        print(final.summary)
    finally:
        await browser.close()


def _config_from_args(args: argparse.Namespace) -> AgentConfig:
    config = AgentConfig.from_env()
    return config.with_overrides(
        provider=args.provider,
        profile_dir=args.profile_dir,
        runs_dir=args.runs_dir,
        max_steps=args.max_steps,
        headless=True if args.headless else None,
        browser_channel=args.browser_channel,
        cdp_url=args.cdp_url,
        record_video=True if args.record_video else None,
        trace=True if args.trace else None,
    )


def _build_client(config: AgentConfig) -> LLMClient:
    provider = config.provider
    if provider == "anthropic":
        return AnthropicToolClient(api_key=os.getenv("ANTHROPIC_API_KEY"))
    if provider == "openai":
        return OpenAIResponsesToolClient(api_key=os.getenv("OPENAI_API_KEY"))
    if provider == "kimi":
        return OpenAICompatibleChatClient(
            api_key=os.getenv("KIMI_API_KEY"),
            base_url=config.kimi_base_url,
            provider="kimi",
        )
    if provider == "fake":
        return FakeLLMClient()
    raise ValueError(f"Unsupported provider {provider!r}.")


async def _profile_login_command(args: argparse.Namespace) -> None:
    config = AgentConfig.from_env()
    profile_dir = Path(args.profile_dir) if args.profile_dir else config.profile_dir
    browser = BrowserController(
        viewport={"width": config.viewport_width, "height": config.viewport_height},
        artifacts_dir=config.runs_dir / "profile-login",
    )
    await browser.launch(
        profile_dir,
        headless=args.headless,
        browser_channel=args.browser_channel,
        cdp_url=args.cdp_url,
    )
    try:
        await browser.navigate(args.url)
        input(
            f"Persistent profile is open at {args.url}.\n"
            f"Log in manually if needed, then press Enter to close the browser.\n"
        )
    finally:
        await browser.close()


def _replay_command(args: argparse.Namespace) -> None:
    for event in replay_events(args.run_dir):
        step = event.get("step")
        event_type = event.get("type")
        if event_type == "tool_call":
            print(f"[{step}] CALL {event.get('tool')} {event.get('args')}")
        elif event_type == "tool_result":
            print(f"[{step}] RESULT ok={event.get('ok')} {event.get('summary')}")
        elif event_type == "final":
            print(f"[{step}] FINAL success={event.get('success')} {event.get('summary')}")
        else:
            print(f"[{step}] {event_type} {event}")


async def _doctor_command(args: argparse.Namespace) -> None:
    config = AgentConfig.from_env().with_overrides(provider=args.provider)
    checks: list[tuple[str, bool, str]] = []
    version_ok = (3, 11) <= sys.version_info[:2] < (3, 14)
    checks.append(("python", version_ok, sys.version.split()[0]))

    for module in ["pydantic", "rich", "playwright", "anthropic", "openai", "tenacity", "dotenv"]:
        try:
            __import__(module)
            checks.append((module, True, "import ok"))
        except Exception as exc:
            checks.append((module, False, f"{type(exc).__name__}: {exc}"))

    missing = missing_provider_keys(config.provider)
    checks.append((f"{config.provider} credentials", not missing, ", ".join(missing) or "configured"))
    if config.provider == "fake":
        checks.append(
            (
                "demo provider",
                False,
                "Use Anthropic, OpenAI, or Kimi for the demo; fake is only for local plumbing checks.",
            )
        )

    if not args.skip_browser:
        try:
            browser = BrowserController(artifacts_dir=config.runs_dir / "doctor")
            await browser.launch(
                config.profile_dir,
                headless=args.headless,
                browser_channel=config.browser_channel,
                cdp_url=config.cdp_url,
            )
            await browser.navigate("about:blank")
            await browser.close()
            checks.append(("chromium", True, "launch ok"))
        except Exception as exc:
            checks.append(
                (
                    "chromium",
                    False,
                    f"{type(exc).__name__}: {exc}. Run `python -m playwright install chromium`.",
                )
            )

    width = max(len(name) for name, _, _ in checks)
    failed = False
    for name, ok, detail in checks:
        failed = failed or not ok
        status = "OK" if ok else "FAIL"
        print(f"{name:<{width}}  {status:<4}  {detail}")
    if failed and config.provider in {"anthropic", "openai", "kimi"}:
        raise SystemExit(1)


def _terminal_ask(prompt: str) -> str:
    return input(prompt)


if __name__ == "__main__":
    main()
