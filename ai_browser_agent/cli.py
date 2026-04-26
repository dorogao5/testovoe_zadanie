"""CLI Interface for the AI Browser Automation Agent.

A beautiful terminal UI built with Rich, providing colorful output for
agent thoughts, actions, results, browser state, and interactive prompts.
"""

from __future__ import annotations

import datetime

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.spinner import Spinner
from rich.live import Live
from rich.markdown import Markdown
from rich.prompt import Prompt
from rich.text import Text
from rich.align import Align
from rich.columns import Columns
from rich import box

from models import (
    ActionResult,
    AgentResult,
    BrowserState,
    SecurityDecision,
    Step,
    ToolCall,
)


# ---------------------------------------------------------------------------
# Color palette (mapped to Rich styles)
# ---------------------------------------------------------------------------
COLOR_THOUGHT = "bold bright_blue"
COLOR_ACTION = "bold bright_yellow"
COLOR_SUCCESS = "bold bright_green"
COLOR_ERROR = "bold bright_red"
COLOR_SECURITY = "bold black on bright_yellow"
COLOR_USER = "bold bright_cyan"
COLOR_BROWSER = "bright_magenta"
COLOR_INFO = "bright_white"
COLOR_DIM = "dim"


class AgentCLI:
    """Beautiful terminal UI for the AI Browser Agent.

    Uses Rich components to display the agent's reasoning, actions,
    browser state, and interactive security prompts in a visually
    appealing way.
    """

    def __init__(self) -> None:
        self.console = Console(
            color_system="auto",
            highlight=True,
            soft_wrap=False,
        )

    # ------------------------------------------------------------------ #
    # Banner & top-level displays
    # ------------------------------------------------------------------ #

    def display_banner(self) -> None:
        """Display the agent's startup banner."""
        banner_text = Text()
        banner_text.append("🌐  ", style="")
        banner_text.append("AI Browser Agent", style="bold bright_cyan")
        banner_text.append("  🤖\n", style="")
        banner_text.append("Powered by ", style="dim")
        banner_text.append("Kimi", style="bold bright_magenta")
        banner_text.append(" + ", style="dim")
        banner_text.append("Playwright", style="bold bright_green")
        banner_text.append(" + ", style="dim")
        banner_text.append("Rich", style="bold bright_red")
        banner_text.append("\n", style="")
        banner_text.append("─" * 50, style="dim")

        panel = Panel(
            Align.center(banner_text),
            border_style="bright_cyan",
            box=box.ROUNDED,
            padding=(1, 2),
        )
        self.console.print(panel)
        self.console.print()

    def display_help(self) -> None:
        """Display available commands and usage hints."""
        table = Table(
            title="Available Commands",
            box=box.SIMPLE_HEAD,
            header_style="bold bright_cyan",
            border_style="dim",
        )
        table.add_column("Command", style="bold bright_yellow", no_wrap=True)
        table.add_column("Description", style="bright_white")

        commands = [
            ("/quit, /q", "Exit the agent"),
            ("/help, /h", "Show this help message"),
            ("/screenshot, /ss", "Capture and save a screenshot"),
            ("/state, /st", "Show current browser state"),
            ("/reset, /r", "Reset agent context and history"),
            ("<any text>", "Give the agent a task to perform"),
        ]
        for cmd, desc in commands:
            table.add_row(cmd, desc)

        self.console.print()
        self.console.print(table)
        self.console.print()

    # ------------------------------------------------------------------ #
    # Step & action displays
    # ------------------------------------------------------------------ #

    def display_step(self, step: Step) -> None:
        """Display a single agent step (thought + action + result).

        Args:
            step: The step to render, containing number, thought,
                action, and result.
        """
        step_header = Text(f"Step {step.number}", style="bold underline bright_white")
        self.console.print(step_header)

        # Thought
        if step.thought:
            thought_text = Text(step.thought, style=COLOR_THOUGHT)
            self.console.print(
                Panel(
                    thought_text,
                    title="[bold bright_blue]💭 Thought[/bold bright_blue]",
                    border_style="blue",
                    box=box.MINIMAL,
                    padding=(0, 1),
                )
            )

        # Action
        action_text = self._format_tool_call(step.action)
        self.console.print(
            Panel(
                action_text,
                title="[bold bright_yellow]🔧 Action[/bold bright_yellow]",
                border_style="yellow",
                box=box.MINIMAL,
                padding=(0, 1),
            )
        )

        # Result
        result_style = COLOR_SUCCESS if "error" not in step.result.lower() else COLOR_ERROR
        result_text = Text(step.result, style=result_style)
        self.console.print(
            Panel(
                result_text,
                title="[bold bright_green]📋 Result[/bold bright_green]",
                border_style="green",
                box=box.MINIMAL,
                padding=(0, 1),
            )
        )
        self.console.print()

    def display_thinking(self, thought: str) -> None:
        """Display the agent's reasoning with a subtle spinner effect.

        Args:
            thought: The agent's reasoning text.
        """
        # Use a simple panel with a thinking emoji — Live spinner is
        # best used inside an async loop, so we provide a lightweight
        # synchronous rendering here that still looks polished.
        md = Markdown(thought)
        self.console.print(
            Panel(
                md,
                title="[bold bright_blue]💭 Thinking …[/bold bright_blue]",
                border_style="bright_blue",
                box=box.ROUNDED,
                padding=(1, 2),
            )
        )

    def display_action(self, action: ToolCall) -> None:
        """Display the action the agent is about to execute.

        Args:
            action: The tool call to render.
        """
        formatted = self._format_tool_call(action)
        self.console.print(
            Panel(
                formatted,
                title="[bold bright_yellow]🔧 Executing Action[/bold bright_yellow]",
                border_style="bright_yellow",
                box=box.ROUNDED,
                padding=(1, 2),
            )
        )

    def display_result(self, result: ActionResult) -> None:
        """Display the result of an executed action.

        Args:
            result: The action result, rendered green for success
                or red for failure.
        """
        style = COLOR_SUCCESS if result.success else COLOR_ERROR
        icon = "✅" if result.success else "❌"
        title_color = "bright_green" if result.success else "bright_red"

        # Build content
        content = Text()
        content.append(f"{icon}  ", style="")
        content.append(result.message, style=style)

        if not result.success and getattr(result, "error_type", None):
            content.append("\n\n", style="")
            content.append(
                f"Error type: {result.error_type}",
                style="dim italic",
            )

        self.console.print(
            Panel(
                content,
                title=f"[bold {title_color}]Result[/bold {title_color}]",
                border_style=title_color,
                box=box.ROUNDED,
                padding=(1, 2),
            )
        )

    # ------------------------------------------------------------------ #
    # Browser state display
    # ------------------------------------------------------------------ #

    def display_browser_state(self, state: BrowserState) -> None:
        """Display a summary of the current browser state.

        Args:
            state: The current browser snapshot including URL,
                title, distilled DOM, and optional screenshot.
        """
        table = Table(
            title="Browser State",
            box=box.SIMPLE_HEAD,
            header_style="bold bright_magenta",
            border_style="magenta",
            show_header=False,
        )
        table.add_column("Property", style="bold bright_magenta", no_wrap=True)
        table.add_column("Value", style="bright_white")

        table.add_row("URL", state.url)
        table.add_row("Title", state.title)
        table.add_row(
            "Timestamp",
            state.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC"),
        )

        # Count interactive elements roughly by line count
        dom_lines = [line for line in state.distilled_dom.splitlines() if line.strip()]
        element_count = max(0, len(dom_lines) - 2)  # minus URL/Title header lines
        table.add_row("Elements", str(element_count))

        if state.screenshot:
            size_kb = len(state.screenshot) / 1024
            table.add_row("Screenshot", f"{size_kb:.1f} KB")

        self.console.print()
        self.console.print(table)
        self.console.print()

    # ------------------------------------------------------------------ #
    # Security prompt
    # ------------------------------------------------------------------ #

    def display_security_prompt(self, decision: SecurityDecision) -> str:
        """Display a security confirmation prompt and return user input.

        Args:
            decision: The security decision containing risk level,
                explanation, and warning message.

        Returns:
            The user's raw input (expected: "yes", "y", etc.).
        """
        # Determine risk emoji and color
        risk = getattr(decision, "risk_level", "high").lower()
        if risk == "critical":
            emoji = "🚨"
            border = "bright_red"
        elif risk == "high":
            emoji = "⚠️"
            border = "yellow"
        else:
            emoji = "ℹ️"
            border = "bright_blue"

        # Build warning text
        warning_parts: list[Text] = []
        warning_parts.append(
            Text(f"{emoji}  SECURITY CHECK REQUIRED  {emoji}\n", style=f"bold {COLOR_SECURITY}")
        )

        reason = getattr(decision, "reason", "") or getattr(decision, "explanation", "")
        if reason:
            warning_parts.append(Text(f"Reason: {reason}\n", style="bold bright_white"))

        warning_msg = getattr(decision, "warning_message", None)
        if warning_msg:
            warning_parts.append(Text(f"{warning_msg}\n", style="bright_yellow"))

        keywords = getattr(decision, "destructive_keywords_found", [])
        if keywords:
            kw_text = ", ".join(keywords)
            warning_parts.append(
                Text(f"Keywords matched: {kw_text}\n", style="bold bright_red")
            )

        # Combine into a panel
        panel_content = Text.assemble(*warning_parts)
        self.console.print()
        self.console.print(
            Panel(
                panel_content,
                title=f"[bold {border}]Security — {risk.upper()} RISK[/bold {border}]",
                border_style=border,
                box=box.HEAVY,
                padding=(1, 2),
            )
        )

        # Ask user
        prompt_text = f"Proceed with this action? ({risk.upper()} risk) [yes/no]: "
        user_input = Prompt.ask(
            Text(prompt_text, style=COLOR_USER),
            console=self.console,
            choices=["yes", "no", "y", "n"],
            show_choices=True,
            default="no",
        )
        return str(user_input).strip()

    # ------------------------------------------------------------------ #
    # Final result & errors
    # ------------------------------------------------------------------ #

    def display_final_result(self, result: AgentResult) -> None:
        """Display the final result after the agent finishes a task.

        Args:
            result: The agent result containing success flag, steps,
                final answer, and timing info.
        """
        # Header
        if result.success:
            header = "[bold bright_green]✅ Task Completed Successfully[/bold bright_green]"
            border = "green"
        else:
            header = "[bold bright_red]❌ Task Failed or Incomplete[/bold bright_red]"
            border = "red"

        # Summary table
        table = Table(
            box=box.SIMPLE_HEAD,
            show_header=False,
            border_style=border,
        )
        table.add_column("Key", style=f"bold {border}", no_wrap=True)
        table.add_column("Value", style="bright_white")

        table.add_row("Task", result.task)
        table.add_row("Success", str(result.success))
        table.add_row("Total Steps", str(result.total_steps))
        table.add_row(
            "Time",
            f"{result.total_time_seconds:.1f} seconds",
        )

        # Final answer
        answer_md = Markdown(result.final_answer)

        content = Group(
            Text("Summary:", style=f"bold {border}"),
            table,
            Text("\nFinal answer:", style=f"bold {border}"),
            answer_md,
        )

        self.console.print()
        self.console.print(
            Panel(
                content,
                title=header,
                border_style=border,
                box=box.ROUNDED,
                padding=(1, 2),
            )
        )
        self.console.print()

    def display_error(self, error: str) -> None:
        """Display an error message.

        Args:
            error: The error text to display.
        """
        self.console.print()
        self.console.print(
            Panel(
                Text(error, style=COLOR_ERROR),
                title="[bold bright_red]Error[/bold bright_red]",
                border_style="bright_red",
                box=box.HEAVY,
                padding=(1, 2),
            )
        )
        self.console.print()

    # ------------------------------------------------------------------ #
    # User interaction
    # ------------------------------------------------------------------ #

    def get_user_input(self, prompt: str = "You") -> str:
        """Prompt the user for input and return their response.

        Args:
            prompt: Label to show before the input prompt.

        Returns:
            The user's raw input string.
        """
        prompt_text = Text(f"{prompt} ▸ ", style=COLOR_USER)
        self.console.print(prompt_text, end="")
        try:
            user_input = input().strip()
        except (EOFError, KeyboardInterrupt):
            user_input = "/quit"
        return user_input

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _format_tool_call(self, action: ToolCall) -> Text:
        """Format a ToolCall into a Rich Text object.

        Args:
            action: The tool call to format.

        Returns:
            A styled Text representation.
        """
        text = Text()
        text.append(f"Tool: ", style="bold bright_white")
        text.append(action.name, style="bold bright_yellow")
        text.append("\n", style="")

        if action.arguments:
            text.append("Arguments:\n", style="bold dim")
            for key, val in action.arguments.items():
                text.append(f"  • {key}: ", style="dim")
                # Truncate long values
                val_str = str(val)
                if len(val_str) > 200:
                    val_str = val_str[:200] + " …"
                text.append(val_str, style="bright_white")
                text.append("\n", style="")

        if action.id:
            text.append(f"ID: {action.id}", style="dim italic")

        return text

    # ------------------------------------------------------------------ #
    # Extra convenience displays (used by main.py commands)
    # ------------------------------------------------------------------ #

    def display_info(self, message: str) -> None:
        """Display an informational message.

        Args:
            message: Info text to display.
        """
        self.console.print(f"[dim]ℹ️  {message}[/dim]")

    def display_success(self, message: str) -> None:
        """Display a success message.

        Args:
            message: Success text to display.
        """
        self.console.print(f"[bold bright_green]✅ {message}[/bold bright_green]")

    def display_warning(self, message: str) -> None:
        """Display a warning message.

        Args:
            message: Warning text to display.
        """
        self.console.print(f"[bold bright_yellow]⚠️  {message}[/bold bright_yellow]")
