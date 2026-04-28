from ai_browser_agent.agent.cascade import ModelCascade
from ai_browser_agent.agent.models import ModelRole
from ai_browser_agent.config import AgentConfig
from ai_browser_agent.llm.base import FakeLLMClient


def test_cascade_escalates_repeated_failures() -> None:
    cascade = ModelCascade(client=FakeLLMClient(), config=AgentConfig())

    assert cascade.select_role(step=5, repeated_failures=2) == ModelRole.strong
    assert cascade.select_role(step=5, vision_needed=True) == ModelRole.vision
    assert cascade.select_role(step=1) == ModelRole.primary


def test_kimi_provider_defaults_to_k2_6(monkeypatch) -> None:
    monkeypatch.setenv("AI_BROWSER_SKIP_DOTENV", "1")
    monkeypatch.delenv("AI_BROWSER_PRIMARY_MODEL", raising=False)
    monkeypatch.delenv("AI_BROWSER_FAST_MODEL", raising=False)
    monkeypatch.delenv("AI_BROWSER_STRONG_MODEL", raising=False)
    monkeypatch.delenv("AI_BROWSER_VISION_MODEL", raising=False)
    monkeypatch.delenv("AI_BROWSER_LLM_TPM_LIMIT", raising=False)

    config = AgentConfig.from_env().with_overrides(provider="kimi")

    assert config.fast_model == "kimi-k2.6"
    assert config.primary_model == "kimi-k2.6"
    assert config.strong_model == "kimi-k2.6"
    assert config.vision_model == "kimi-k2.6"
    assert config.llm_tpm_limit == 50000
    assert config.kimi_thinking == "disabled"
    assert config.kimi_strong_thinking == "enabled"


def test_env_tpm_limit_override_is_preserved(monkeypatch) -> None:
    monkeypatch.setenv("AI_BROWSER_SKIP_DOTENV", "1")
    monkeypatch.setenv("AI_BROWSER_PROVIDER", "kimi")
    monkeypatch.setenv("AI_BROWSER_LLM_TPM_LIMIT", "75000")

    config = AgentConfig.from_env()

    assert config.llm_tpm_limit == 75000


def test_ask_user_is_disabled_by_default_from_env(monkeypatch) -> None:
    monkeypatch.setenv("AI_BROWSER_SKIP_DOTENV", "1")
    monkeypatch.delenv("AI_BROWSER_ALLOW_ASK_USER", raising=False)

    config = AgentConfig.from_env()

    assert config.allow_ask_user is False


def test_ask_user_can_be_enabled_from_env(monkeypatch) -> None:
    monkeypatch.setenv("AI_BROWSER_SKIP_DOTENV", "1")
    monkeypatch.setenv("AI_BROWSER_ALLOW_ASK_USER", "true")

    config = AgentConfig.from_env()

    assert config.allow_ask_user is True
