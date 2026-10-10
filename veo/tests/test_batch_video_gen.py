"""Tests for batch_video_gen.py, run as a real child process. No network and no real key: the child imports the
fake google.genai from kshared/tests/support/stubs (asserted below), GEMINI_API_KEY/GOOGLE_API_KEY are removed or
set to a dummy, and HTTPS_PROXY points to a closed local port. The stub's operations finish at once (no polling).

Run: python -m unittest discover -s veo/tests -v
"""
import json
import os
import shutil
import subprocess
import sys
import unittest

TESTS = os.path.dirname(os.path.abspath(__file__))
VEO = os.path.dirname(TESTS)
sys.path.insert(0, os.path.join(os.path.dirname(VEO), "kshared", "tests", "support"))
import kScriptHarness as H  # noqa: E402

SCRIPT = os.path.join(VEO, "batch_video_gen.py")
MISSING_KEY = "GEMINI_API_KEY environment variable required"
HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


class kVideoGenTests(unittest.TestCase):

    def setUp(self):
        self.Work = H.NewWorkFolder("veo")
        self.Log = os.path.join(self.Work, "stub-log.jsonl")
        self.Image = os.path.join(self.Work, "start.png")
        with open(self.Image, "wb") as File:
            File.write(b"\x89PNG\r\n\x1a\nfake")

    def tearDown(self):
        H.RemoveWorkFolder(self.Work)

    def Run(self, *Args, Env=None):
        return H.Run(SCRIPT, list(Args), self.Work, Env if Env is not None else H.KeyEnv(KSTUB_LOG=self.Log))

    def WriteSpec(self, Spec):
        Path = os.path.join(self.Work, "spec.json")
        with open(Path, "w", encoding="utf-8") as File:
            File.write(Spec if isinstance(Spec, str) else json.dumps(Spec))
        return Path

    def ExpectBadInput(self, Result, Text):
        Code, _, Err = Result
        self.assertEqual(Code, 2, Err)
        self.assertIn(Text, Err)
        self.assertNotIn("Traceback", Err)
        self.assertFalse(os.path.exists(self.Log), "the API client must not be created for bad input")

    def Videos(self):
        return [Call for Call in H.StubLog(self.Log) if Call["kind"] == "generate_videos"]

    def test_StubsAreImported(self):
        Lines = H.StubsAreImported(self.Work)
        self.assertEqual(len(Lines), 3, Lines)
        for Line in Lines[:2]:
            self.assertTrue(os.path.realpath(Line).startswith(os.path.realpath(H.STUBS)), Line)
        self.assertEqual(Lines[2], "True")

    def test_Help(self):
        Code, Out, Err = self.Run("--help")
        self.assertEqual(Code, 0, Err)
        self.assertIn("Batch video generation", Out)
        self.assertIn("--negative-prompt", Out)

    def test_NoPromptNoSpec(self):
        self.ExpectBadInput(self.Run(), "Either --prompt + --out OR --spec required")

    def test_PromptWithoutOut(self):
        self.ExpectBadInput(self.Run("--prompt", "waves"), "--prompt requires --out")

    def test_MissingKey(self):
        self.ExpectBadInput(self.Run("--prompt", "waves", "--out", "w.mp4", Env=H.BaseEnv(KSTUB_LOG=self.Log)),
                            MISSING_KEY)

    def test_MissingSpec(self):
        self.ExpectBadInput(self.Run("--spec", "nothere.json"), "spec file not found")

    def test_SpecNotJson(self):
        self.ExpectBadInput(self.Run("--spec", self.WriteSpec("{oops")), "spec file is not valid JSON")

    def test_SpecEntryWithoutPrompt(self):
        self.ExpectBadInput(self.Run("--spec", self.WriteSpec([{"out": "a.mp4"}])), "spec entry 0 needs")

    def test_MissingImageArgument(self):
        self.ExpectBadInput(self.Run("--prompt", "waves", "--out", "w.mp4", "--image", "nothere.png"),
                            "starting-frame image not found: nothere.png")

    def test_MissingImageInSpecStopsBeforeAnyCall(self):
        Spec = self.WriteSpec([{"prompt": "a", "out": "a.mp4"}, {"prompt": "b", "out": "b.mp4", "image": "gone.png"}])
        self.ExpectBadInput(self.Run("--spec", Spec), "starting-frame image not found: gone.png")
        self.assertFalse(os.path.exists(os.path.join(self.Work, "a.mp4")))

    def test_MuteWithoutFfmpeg(self):
        Env = H.KeyEnv(KSTUB_LOG=self.Log, PATH=os.path.dirname(sys.executable))
        self.ExpectBadInput(self.Run("--prompt", "waves", "--out", "w.mp4", "--mute", Env=Env),
                            "--mute needs ffmpeg")

    def test_SingleVideoWithOptions(self):
        Out = os.path.join(self.Work, "sub", "waves.mp4")
        Code, Stdout, Err = self.Run("--prompt", "waves", "--out", Out, "--image", "start.png", "--resolution",
                                     "1080p", "--negative-prompt", "text, logos")
        self.assertEqual(Code, 0, Err)
        with open(Out, "rb") as File:
            self.assertEqual(File.read(), b"STUBVIDEO:waves")
        self.assertIn("1/1 videos generated.", Err)  # progress is stderr (kConsole, 2026-10-10)
        self.assertIn("  polling", Err)
        self.assertEqual(Stdout, "")
        Calls = H.StubLog(self.Log)
        self.assertEqual(Calls[0]["api_key"], H.DUMMY_KEY)
        Video = self.Videos()[0]
        self.assertEqual(Video["model"], "veo-3.1-generate-preview")
        self.assertEqual(Video["image_mime"], "image/png")
        self.assertEqual(Video["image_size"], 12)
        self.assertEqual(Video["resolution"], "1080p")
        self.assertEqual(Video["negative_prompt"], "text, logos")

    def test_BatchWithOneRefusal(self):
        Spec = self.WriteSpec([
            {"prompt": "first", "out": "one.mp4"},
            {"prompt": "REFUSE this", "out": "two.mp4"},
            {"prompt": "NOVIDEO please", "out": "three.mp4"},
            {"prompt": "fourth", "out": "four.mp4", "model": "veo-3.1-fast-generate-preview"},
        ])
        Code, Stdout, Err = self.Run("--spec", Spec)
        self.assertEqual(Code, 1, Err)
        self.assertIn("ERROR: APIError: 429 RESOURCE_EXHAUSTED stub refusal", Err)
        self.assertIn("ERROR: no video returned", Err)
        self.assertNotIn("Traceback", Err)
        self.assertIn("2/4 videos generated.", Err)
        self.assertEqual(Stdout, "")
        for Name, Exists in (("one.mp4", True), ("two.mp4", False), ("three.mp4", False), ("four.mp4", True)):
            self.assertEqual(os.path.isfile(os.path.join(self.Work, Name)), Exists, Name)
        self.assertEqual([Video["model"] for Video in self.Videos()][3], "veo-3.1-fast-generate-preview")

    @unittest.skipUnless(HAS_FFMPEG, "ffmpeg and ffprobe not on PATH")
    def test_MuteStripsAudio(self):
        Clip = os.path.join(self.Work, "clip.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10:duration=1",
                        "-f", "lavfi", "-i", "sine=duration=1", "-c:v", "libx264", "-preset", "ultrafast",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", Clip], check=True, capture_output=True, timeout=120)
        Code, _, Err = self.Run("--prompt", "waves", "--out", "muted.mp4", "--mute", "--quiet",
                                Env=H.KeyEnv(KSTUB_LOG=self.Log, KSTUB_VIDEO=Clip))
        self.assertEqual(Code, 0, Err)
        Probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0",
                                os.path.join(self.Work, "muted.mp4")], capture_output=True, text=True, check=True)
        self.assertEqual(Probe.stdout.split(), ["video"])


if __name__ == "__main__":
    unittest.main()
