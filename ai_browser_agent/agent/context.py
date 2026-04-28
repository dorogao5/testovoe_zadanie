from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ai_browser_agent.agent.models import ActionRecord, AgentState
from ai_browser_agent.agent.prompts import SYSTEM_PROMPT


@dataclass
class ContextManager:
    budget_tokens: int = 24_000
    recent_steps_limit: int = 12
    compacted_memory: list[str] = field(default_factory=list)

    def build_messages(self, state: AgentState, latest_tool_result: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self._compact_if_needed(state)
        screenshot_images = _latest_screenshot_paths(latest_tool_result)
        limits = _message_limits_for_state(state)
        payload = self._build_payload(
            state,
            latest_tool_result,
            max_elements=limits[0][0],
            max_text_chunks=limits[0][1],
        )
        for max_elements, max_text_chunks in limits[1:]:
            if self.estimate_tokens([{"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}]) <= self.budget_tokens:
                break
            payload = self._build_payload(
                state,
                latest_tool_result,
                max_elements=max_elements,
                max_text_chunks=max_text_chunks,
            )
            payload["context_budget_note"] = (
                f"Observation compacted to {max_elements} elements and {max_text_chunks} text chunks "
                f"to stay near the {self.budget_tokens}-token context budget. Use query_dom/extract "
                "for targeted details instead of broad exploration."
            )
        message = {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        }
        if screenshot_images:
            message["images"] = screenshot_images
        return [message]

    def system_prompt(self) -> str:
        return SYSTEM_PROMPT

    def _build_payload(
        self,
        state: AgentState,
        latest_tool_result: dict[str, Any] | None,
        *,
        max_elements: int,
        max_text_chunks: int,
    ) -> dict[str, Any]:
        return {
            "trusted_user_task": state.task,
            "current_plan": [item.render() for item in state.plan],
            "agent_memory": state.memory,
            "trusted_action_trace": _action_trace(state.recent_actions[-self.recent_steps_limit :]),
            "recent_actions": [
                _compact_action_record(record)
                for record in state.recent_actions[-self.recent_steps_limit :]
            ],
            "recent_failures": [failure.model_dump(mode="json") for failure in state.failures[-6:]],
            "untrusted_page_content": state.last_observation.to_model_summary(
                max_elements=max_elements,
                max_text_chunks=max_text_chunks,
            )
            if state.last_observation
            else None,
            "latest_tool_result": _compact_tool_result(latest_tool_result),
            "instructions": (
                "Choose the next tool call. If the current observation is missing, call observe. "
                "If a ref is stale or unclear, call query_dom or observe before acting. "
                "For large pages, prefer query_dom/extract with a specific query over broad scans."
            ),
        }

    def remember_tool_result(self, state: AgentState, record: ActionRecord) -> None:
        if len(state.recent_actions) > self.recent_steps_limit * 2:
            older = state.recent_actions[: -self.recent_steps_limit]
            summary = "; ".join(
                f"step {item.step} {item.tool}: {item.summary}" for item in older[-8:]
            )
            if summary:
                self.compacted_memory.append(summary)
                state.memory = "\n".join(self.compacted_memory[-8:])
            state.recent_actions = state.recent_actions[-self.recent_steps_limit :]

    def estimate_tokens(self, messages: list[dict[str, str]]) -> int:
        return max(1, sum(len(message.get("content", "")) for message in messages) // 4)

    def _compact_if_needed(self, state: AgentState) -> None:
        messages = [
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": state.task,
                        "memory": state.memory,
                        "actions": [a.model_dump(mode="json") for a in state.recent_actions],
                        "observation": state.last_observation.to_model_summary()
                        if state.last_observation
                        else None,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            }
        ]
        if self.estimate_tokens(messages) <= self.budget_tokens:
            return
        older = state.recent_actions[: -self.recent_steps_limit]
        if older:
            self.compacted_memory.append(_summarize_actions(older))
            state.recent_actions = state.recent_actions[-self.recent_steps_limit :]
        state.memory = "\n".join(self.compacted_memory[-10:])


def _summarize_actions(actions: list[ActionRecord]) -> str:
    succeeded = sum(1 for action in actions if action.ok)
    failed = len(actions) - succeeded
    last = actions[-1].summary if actions else ""
    return f"Compacted {len(actions)} older actions ({succeeded} ok, {failed} failed). Last: {last}"


def _message_limits_for_state(state: AgentState) -> list[tuple[int, int]]:
    observation = state.last_observation
    if observation is None:
        return [(60, 12), (40, 8), (24, 5), (12, 3)]
    large_dynamic_page = (
        len(observation.elements) > 90
        or observation.stats.text_length > 7000
        or "mail.google.com" in observation.url
    )
    if large_dynamic_page:
        return [(32, 6), (20, 4), (12, 3), (8, 2)]
    return [(60, 12), (40, 8), (24, 5), (12, 3)]


def _action_trace(actions: list[ActionRecord]) -> list[str]:
    trace: list[str] = []
    for action in actions:
        target = action.target or {}
        target_bits = []
        if target:
            label = target.get("name") or target.get("text") or target.get("aria_label") or target.get("placeholder")
            if label:
                target_bits.append(f"target_at_action={label!r}")
            if target.get("role"):
                target_bits.append(f"role={target['role']!r}")
            if target.get("tag"):
                target_bits.append(f"tag={target['tag']!r}")
            parents = target.get("parent_chain")
            if isinstance(parents, list) and parents:
                target_bits.append(f"parents={parents[:3]!r}")
            if target.get("ref"):
                target_bits.append(f"historical_ref={target['ref']!r}")
        intent = action.args.get("intent")
        intent_text = f" intent={intent!r}" if intent else ""
        target_text = " " + " ".join(target_bits) if target_bits else ""
        trace.append(
            f"step {action.step}: {action.tool}{intent_text} ok={action.ok} "
            f"summary={action.summary!r}{target_text}"
        )
    return trace


def _compact_action_record(record: ActionRecord) -> dict[str, Any]:
    args = record.args or {}
    compact: dict[str, Any] = {
        "step": record.step,
        "tool": record.tool,
        "ok": record.ok,
        "summary": record.summary[:260],
    }
    intent = args.get("intent")
    if intent:
        compact["intent"] = str(intent)[:180]
    if record.target:
        target = record.target
        compact["target_at_action"] = {
            key: target.get(key)
            for key in ("ref", "role", "tag", "name", "text", "placeholder")
            if target.get(key)
        }
        parents = target.get("parent_chain")
        if isinstance(parents, list) and parents:
            compact["target_at_action"]["parent_chain"] = parents[:2]
    if record.page_fingerprint:
        compact["page_fingerprint"] = record.page_fingerprint
    return compact


def _latest_screenshot_paths(tool_result: dict[str, Any] | None) -> list[str]:
    if not tool_result:
        return []
    data = tool_result.get("data")
    if not isinstance(data, dict):
        return []
    path = data.get("path")
    if isinstance(path, str) and path.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        return [path]
    artifact_path = data.get("failure_screenshot")
    if isinstance(artifact_path, str) and artifact_path.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        return [artifact_path]
    return []


def _compact_tool_result(tool_result: dict[str, Any] | None) -> dict[str, Any] | None:
    if tool_result is None:
        return None
    result = json.loads(json.dumps(tool_result, ensure_ascii=False, default=str))
    data = result.get("data")
    if isinstance(data, dict):
        observation = data.get("observation")
        if isinstance(observation, dict):
            data["observation"] = _observation_receipt(observation)
        candidates = data.get("candidates")
        if isinstance(candidates, list):
            data["candidates"] = [_compact_candidate(candidate) for candidate in candidates[:3]]
            data["candidates_compacted"] = True
        previous = data.get("previous_action")
        if isinstance(previous, dict):
            prev_result = previous.get("result")
            if isinstance(prev_result, dict):
                prev_data = prev_result.get("data")
                if isinstance(prev_data, dict) and isinstance(prev_data.get("observation"), dict):
                    prev_data["observation"] = _observation_receipt(prev_data["observation"])
    return result


def _compact_candidate(candidate: Any) -> Any:
    if not isinstance(candidate, dict):
        return candidate
    compact = {
        "ref": candidate.get("ref"),
        "score": candidate.get("score"),
        "evidence": str(candidate.get("evidence", ""))[:180],
    }
    element = candidate.get("element")
    if isinstance(element, dict):
        compact["element"] = {
            key: element.get(key)
            for key in ("ref", "role", "tag", "name", "text", "aria_label", "parent_chain", "bbox")
            if element.get(key)
        }
        for key in ("name", "text", "aria_label"):
            if key in compact["element"]:
                compact["element"][key] = str(compact["element"][key])[:180]
    return compact


def _observation_receipt(observation: dict[str, Any]) -> dict[str, Any]:
    elements = observation.get("elements")
    chunks = observation.get("text_chunks")
    return {
        "url": observation.get("url"),
        "title": observation.get("title"),
        "fingerprint": observation.get("fingerprint"),
        "stats": observation.get("stats"),
        "elements_count": len(elements) if isinstance(elements, list) else None,
        "text_chunks_count": len(chunks) if isinstance(chunks, list) else None,
        "truncated": observation.get("truncated"),
        "context_compacted": True,
        "note": "Full latest observation is already present in untrusted_page_content.",
    }
