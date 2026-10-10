"""Tests for kGenAi (key and package checks only - no API call, no real key).
Run: python -m unittest discover -s kshared/tests -p "test_kGenAi.py" -v
"""
import os
import sys
import unittest
from unittest import mock

KSHARED = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KSHARED)
import kGenAi as kGenAiModule  # noqa: E402
from kGenAi import kGenAi  # noqa: E402
from kShared import ToolInputException, ToolReportableException, kS  # noqa: E402

MESSAGE = "GEMINI_API_KEY environment variable required (get one from https://aistudio.google.com/apikey)"


class kGenAiKeyTests(unittest.TestCase):

    def setUp(self):
        kS.Reset()
        self.Env = {Name: Value for Name, Value in os.environ.items()
                    if Name not in ("GEMINI_API_KEY", "GOOGLE_API_KEY")}

    def tearDown(self):
        kS.Reset()

    def test_MissingKeyIsBadInput(self):
        with mock.patch.dict(os.environ, self.Env, clear=True):
            self.assertFalse(kGenAi.HasKey())
            with self.assertRaises(ToolInputException) as Caught:
                kGenAi.RequireKey()
        self.assertEqual(str(Caught.exception), MESSAGE)
        self.assertEqual(Caught.exception.ExitCode, 2)
        self.assertFalse(kS.ErrorMode)

    def test_HintIsAppended(self):
        with mock.patch.dict(os.environ, self.Env, clear=True):
            with self.assertRaises(ToolInputException) as Caught:
                kGenAi.RequireKey(" Or pass --no-llm.")
        self.assertEqual(str(Caught.exception), MESSAGE + " Or pass --no-llm.")

    def test_EmptyKeyCountsAsMissing(self):
        with mock.patch.dict(os.environ, dict(self.Env, GEMINI_API_KEY=""), clear=True):
            with self.assertRaises(ToolInputException):
                kGenAi.RequireKey()

    def test_GeminiKeyWins(self):
        with mock.patch.dict(os.environ, dict(self.Env, GEMINI_API_KEY="g-fake", GOOGLE_API_KEY="o-fake"),
                             clear=True):
            self.assertEqual(kGenAi.RequireKey(), "g-fake")
            self.assertTrue(kGenAi.HasKey())

    def test_GoogleKeyAccepted(self):
        with mock.patch.dict(os.environ, dict(self.Env, GOOGLE_API_KEY="o-fake"), clear=True):
            self.assertEqual(kGenAi.RequireKey(), "o-fake")

    def test_ErrorModeDefaults(self):
        kS.ErrorMode = True
        self.assertEqual(kGenAi.RequireKey(), "")
        self.assertFalse(kGenAi.HasKey())
        self.assertEqual(kGenAi.Load("x.py"), (None, None, None))


class kGenAiLoadTests(unittest.TestCase):

    def setUp(self):
        kS.Reset()

    def test_MissingPackageIsReportableExit1(self):
        with mock.patch.object(kGenAiModule.importlib.util, "find_spec", return_value=None):
            with self.assertRaises(ToolReportableException) as Caught:
                kGenAi.Load("batch_image_gen.py")
        self.assertEqual(Caught.exception.ExitCode, 1)
        self.assertIn("uvx --with google-genai python batch_image_gen.py", str(Caught.exception))
        self.assertFalse(kS.ErrorMode)


if __name__ == "__main__":
    unittest.main()
