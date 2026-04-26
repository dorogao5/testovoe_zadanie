import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import main  # noqa: E402


class MainConfigTests(unittest.TestCase):
    def test_resolve_headless_from_env(self) -> None:
        with patch.dict(os.environ, {"BROWSER_HEADLESS": "true"}, clear=False):
            self.assertTrue(main._resolve_headless(cli_headless=False))

    def test_cli_headless_has_priority(self) -> None:
        with patch.dict(os.environ, {"BROWSER_HEADLESS": "false"}, clear=False):
            self.assertTrue(main._resolve_headless(cli_headless=True))

    def test_profile_dir_from_env(self) -> None:
        with patch.dict(os.environ, {"BROWSER_PROFILE_DIR": "./profile"}, clear=False):
            self.assertEqual(main._resolve_profile_dir(None), "./profile")

    def test_cli_profile_dir_has_priority(self) -> None:
        with patch.dict(os.environ, {"BROWSER_PROFILE_DIR": "./env-profile"}, clear=False):
            self.assertEqual(main._resolve_profile_dir("./cli-profile"), "./cli-profile")


if __name__ == "__main__":
    unittest.main()
