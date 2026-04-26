import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from context_manager import ContextManager  # noqa: E402


class _FakeLLMClient:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, temperature=0.2, max_tokens=800):  # noqa: ANN001
        self.calls += 1
        return {"choices": [{"message": {"content": "LLM summary result"}}]}

    def extract_content(self, response):  # noqa: ANN001
        return response["choices"][0]["message"]["content"]


class ContextManagerSummarizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_llm_summary_works_inside_running_loop(self) -> None:
        llm = _FakeLLMClient()
        manager = ContextManager(max_tokens=6000, model="qwen3.6-max-preview", llm_client=llm)
        manager.set_task_description("Test task")

        messages = [
            {"role": "user", "content": "Current page: Example (https://example.com)"},
            {"role": "assistant", "content": "Thought: click search"},
            {"role": "tool", "content": "Clicked search"},
        ]

        summary = manager.summarize_old_messages(messages)
        self.assertIn("LLM summary result", summary)
        self.assertEqual(llm.calls, 1)


if __name__ == "__main__":
    unittest.main()
