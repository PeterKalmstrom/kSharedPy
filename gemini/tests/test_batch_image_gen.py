"""Tests for batch_image_gen.py, run as a real child process. No network and no real key: the child imports the
fake google.genai from kshared/tests/support/stubs (asserted below), GEMINI_API_KEY/GOOGLE_API_KEY are removed or
set to a dummy, and HTTPS_PROXY points to a closed local port.

Run: python -m unittest discover -s gemini/tests -v
"""
import json
import os
import sys
import unittest

TESTS = os.path.dirname(os.path.abspath(__file__))
GEMINI = os.path.dirname(TESTS)
sys.path.insert(0, os.path.join(os.path.dirname(GEMINI), "kshared", "tests", "support"))
import kScriptHarness as H  # noqa: E402

SCRIPT = os.path.join(GEMINI, "batch_image_gen.py")
MISSING_KEY = "GEMINI_API_KEY environment variable required"
PNG = b"\x89PNG\r\n\x1a\n"


class kImageGenTests(unittest.TestCase):

    def setUp(self):
        self.Work = H.NewWorkFolder("gemini")
        self.Log = os.path.join(self.Work, "stub-log.jsonl")

    def tearDown(self):
        H.RemoveWorkFolder(self.Work)

    def Run(self, *Args, Env=None):
        return H.Run(SCRIPT, list(Args), self.Work, Env if Env is not None else H.KeyEnv(KSTUB_LOG=self.Log))

    def WriteSpec(self, Spec, Name="spec.json"):
        Path = os.path.join(self.Work, Name)
        with open(Path, "w", encoding="utf-8") as File:
            File.write(Spec if isinstance(Spec, str) else json.dumps(Spec))
        return Path

    def ExpectBadInput(self, Result, Text):
        Code, _, Err = Result
        self.assertEqual(Code, 2, Err)
        self.assertIn(Text, Err)
        self.assertNotIn("Traceback", Err)
        self.assertFalse(os.path.exists(self.Log), "the API client must not be created for bad input")

    def test_StubsAreImported(self):
        Lines = H.StubsAreImported(self.Work)
        self.assertEqual(len(Lines), 3, Lines)
        self.assertTrue(os.path.realpath(Lines[0]).startswith(os.path.realpath(H.STUBS)), Lines[0])
        self.assertEqual(Lines[2], "True")

    def test_Help(self):
        Code, Out, Err = self.Run("--help")
        self.assertEqual(Code, 0, Err)
        self.assertIn("Batch image generation", Out)
        self.assertIn("--spec", Out)

    def test_NoPromptNoSpec(self):
        self.ExpectBadInput(self.Run(), "Either --prompt + --out OR --spec required")

    def test_PromptWithoutOut(self):
        self.ExpectBadInput(self.Run("--prompt", "an apple"), "--prompt requires --out")

    def test_MissingKey(self):
        self.ExpectBadInput(self.Run("--prompt", "an apple", "--out", "a.png", Env=H.BaseEnv(KSTUB_LOG=self.Log)),
                            MISSING_KEY)

    def test_GoogleKeyIsAccepted(self):
        Code, _, Err = self.Run("--prompt", "an apple", "--out", "a.png", "--quiet",
                                Env=H.BaseEnv(GOOGLE_API_KEY=H.DUMMY_KEY, KSTUB_LOG=self.Log))
        self.assertEqual(Code, 0, Err)
        self.assertEqual(H.StubLog(self.Log)[0]["api_key"], H.DUMMY_KEY)

    def test_MissingSpec(self):
        self.ExpectBadInput(self.Run("--spec", "nothere.json"), "spec file not found")

    def test_SpecNotJson(self):
        self.ExpectBadInput(self.Run("--spec", self.WriteSpec("[{\"prompt\": ")), "spec file is not valid JSON")

    def test_SpecNotAList(self):
        self.ExpectBadInput(self.Run("--spec", self.WriteSpec({"prompt": "x", "out": "x.png"})),
                            "spec must be a JSON list")

    def test_SpecEntryWithoutOut(self):
        Spec = self.WriteSpec([{"prompt": "a", "out": "a.png"}, {"prompt": "b"}])
        self.ExpectBadInput(self.Run("--spec", Spec), "spec entry 1 needs \"prompt\" and \"out\"")
        self.assertFalse(os.path.exists(os.path.join(self.Work, "a.png")), "validated in full before any call")

    def test_SingleImage(self):
        Out = os.path.join(self.Work, "sub", "apple.png")
        Code, Stdout, Err = self.Run("--prompt", "an apple", "--out", Out, "--aspect", "1:1")
        self.assertEqual(Code, 0, Err)
        with open(Out, "rb") as File:
            self.assertEqual(File.read(), PNG + b"STUB:an apple")
        self.assertIn("1/1 images generated.", Err)  # progress is stderr (kConsole, 2026-10-10)
        self.assertEqual(Stdout, "")
        Calls = H.StubLog(self.Log)
        self.assertEqual([Call["kind"] for Call in Calls], ["client", "generate_content"])
        self.assertEqual(Calls[0]["api_key"], H.DUMMY_KEY)
        self.assertEqual(Calls[1]["model"], "gemini-3.1-flash-image-preview")

    def test_BatchWithOneRefusal(self):
        Spec = self.WriteSpec([
            {"prompt": "first", "out": "one.png"},
            {"prompt": "REFUSE this", "out": "two.png"},
            {"prompt": "NOIMAGE please", "out": "three.png"},
            {"prompt": "fourth", "out": "four.png", "model": "other-image-model"},
        ])
        Code, Stdout, Err = self.Run("--spec", Spec)
        self.assertEqual(Code, 1, Err)
        self.assertIn("ERROR: APIError: 400 stub refusal", Err)
        self.assertIn("ERROR: no image returned for three.png", Err)
        self.assertNotIn("Traceback", Err)
        self.assertIn("2/4 images generated.", Err)
        self.assertEqual(Stdout, "")
        for Name, Exists in (("one.png", True), ("two.png", False), ("three.png", False), ("four.png", True)):
            self.assertEqual(os.path.isfile(os.path.join(self.Work, Name)), Exists, Name)
        Models = [Call["model"] for Call in H.StubLog(self.Log) if Call["kind"] == "generate_content"]
        self.assertEqual(len(Models), 4)
        self.assertEqual(Models[3], "other-image-model")

    def test_QuietStillPrintsRefusal(self):
        Code, Stdout, Err = self.Run("--prompt", "REFUSE", "--out", "x.png", "--quiet")
        self.assertEqual(Code, 1)
        self.assertEqual(Stdout, "")
        self.assertIn("ERROR: APIError", Err)
        self.assertNotIn("Generating", Err)  # --quiet suppresses progress only

    def test_UnexpectedFailureIsReported(self):
        Code, _, Err = self.Run("--prompt", "an apple", "--out", "a.png",
                                Env=H.KeyEnv(KSTUB_LOG=self.Log, STUB_MODE="fail"))
        self.assertEqual(Code, 1, Err)
        self.assertIn("stub API down", Err)
        self.assertIn("kImageGenerator.Request", Err)


if __name__ == "__main__":
    unittest.main()
