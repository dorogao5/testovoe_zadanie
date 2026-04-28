from __future__ import annotations

from ai_browser_agent.agent.models import ModelRole
from ai_browser_agent.config import AgentConfig
from ai_browser_agent.llm.base import LLMClient, LLMRequest, LLMResponse


class ModelCascade:
    def __init__(self, *, client: LLMClient, config: AgentConfig) -> None:
        self.client = client
        self.config = config

    def select_role(
        self,
        *,
        step: int,
        repeated_failures: int = 0,
        risky: bool = False,
        vision_needed: bool = False,
        final_verification: bool = False,
    ) -> ModelRole:
        if vision_needed:
            return ModelRole.vision
        if risky or final_verification or repeated_failures >= 2:
            return ModelRole.strong
        if step <= 1:
            return ModelRole.primary
        return ModelRole.fast if step % 3 else ModelRole.primary

    def model_for(self, role: ModelRole) -> str:
        return {
            ModelRole.fast: self.config.fast_model,
            ModelRole.primary: self.config.primary_model,
            ModelRole.strong: self.config.strong_model,
            ModelRole.vision: self.config.vision_model,
        }[role]

    async def complete(self, request: LLMRequest, *, role: ModelRole) -> LLMResponse:
        request.model = self.model_for(role)
        return await self.client.complete(request)

