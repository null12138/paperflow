import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paperflow.config import load_env


class ConfigTests(unittest.TestCase):
    def test_load_env_and_preserve_explicit_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("A_TEST_KEY=from-file\nexport B_TEST_KEY='quoted'\n", encoding="utf-8")
            with patch.dict(os.environ, {"A_TEST_KEY": "explicit"}, clear=False):
                os.environ.pop("B_TEST_KEY", None)
                self.assertEqual(load_env(path), path)
                self.assertEqual(os.environ["A_TEST_KEY"], "explicit")
                self.assertEqual(os.environ["B_TEST_KEY"], "quoted")
                os.environ.pop("B_TEST_KEY", None)


if __name__ == "__main__":
    unittest.main()
