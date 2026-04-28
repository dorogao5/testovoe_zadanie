from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - dependency is validated by doctor
    load_dotenv = None


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


PROVIDER_DEFAULT_MODELS: dict[str, dict[str, str]] = {
    "anthropic": {
        "primary": "claude-3-7-sonnet-latest",
        "fast": "claude-3-5-haiku-latest",
        "strong": "claude-3-7-sonnet-latest",
        "vision": "claude-3-7-sonnet-latest",
    },
    "openai": {
        "primary": "gpt-5.2",
        "fast": "gpt-5.4-mini",
        "strong": "gpt-5.5",
        "vision": "gpt-5.5",
    },
    "kimi": {
        "primary": "kimi-k2.6",
        "fast": "kimi-k2.6",
        "strong": "kimi-k2.6",
        "vision": "kimi-k2.6",
    },
    "fake": {
        "primary": "fake-primary",
        "fast": "fake-fast",
        "strong": "fake-strong",
        "vision": "fake-vision",
    },
}

DEFAULT_KIMI_BASE_URL = "https://api.moonshot.ai/v1"


@dataclass(frozen=True)
class AgentConfig:
    provider: str = "anthropic"
    primary_model: str = "claude-3-7-sonnet-latest"
    fast_model: str = "claude-3-5-haiku-latest"
    strong_model: str = "claude-3-7-sonnet-latest"
    vision_model: str = "claude-3-7-sonnet-latest"
    profile_dir: Path = Path("./profiles/default")
    runs_dir: Path = Path("./runs")
    max_steps: int = 50
    max_consecutive_failures: int = 5
    headless: bool = False
    record_video: bool = False
    trace: bool = False
    browser_channel: str | None = None
    cdp_url: str | None = None
    viewport_width: int = 1280
    viewport_height: int = 900
    context_budget_tokens: int = 24_000
    llm_tpm_limit: int = 0
    llm_tpm_safety_factor: float = 2.0
    allow_ask_user: bool = False
    kimi_base_url: str = DEFAULT_KIMI_BASE_URL
    kimi_thinking: str = "disabled"
    kimi_strong_thinking: str = "enabled"
    prompt_cache_key: str | None = None
    safety_identifier: str | None = None

    @classmethod
    def from_env(cls) -> "AgentConfig":
        if load_dotenv is not None and not _bool_env("AI_BROWSER_SKIP_DOTENV", False):
            load_dotenv()
        provider = os.getenv("AI_BROWSER_PROVIDER", cls.provider)
        defaults = PROVIDER_DEFAULT_MODELS.get(provider, PROVIDER_DEFAULT_MODELS["anthropic"])
        default_tpm_limit = "50000" if provider == "kimi" else str(cls.llm_tpm_limit)
        return cls(
            provider=provider,
            primary_model=os.getenv("AI_BROWSER_PRIMARY_MODEL", defaults["primary"]),
            fast_model=os.getenv("AI_BROWSER_FAST_MODEL", defaults["fast"]),
            strong_model=os.getenv("AI_BROWSER_STRONG_MODEL", defaults["strong"]),
            vision_model=os.getenv("AI_BROWSER_VISION_MODEL", defaults["vision"]),
            profile_dir=Path(os.getenv("AI_BROWSER_PROFILE_DIR", str(cls.profile_dir))),
            runs_dir=Path(os.getenv("AI_BROWSER_RUNS_DIR", str(cls.runs_dir))),
            max_steps=int(os.getenv("AI_BROWSER_MAX_STEPS", str(cls.max_steps))),
            max_consecutive_failures=int(
                os.getenv(
                    "AI_BROWSER_MAX_CONSECUTIVE_FAILURES",
                    str(cls.max_consecutive_failures),
                )
            ),
            headless=_bool_env("AI_BROWSER_HEADLESS", cls.headless),
            record_video=_bool_env("AI_BROWSER_RECORD_VIDEO", cls.record_video),
            trace=_bool_env("AI_BROWSER_TRACE", cls.trace),
            browser_channel=os.getenv("AI_BROWSER_BROWSER_CHANNEL") or None,
            cdp_url=os.getenv("AI_BROWSER_CDP_URL") or None,
            viewport_width=int(os.getenv("AI_BROWSER_VIEWPORT_WIDTH", str(cls.viewport_width))),
            viewport_height=int(os.getenv("AI_BROWSER_VIEWPORT_HEIGHT", str(cls.viewport_height))),
            context_budget_tokens=int(
                os.getenv("AI_BROWSER_CONTEXT_BUDGET_TOKENS", str(cls.context_budget_tokens))
            ),
            llm_tpm_limit=int(os.getenv("AI_BROWSER_LLM_TPM_LIMIT", default_tpm_limit)),
            llm_tpm_safety_factor=float(
                os.getenv("AI_BROWSER_LLM_TPM_SAFETY_FACTOR", str(cls.llm_tpm_safety_factor))
            ),
            allow_ask_user=_bool_env("AI_BROWSER_ALLOW_ASK_USER", cls.allow_ask_user),
            kimi_base_url=os.getenv("KIMI_BASE_URL", cls.kimi_base_url),
            kimi_thinking=os.getenv("AI_BROWSER_KIMI_THINKING", cls.kimi_thinking).strip().lower(),
            kimi_strong_thinking=os.getenv(
                "AI_BROWSER_KIMI_STRONG_THINKING", cls.kimi_strong_thinking
            )
            .strip()
            .lower(),
            prompt_cache_key=os.getenv("AI_BROWSER_PROMPT_CACHE_KEY") or None,
            safety_identifier=os.getenv("AI_BROWSER_SAFETY_IDENTIFIER") or None,
        )

    def with_overrides(
        self,
        *,
        provider: str | None = None,
        profile_dir: Path | None = None,
        runs_dir: Path | None = None,
        max_steps: int | None = None,
        headless: bool | None = None,
        record_video: bool | None = None,
        trace: bool | None = None,
        browser_channel: str | None = None,
        cdp_url: str | None = None,
    ) -> "AgentConfig":
        selected_provider = provider or self.provider
        defaults = PROVIDER_DEFAULT_MODELS.get(selected_provider, PROVIDER_DEFAULT_MODELS["anthropic"])
        provider_changed = provider is not None and provider != self.provider
        if os.getenv("AI_BROWSER_LLM_TPM_LIMIT"):
            llm_tpm_limit = self.llm_tpm_limit
        elif provider_changed:
            llm_tpm_limit = 50000 if selected_provider == "kimi" else 0
        else:
            llm_tpm_limit = self.llm_tpm_limit
        return AgentConfig(
            provider=selected_provider,
            primary_model=defaults["primary"] if provider_changed and not os.getenv("AI_BROWSER_PRIMARY_MODEL") else self.primary_model,
            fast_model=defaults["fast"] if provider_changed and not os.getenv("AI_BROWSER_FAST_MODEL") else self.fast_model,
            strong_model=defaults["strong"] if provider_changed and not os.getenv("AI_BROWSER_STRONG_MODEL") else self.strong_model,
            vision_model=defaults["vision"] if provider_changed and not os.getenv("AI_BROWSER_VISION_MODEL") else self.vision_model,
            profile_dir=profile_dir or self.profile_dir,
            runs_dir=runs_dir or self.runs_dir,
            max_steps=max_steps if max_steps is not None else self.max_steps,
            max_consecutive_failures=self.max_consecutive_failures,
            headless=headless if headless is not None else self.headless,
            record_video=record_video if record_video is not None else self.record_video,
            trace=trace if trace is not None else self.trace,
            browser_channel=browser_channel if browser_channel is not None else self.browser_channel,
            cdp_url=cdp_url if cdp_url is not None else self.cdp_url,
            viewport_width=self.viewport_width,
            viewport_height=self.viewport_height,
            context_budget_tokens=self.context_budget_tokens,
            llm_tpm_limit=llm_tpm_limit,
            llm_tpm_safety_factor=self.llm_tpm_safety_factor,
            allow_ask_user=self.allow_ask_user,
            kimi_base_url=self.kimi_base_url,
            kimi_thinking=self.kimi_thinking,
            kimi_strong_thinking=self.kimi_strong_thinking,
            prompt_cache_key=self.prompt_cache_key,
            safety_identifier=self.safety_identifier,
        )


def missing_provider_keys(provider: str) -> list[str]:
    if provider == "anthropic":
        return [] if os.getenv("ANTHROPIC_API_KEY") else ["ANTHROPIC_API_KEY"]
    if provider == "openai":
        return [] if os.getenv("OPENAI_API_KEY") else ["OPENAI_API_KEY"]
    if provider == "kimi":
        return [] if os.getenv("KIMI_API_KEY") else ["KIMI_API_KEY"]
    return []
