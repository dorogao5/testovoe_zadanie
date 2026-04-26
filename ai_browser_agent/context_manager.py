"""ContextManager — Conversation history and token-budget management.

Manages two separate message stores:

* ``_full_history`` — every message ever added (never discarded, for audit / reference).
* ``_messages`` — the trimmed / condensed list sent to the LLM API.

Trimming strategy
-----------------
1. Always preserve the system prompt + current task description.
2. Keep the most recent 8–10 interaction pairs (observation → thought → action → result).
3. When the token budget is approached, older messages are summarized into a single
   "memory" assistant message.
4. Summarization is performed via the :meth:`summarize_old_messages` helper which
   can use a local heuristic or an optional LLM client for higher-quality compression.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from models import BrowserState, ToolCall
from prompts import SUMMARIZER_PROMPT
from utils import count_tokens

logger = logging.getLogger("context_manager")


class ContextManager:
    """Manages conversation context with token-budget trimming and summarization."""

    # Number of most-recent interaction *pairs* to keep verbatim before trimming.
    _DEFAULT_KEEP_PAIRS = 10
    _SUMMARY_MAX_MESSAGES = 80
    _SUMMARY_MAX_CHARS = 12000
    _SUMMARY_THREAD_TIMEOUT_S = 8.0

    def __init__(
        self,
        max_tokens: int = 6000,
        model: str = "kimi-latest",
        llm_client: Any | None = None,
    ) -> None:
        self.max_tokens = max_tokens
        self.model = model
        self._llm_client = llm_client

        # The condensed list sent to the API
        self._messages: list[dict[str, Any]] = []
        # The complete, immutable audit log
        self._full_history: list[dict[str, Any]] = []

        # Special protected slots
        self._system_prompt: str = ""
        self._task_description: str = ""

    # ------------------------------------------------------------------ #
    # Basic message operations
    # ------------------------------------------------------------------ #

    def add_message(self, role: str, content: str) -> None:
        """Append a plain text message to both stores."""
        msg = {"role": role, "content": content}
        self._messages.append(msg)
        self._full_history.append(msg)
        self._maybe_trim()

    def add_tool_result(self, tool_call_id: str, name: str, content: str) -> None:
        """Append a tool / function result message."""
        msg = {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": name,
            "content": content,
        }
        self._messages.append(msg)
        self._full_history.append(msg)
        self._maybe_trim()

    def get_messages(self) -> list[dict[str, Any]]:
        """Return the trimmed list ready for the LLM API."""
        return list(self._messages)

    def get_full_history(self) -> list[dict[str, Any]]:
        """Return the complete, un-trimmed message log."""
        return list(self._full_history)

    def get_token_count(self) -> int:
        """Approximate total tokens in the *trimmed* message list."""
        total = 0
        for msg in self._messages:
            # Count both the role overhead and the content
            text = f"{msg.get('role', '')}: {msg.get('content', '')}"
            total += count_tokens(text)
        return total

    def get_full_token_count(self) -> int:
        """Approximate total tokens in the *full* history."""
        total = 0
        for msg in self._full_history:
            text = f"{msg.get('role', '')}: {msg.get('content', '')}"
            total += count_tokens(text)
        return total

    def add_system_prompt(self, prompt: str) -> None:
        """Set (or replace) the system prompt."""
        self._system_prompt = prompt
        self._rebuild_messages()

    def get_system_prompt(self) -> str:
        """Return the current system prompt text."""
        return self._system_prompt

    def set_task_description(self, task: str) -> None:
        """Set (or replace) the current task description."""
        self._task_description = task
        self._rebuild_messages()

    def get_task_description(self) -> str:
        """Return the current task description."""
        return self._task_description

    def clear(self) -> None:
        """Reset both stores but preserve system prompt and task."""
        self._messages = []
        self._full_history = []
        self._rebuild_messages()

    def set_llm_client(self, client: Any) -> None:
        """Attach an LLM client for high-quality summarization."""
        self._llm_client = client

    # ------------------------------------------------------------------ #
    # High-level domain-specific helpers
    # ------------------------------------------------------------------ #

    def add_observation(self, state: BrowserState) -> None:
        """Record a distilled browser-state observation as a user message."""
        content = (
            f"Current page: {state.title} ({state.url})\n"
            f"{state.distilled_dom}"
        )
        self.add_message("user", content)

    def add_thought_and_action(
        self,
        thought: str,
        action: ToolCall,
        result: str,
    ) -> None:
        """Record an assistant thought, its tool call, and the execution result."""
        # Record the assistant's reasoning
        self.add_message("assistant", f"Thought: {thought}")

        # Record the tool call as a synthetic assistant message with tool_calls
        tool_call_msg = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": action.id,
                    "type": "function",
                    "function": {
                        "name": action.name,
                        "arguments": str(action.arguments),
                    },
                }
            ],
        }
        self._messages.append(tool_call_msg)
        self._full_history.append(tool_call_msg)

        # Record the tool result
        self.add_tool_result(
            tool_call_id=action.id,
            name=action.name,
            content=result,
        )
        self._maybe_trim()

    # ------------------------------------------------------------------ #
    # Trimming & summarization
    # ------------------------------------------------------------------ #

    def trim_history(self) -> None:
        """Explicitly force a trim (usually called automatically by ``_maybe_trim``)."""
        self._perform_trim()

    def _maybe_trim(self) -> None:
        """Trim if we're over budget (with a 10 % head-room buffer)."""
        budget = int(self.max_tokens * 0.9)
        if self.get_token_count() > budget:
            logger.info(
                "Token budget approached (%d / %d); trimming history …",
                self.get_token_count(),
                self.max_tokens,
            )
            self._perform_trim()

    def _rebuild_messages(self) -> None:
        """Rebuild ``_messages`` from scratch, inserting system + task first."""
        rebuilt: list[dict[str, Any]] = []
        if self._system_prompt:
            rebuilt.append({"role": "system", "content": self._system_prompt})
        if self._task_description:
            rebuilt.append({"role": "user", "content": f"Task: {self._task_description}"})
        # Re-append all non-system/task messages from full history
        for msg in self._full_history:
            role = msg.get("role", "")
            if role == "system":
                continue
            if role == "user" and msg.get("content", "").startswith("Task:"):
                continue
            rebuilt.append(msg)
        self._messages = rebuilt
        self._maybe_trim()

    def _perform_trim(self) -> None:
        """Core trimming logic.

        1. Identify protected prefix (system + task).
        2. Keep the most recent ``_DEFAULT_KEEP_PAIRS`` interaction cycles.
        3. Summarize everything in between into a single "memory" message.
        """
        protected: list[dict[str, Any]] = []
        body: list[dict[str, Any]] = []

        for msg in self._messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "system":
                protected.append(msg)
                continue
            if role == "user" and content.startswith("Task:"):
                protected.append(msg)
                continue
            body.append(msg)

        if not body:
            self._messages = protected
            return

        # How many messages constitute one "keep window"?
        # We treat ~4 messages as one cycle (observation, thought, tool_call, result).
        keep_count = self._DEFAULT_KEEP_PAIRS * 4

        if len(body) <= keep_count:
            # Nothing to trim — just ensure we're under budget
            self._messages = protected + body
            return

        older = body[:-keep_count]
        recent = body[-keep_count:]

        # Summarize older messages
        summary = self.summarize_old_messages(older)
        memory_msg = {
            "role": "user",
            "content": f"[Memory — earlier interactions summarized]\n{summary}",
        }

        self._messages = protected + [memory_msg] + recent
        logger.info(
            "Trimmed history: %d older messages summarized into %d tokens",
            len(older),
            count_tokens(memory_msg["content"]),
        )

    def summarize_old_messages(self, messages: list[dict[str, Any]]) -> str:
        """Compress a sequence of older messages into a compact summary.

        If an LLM client has been attached via :meth:`set_llm_client`, a
        high-quality LLM-based summary is produced.  Otherwise a fast local
        heuristic is used.
        """
        if self._llm_client is not None:
            return self._summarize_with_llm(messages)
        return self._summarize_locally(messages)

    # ------------------------------------------------------------------ #
    # Summarization engines
    # ------------------------------------------------------------------ #

    def _summarize_with_llm(self, messages: list[dict[str, Any]]) -> str:
        """Use the attached LLM client to produce a concise summary."""
        try:
            # Guardrail: for very large backlogs, use local summary to avoid
            # expensive mega-prompts and token spikes.
            if len(messages) > self._SUMMARY_MAX_MESSAGES * 2:
                return self._summarize_locally(messages)

            formatted = self._format_messages_for_summary(messages)
            prompt = SUMMARIZER_PROMPT.format(
                task=self._task_description or "<no task>",
                messages=formatted,
                max_tokens=500,
            )
            chat_messages = [
                {"role": "system", "content": "You are a helpful summarizer."},
                {"role": "user", "content": prompt},
            ]
            # KimiClient.chat is async — run it synchronously via asyncio.run
            # because ContextManager methods are sync by design.
            import asyncio

            async def _call() -> dict[str, Any]:
                return await self._llm_client.chat(
                    messages=chat_messages,
                    temperature=0.2,
                    max_tokens=800,
                )

            def _run_in_fresh_loop() -> dict[str, Any]:
                return asyncio.run(_call())

            try:
                asyncio.get_running_loop()
                has_running_loop = True
            except RuntimeError:
                has_running_loop = False

            if has_running_loop:
                # ContextManager is sync by design and can be called inside an active
                # event loop. Run the async summarization in a dedicated thread with
                # its own loop to avoid RuntimeError from nested asyncio.run().
                holder: dict[str, Any] = {"response": None, "error": None}

                def _worker() -> None:
                    try:
                        holder["response"] = _run_in_fresh_loop()
                    except Exception as worker_exc:  # noqa: BLE001
                        holder["error"] = worker_exc

                thread = threading.Thread(target=_worker, daemon=True)
                thread.start()
                thread.join(timeout=self._SUMMARY_THREAD_TIMEOUT_S)
                if thread.is_alive():
                    logger.warning(
                        "LLM summary timed out after %.1fs; falling back to local heuristic",
                        self._SUMMARY_THREAD_TIMEOUT_S,
                    )
                    return self._summarize_locally(messages)
                if holder["error"] is not None:
                    raise holder["error"]
                response = holder["response"] or {}
            else:
                response = _run_in_fresh_loop()

            content = ""
            if hasattr(self._llm_client, "extract_content"):
                content = self._llm_client.extract_content(response)
            else:
                choices = response.get("choices", [])
                if choices:
                    content = choices[0].get("message", {}).get("content", "")
            return content.strip() or "<no summary generated>"
        except Exception as exc:
            logger.warning("LLM summarization failed (%s); falling back to local heuristic", exc)
            return self._summarize_locally(messages)

    def _summarize_locally(self, messages: list[dict[str, Any]]) -> str:
        """Fast extractive summarisation without an LLM."""
        observations: list[str] = []
        actions: list[str] = []
        findings: list[str] = []
        current_url: str = ""

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if not content:
                continue

            # Extract URL from observation messages
            if role == "user" and content.startswith("Current page:"):
                lines = content.splitlines()
                if lines:
                    observations.append(lines[0])
                    # Try to grab the URL from the first line
                    parts = lines[0].split("(")
                    if len(parts) > 1:
                        current_url = parts[-1].rstrip(")")

            # Extract thought / action from assistant
            if role == "assistant" and content.startswith("Thought:"):
                thought = content[len("Thought:") :].strip()
                if thought:
                    actions.append(thought)

            # Extract tool results (skip raw DOM noise)
            if role == "tool":
                # Only keep results that look like meaningful information
                if len(content) < 300:
                    findings.append(content)
                else:
                    # Truncate long results (usually DOM)
                    findings.append(content[:200] + " …")

        parts: list[str] = []
        if current_url:
            parts.append(f"Last known URL: {current_url}")
        if observations:
            parts.append(f"Visited {len(observations)} page(s).")
        if actions:
            parts.append("Key actions taken:")
            for a in actions[-5:]:
                parts.append(f"  - {a}")
        if findings:
            parts.append("Notable findings:")
            for f in findings[-5:]:
                parts.append(f"  • {f}")

        return "\n".join(parts) if parts else "<earlier interactions summarized — no key details>"

    @staticmethod
    def _format_messages_for_summary(messages: list[dict[str, Any]]) -> str:
        """Pretty-print messages for the summarizer prompt."""
        lines: list[str] = []
        total_chars = 0
        for i, msg in enumerate(messages[: ContextManager._SUMMARY_MAX_MESSAGES], 1):
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            snippet = content[:200]
            line = f"[{i}] {role}: {snippet}"
            if total_chars + len(line) > ContextManager._SUMMARY_MAX_CHARS:
                lines.append("[...] truncated due to summary input budget")
                break
            lines.append(line)
            total_chars += len(line)
        return "\n".join(lines)
