import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from error_handler import ErrorHandler  # noqa: E402
from models import ActionResult  # noqa: E402


class ErrorHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_with_retry_returns_models_action_result(self) -> None:
        handler = ErrorHandler(max_retries=0)

        async def action_func(**_kwargs):  # noqa: ANN003
            return "ok"

        result = await handler.execute_with_retry(action_func=action_func, context={}, strategy="simple")
        self.assertIsInstance(result, ActionResult)
        self.assertTrue(result.success)
        self.assertEqual(result.retry_count, 0)

    async def test_execute_with_retry_failure_sets_error_fields(self) -> None:
        handler = ErrorHandler(max_retries=0)

        async def action_func(**_kwargs):  # noqa: ANN003
            raise ValueError("boom")

        result = await handler.execute_with_retry(action_func=action_func, context={}, strategy="simple")
        self.assertIsInstance(result, ActionResult)
        self.assertFalse(result.success)
        self.assertIn("boom", result.message)
        self.assertEqual(result.error_type, "validation_error")


if __name__ == "__main__":
    unittest.main()
