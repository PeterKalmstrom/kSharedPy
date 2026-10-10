"""Regression tests for the vedit scripts: each script runs as a real child process on the small fixtures in
vedit/tests/fixtures, and its exit code, stdout, stderr and output files are compared with fixtures/expected.

Run: python -m unittest discover -s vedit/tests -v

- No network and no real key: the child imports the fake google.genai and static_ffmpeg from
  kshared/tests/support/stubs (asserted by test_StubsAreImported); see kshared/tests/support/kScriptHarness.py.
- The media tests (apply, audio_check, propose) run only where ffmpeg is on PATH. They make a 12 s test clip with
  ffmpeg at test time (no binary media is committed) and compare media by stream properties and durations, and
  measured numbers with a tolerance, because encoders and filters differ between ffmpeg builds.
- KSHAREDPY_UPDATE_EXPECTED=1 rewrites fixtures/expected (review the diff before committing).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

TESTS = os.path.dirname(os.path.abspath(__file__))
VEDIT = os.path.dirname(TESTS)
FIXTURES = os.path.join(TESTS, "fixtures")
EXPECTED = os.path.join(FIXTURES, "expected")
sys.path.insert(0, os.path.join(os.path.dirname(VEDIT), "kshared", "tests", "support"))
import kScriptHarness as H  # noqa: E402

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def Script(Name):
    return os.path.join(VEDIT, Name)


def MakeClip(Path):
    """12 s, 320x180, 30 fps test pattern. Audio a:0: a 440 Hz tone with silence from 3 s to 7 s; a:1: a louder
    660 Hz tone throughout (a different "microphone" for audio_check)."""
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=12",
        "-f", "lavfi", "-i", "aevalsrc=0.1*sin(2*PI*440*t)*not(between(t\\,3\\,7)):s=44100:d=12",
        "-f", "lavfi", "-i", "aevalsrc=0.3*sin(2*PI*660*t):s=44100:d=12",
        "-map", "0:v", "-map", "1:a", "-map", "2:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "1",
        Path], check=True, capture_output=True, timeout=120)


def ProbeSummary(Path):
    """The stream layout and durations of a media file - what must stay the same across ffmpeg builds."""
    Result = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                             "stream=codec_type,codec_name,width,height,r_frame_rate,channels:format=duration",
                             "-of", "json", Path], capture_output=True, text=True, check=True, timeout=60)
    Data = json.loads(Result.stdout)
    Streams = []
    for Stream in Data["streams"]:
        Keep = {"codec_type": Stream["codec_type"], "codec_name": Stream["codec_name"]}
        if Stream["codec_type"] == "video":
            Keep.update(width=Stream["width"], height=Stream["height"], r_frame_rate=Stream["r_frame_rate"])
        else:
            Keep["channels"] = Stream["channels"]
        Streams.append(Keep)
    return {"streams": Streams, "duration": round(float(Data["format"]["duration"]), 2)}


def MaskNumbers(Text):
    """Decimal numbers -> <N> and runs of spaces -> one (measured values, and so the column widths, vary slightly
    between ffmpeg builds). A number glued to a name ("gemini-2.5-flash") stays."""
    return re.sub(r" +", " ", re.sub(r"(?<![\w-])[-+]?\d+\.\d+", "<N>", Text))


class kVeditCase(unittest.TestCase):
    """A fresh work folder holding a copy of the fixtures; scripts run with it as the current folder."""

    def setUp(self):
        self.Work = H.NewWorkFolder("vedit")
        for Name in os.listdir(FIXTURES):
            if Name == "expected":
                continue
            Source = os.path.join(FIXTURES, Name)
            if os.path.isdir(Source):
                shutil.copytree(Source, os.path.join(self.Work, Name))
            else:
                shutil.copy(Source, self.Work)
        self.Log = os.path.join(self.Work, "stub-log.jsonl")

    def tearDown(self):
        H.RemoveWorkFolder(self.Work)

    def Run(self, Name, *Args, Env=None):
        """(exit code, stdout, stderr) of vedit/<Name> with the work folder normalised to <WORK>."""
        Code, Out, Err = H.Run(Script(Name), list(Args), self.Work, Env if Env is not None else H.BaseEnv())
        return Code, H.Normalise(Out, {"<WORK>": self.Work}), H.Normalise(Err, {"<WORK>": self.Work})

    def Expect(self, Case, Code, Out, Err):
        """Compare exit code, stdout and stderr with expected/<Case>.txt."""
        H.AssertMatches(self, f"rc={Code}\n--- stdout\n{Out}\n--- stderr\n{Err}", os.path.join(EXPECTED, Case + ".txt"))

    def ExpectFile(self, Name, Expected=None):
        """Compare the work-folder file Name with expected/<Expected or Name>."""
        Path = os.path.join(self.Work, Name)
        self.assertTrue(os.path.isfile(Path), f"not written: {Name}")
        H.AssertMatches(self, H.ReadText(Path), os.path.join(EXPECTED, Expected or Name))

    def ExpectBadInput(self, Code, Err, Text):
        self.assertEqual(Code, 2, Err)
        self.assertIn(Text, Err)
        self.assertNotIn("Traceback", Err)
        self.assertNotIn("ERROR-REPORT-PENDING", Err)


class kStubTests(kVeditCase):

    def test_StubsAreImported(self):
        Lines = H.StubsAreImported(self.Work)
        self.assertEqual(len(Lines), 3, Lines)
        for Line in Lines[:2]:
            self.assertTrue(os.path.realpath(Line).startswith(os.path.realpath(H.STUBS)), Line)
        self.assertEqual(Lines[2], "True")


class kSrtToScriptTests(kVeditCase):

    def test_DefaultWrap(self):
        self.Expect("s2s1", *self.Run("srt_to_script.py", "--in", "sample.srt"))

    def test_ParagraphsToFile(self):
        Code, Out, Err = self.Run("srt_to_script.py", "--in", "sample.srt", "--paragraphs",
                                  "--out", os.path.join(self.Work, "script_p.txt"))
        self.Expect("s2s2", Code, Out, Err)
        self.ExpectFile("script_p.txt")

    def test_LastWidthWins(self):
        self.Expect("s2s3", *self.Run("srt_to_script.py", "--in", "sample.srt", "--width", "0", "--width", "20",
                                      "--paragraphs"))

    def test_MissingInputIsBadInput(self):
        Code, Out, Err = self.Run("srt_to_script.py", "--in", "missing.srt")
        self.ExpectBadInput(Code, Err, "input not found: missing.srt")
        self.Expect("s2s4", Code, Out, Err)


class kSubtitlesTests(kVeditCase):

    def test_CutTimelineNoPolish(self):
        Code, Out, Err = self.Run("subtitles.py", "--no-polish", "--transcript", "transcript.json", "--edl", "edl.json",
                                  "--out", os.path.join(self.Work, "cut.srt"), "--max-line", "20")
        self.Expect("sub1", Code, Out, Err)
        self.ExpectFile("cut.srt")

    def test_SourceTimelineVtt(self):
        Code, Out, Err = self.Run("subtitles.py", "--no-polish", "--transcript", "transcript.json",
                                  "--out", os.path.join(self.Work, "src.vtt"))
        self.Expect("sub2", Code, Out, Err)
        self.ExpectFile("src.vtt")

    def test_NoKeySkipsPolishWithWarning(self):
        Code, Out, Err = self.Run("subtitles.py", "--transcript", "transcript.json", "--edl", "edl.json",
                                  "--out", os.path.join(self.Work, "nokey.srt"))
        self.Expect("sub3", Code, Out, Err)
        self.ExpectFile("nokey.srt")
        self.assertFalse(os.path.exists(self.Log))

    def test_UnknownExtensionIsBadInput(self):
        Code, Out, Err = self.Run("subtitles.py", "--no-polish", "--transcript", "transcript.json", "--out", "bad.txt")
        self.ExpectBadInput(Code, Err, "unsupported format 'txt'")
        self.Expect("sub4", Code, Out, Err)

    def test_FormatOverridesExtension(self):
        Code, Out, Err = self.Run("subtitles.py", "--no-polish", "--transcript", "transcript.json",
                                  "--out", os.path.join(self.Work, "forced.txt"), "--format", "vtt")
        self.Expect("sub5", Code, Out, Err)
        self.ExpectFile("forced.txt")

    def test_PolishWithFakeGemini(self):
        Code, Out, Err = self.Run("subtitles.py", "--transcript", "transcript.json",
                                  "--out", os.path.join(self.Work, "g3.srt"), Env=H.KeyEnv(KSTUB_LOG=self.Log))
        self.Expect("g3", Code, Out, Err)
        self.ExpectFile("g3.srt")
        Calls = H.StubLog(self.Log)
        self.assertEqual([Call["kind"] for Call in Calls], ["client", "generate_content"])
        self.assertEqual(Calls[0]["env_key"], H.DUMMY_KEY)

    def test_PolishFailureFallsBackToRaw(self):
        Code, Out, Err = self.Run("subtitles.py", "--transcript", "transcript.json",
                                  "--out", os.path.join(self.Work, "g4.srt"), Env=H.KeyEnv(STUB_MODE="fail"))
        self.Expect("g4", Code, Out, Err)
        self.ExpectFile("g4.srt")

    def test_MissingTranscriptIsBadInput(self):
        Code, _, Err = self.Run("subtitles.py", "--no-polish", "--transcript", "nothere.json", "--out", "x.srt")
        self.ExpectBadInput(Code, Err, "transcript not found")

    def test_TruncatedEdlIsBadInput(self):
        Code, _, Err = self.Run("subtitles.py", "--no-polish", "--transcript", "transcript.json",
                                "--edl", "edl_truncated.json", "--out", "x.srt")
        self.ExpectBadInput(Code, Err, "EDL is not valid JSON")

    def test_EdlWithoutSegmentsIsBadInput(self):
        # A valid-JSON EDL without "segments" is bad input (fixed 2026-10-10 via kVeditData; was a KeyError, exit 1).
        Code, _, Err = self.Run("subtitles.py", "--no-polish", "--transcript", "transcript.json",
                                "--edl", "edl_no_segments.json", "--out", "x.srt")
        self.ExpectBadInput(Code, Err, "segments")


class kPolishScriptTests(kVeditCase):

    def test_MissingKeyIsBadInput(self):
        Code, Out, Err = self.Run("polish_script.py", "--in", "sample.srt", "--out", "pol.txt")
        self.ExpectBadInput(Code, Err, "GEMINI_API_KEY environment variable required")
        self.Expect("pol1", Code, Out, Err)

    def test_PolishWithFakeGemini(self):
        Code, Out, Err = self.Run("polish_script.py", "--in", "sample.srt",
                                  "--out", os.path.join(self.Work, "polished.txt"), Env=H.KeyEnv())
        self.Expect("g5", Code, Out, Err)
        self.ExpectFile("polished.txt")

    def test_MissingInputIsBadInput(self):
        Code, _, Err = self.Run("polish_script.py", "--in", "nothere.srt", "--out", "pol.txt", Env=H.KeyEnv())
        self.ExpectBadInput(Code, Err, "input not found: nothere.srt")


class kReviewTests(kVeditCase):

    def test_ReviewAll(self):
        self.Expect("rev1", *self.Run("review.py", "--edl", "edl.json", "--transcript", "transcript.json"))

    def test_ReviewOnlyRetakeToFile(self):
        Code, Out, Err = self.Run("review.py", "--edl", "edl.json", "--transcript", "transcript.json", "--only",
                                  "retake", "--out", os.path.join(self.Work, "review_retake.md"), "--context-s", "5",
                                  "--max-line", "40")
        self.Expect("rev2", Code, Out, Err)
        self.ExpectFile("review_retake.md")

    def test_RestoreToFile(self):
        Code, Out, Err = self.Run("review.py", "--edl", "edl.json", "--restore", "retake",
                                  "--out", os.path.join(self.Work, "restored.json"))
        self.Expect("rev3", Code, Out, Err)
        self.ExpectFile("restored.json")

    def test_RestoreToStdout(self):
        self.Expect("rev4", *self.Run("review.py", "--edl", "edl.json", "--restore", "silence"))

    def test_ReviewNeedsTranscript(self):
        Code, Out, Err = self.Run("review.py", "--edl", "edl.json")
        self.ExpectBadInput(Code, Err, "--transcript required for review mode")
        self.Expect("rev5", Code, Out, Err)

    def test_FilterWithoutMatches(self):
        self.Expect("rev6", *self.Run("review.py", "--edl", "edl.json", "--transcript", "transcript.json",
                                      "--only", "static"))

    def test_MissingEdlIsBadInput(self):
        Code, _, Err = self.Run("review.py", "--edl", "nothere.json", "--transcript", "transcript.json")
        self.ExpectBadInput(Code, Err, "EDL not found")

    def test_TruncatedEdlIsBadInput(self):
        Code, _, Err = self.Run("review.py", "--edl", "edl_truncated.json", "--transcript", "transcript.json")
        self.ExpectBadInput(Code, Err, "EDL is not valid JSON")

    def test_EdlWithoutSegmentsIsBadInput(self):
        # A valid-JSON EDL without "segments" is bad input (fixed 2026-10-10 via kVeditData; was a KeyError, exit 1).
        Code, _, Err = self.Run("review.py", "--edl", "edl_no_segments.json", "--transcript", "transcript.json")
        self.ExpectBadInput(Code, Err, "segments")


class kEdlToTscprojTests(kVeditCase):

    def test_BundleOutput(self):
        Code, Out, Err = self.Run("edl_to_tscproj.py", "--edl", "edl.json", "--tscproj", "Proj.tscproj",
                                  "--out", os.path.join(self.Work, "Out.tscproj"))
        self.Expect("tsc1", Code, Out, Err)
        self.ExpectFile(os.path.join("Out.tscproj", "Out.tscproj"), "Out.tscproj.json")
        self.assertTrue(os.path.isfile(os.path.join(self.Work, "Out.tscproj", "rec.trec")))

    def test_FlatOutputOtherSource(self):
        Code, Out, Err = self.Run("edl_to_tscproj.py", "--edl", "edl.json", "--tscproj",
                                  os.path.join("Proj.tscproj", "Proj.tscproj"),
                                  "--out", os.path.join(self.Work, "flat.tscproj"), "--src", "intro")
        self.Expect("tsc2", Code, Out, Err)
        self.ExpectFile("flat.tscproj", "flat.tscproj.json")

    def test_MissingProjectIsBadInput(self):
        Code, Out, Err = self.Run("edl_to_tscproj.py", "--edl", "edl.json", "--tscproj", "nothere",
                                  "--out", "x.tscproj")
        self.ExpectBadInput(Code, Err, "tscproj not found: nothere")
        self.Expect("tsc3", Code, Out, Err)

    def test_TruncatedEdlIsBadInput(self):
        Code, _, Err = self.Run("edl_to_tscproj.py", "--edl", "edl_truncated.json", "--tscproj", "Proj.tscproj",
                                "--out", "x.tscproj")
        self.ExpectBadInput(Code, Err, "EDL is not valid JSON")

    def test_MissingEdlIsBadInput(self):
        Code, _, Err = self.Run("edl_to_tscproj.py", "--edl", "nothere.json", "--tscproj", "Proj.tscproj",
                                "--out", "x.tscproj")
        self.ExpectBadInput(Code, Err, "EDL not found")

    def test_EdlWithoutSegmentsIsBadInput(self):
        # A valid-JSON EDL without "segments" is bad input (fixed 2026-10-10 via kVeditData; was a KeyError, exit 1).
        Code, _, Err = self.Run("edl_to_tscproj.py", "--edl", "edl_no_segments.json", "--tscproj", "Proj.tscproj",
                                "--out", "x.tscproj")
        self.ExpectBadInput(Code, Err, "segments")


class kMissingMediaTests(kVeditCase):
    """The input checks come before ffmpeg is needed, so these run everywhere."""

    def test_ApplyMissingInput(self):
        Code, Out, Err = self.Run("apply.py", "--in", "nothere.mp4", "--edl", "edl.json", "--out", "x.mp4")
        self.ExpectBadInput(Code, Err, "input not found: nothere.mp4")
        self.Expect("app4", Code, Out, Err)

    def test_AudioCheckMissingInput(self):
        Code, _, Err = self.Run("audio_check.py", "--in", "nothere.mp4", "--calibrate", "--profile-out", "p.json")
        self.ExpectBadInput(Code, Err, "input not found: nothere.mp4")

    def test_ProposeMissingInput(self):
        Code, _, Err = self.Run("propose.py", "--in", "nothere.mp4", "--out", "p.json", "--no-llm")
        self.ExpectBadInput(Code, Err, "input not found: nothere.mp4")


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg and ffprobe not on PATH")
class kMediaCase(kVeditCase):
    """Adds clip.mp4, made once per class with ffmpeg and copied into each work folder."""

    @classmethod
    def setUpClass(cls):
        cls.ClipFolder = H.NewWorkFolder("vedit")
        cls.Clip = os.path.join(cls.ClipFolder, "clip.mp4")
        MakeClip(cls.Clip)

    @classmethod
    def tearDownClass(cls):
        H.RemoveWorkFolder(cls.ClipFolder)

    def setUp(self):
        super().setUp()
        shutil.copy(self.Clip, os.path.join(self.Work, "clip.mp4"))

    def ExpectMedia(self, Name):
        Path = os.path.join(self.Work, Name)
        self.assertTrue(os.path.isfile(Path), f"not written: {Name}")
        Expected = os.path.join(EXPECTED, Name + ".probe.json")
        Actual = ProbeSummary(Path)
        if H.UPDATE:
            H.AssertMatches(self, json.dumps(Actual, indent=2) + "\n", Expected)
            return
        H.AssertJsonClose(self, Actual, json.loads(H.ReadText(Expected)), 0.15)

    def ExpectJson(self, Name, Tolerance):
        H.AssertJsonFileClose(self, os.path.join(self.Work, Name), os.path.join(EXPECTED, Name), Tolerance)


class kApplyTests(kMediaCase):

    def test_Reencode(self):
        Code, Out, Err = self.Run("apply.py", "--in", "clip.mp4", "--edl", "edl.json", "--out", "re.mp4", "--quiet",
                                  "--preset", "ultrafast")
        self.Expect("app1", Code, Out, Err)
        self.ExpectMedia("re.mp4")

    def test_MixAudioFps(self):
        Code, Out, Err = self.Run("apply.py", "--in", "clip.mp4", "--edl", "edl.json", "--out", "mix.mp4",
                                  "--mix-audio", "--audio-fade-ms", "0", "--fps", "25", "--quiet",
                                  "--preset", "ultrafast")
        self.Expect("app2", Code, Out, Err)
        self.ExpectMedia("mix.mp4")

    def test_StreamCopy(self):
        Code, Out, Err = self.Run("apply.py", "--in", "clip.mp4", "--edl", "edl.json", "--out", "copy.mp4", "--copy",
                                  "--quiet")
        self.Expect("app3", Code, Out, Err)
        Summary = ProbeSummary(os.path.join(self.Work, "copy.mp4"))
        self.assertEqual([Stream["codec_type"] for Stream in Summary["streams"]], ["video", "audio"])
        self.assertGreater(Summary["duration"], 5.5)  # stream copy cuts on keyframes: only roughly the 6.1 s kept
        self.assertLess(Summary["duration"], 12.0)

    def test_TruncatedEdlIsBadInput(self):
        Code, _, Err = self.Run("apply.py", "--in", "clip.mp4", "--edl", "edl_truncated.json", "--out", "x.mp4")
        self.ExpectBadInput(Code, Err, "EDL is not valid JSON")

    def test_EdlWithoutSegmentsIsBadInput(self):
        # A valid-JSON EDL without "segments" is bad input (fixed 2026-10-10 via kVeditData; was a KeyError, exit 1).
        Code, _, Err = self.Run("apply.py", "--in", "clip.mp4", "--edl", "edl_no_segments.json", "--out", "x.mp4")
        self.ExpectBadInput(Code, Err, "segments")


class kAudioCheckTests(kMediaCase):

    def Masked(self, Case, Code, Out, Err):
        self.Expect(Case, Code, MaskNumbers(Out), MaskNumbers(Err))

    def test_CalibrateThenCheck(self):
        Code, Out, Err = self.Run("audio_check.py", "--in", "clip.mp4", "--calibrate", "--profile-out",
                                  os.path.join(self.Work, "profile.json"))
        self.Masked("aud1", Code, Out, Err)
        self.ExpectJson("profile.json", 0.5)
        Code, Out, Err = self.Run("audio_check.py", "--in", "clip.mp4", "--profile", "profile.json")
        self.Masked("aud2", Code, Out, Err)
        self.assertIn("PASS", Out)
        Code, Out, Err = self.Run("audio_check.py", "--in", "clip.mp4", "--audio-stream", "1", "--profile",
                                  "profile.json")
        self.Masked("aud3", Code, Out, Err)
        self.assertEqual(Code, 1)
        self.assertIn("FAIL", Out)

    def test_CalibrateNeedsProfileOut(self):
        Code, Out, Err = self.Run("audio_check.py", "--in", "clip.mp4", "--calibrate")
        self.ExpectBadInput(Code, Err, "--profile-out required in calibrate mode")

    def test_CalibrateWithTolerance(self):
        Code, Out, Err = self.Run("audio_check.py", "--in", "clip.mp4", "--calibrate", "--tolerance-db", "1.5",
                                  "--profile-out", os.path.join(self.Work, "profile2.json"), "--quiet")
        self.Masked("aud5", Code, Out, Err)
        self.ExpectJson("profile2.json", 0.5)

    def test_MissingProfileIsBadInput(self):
        Code, _, Err = self.Run("audio_check.py", "--in", "clip.mp4", "--profile", "nothere.json")
        self.ExpectBadInput(Code, Err, "profile not found")


class kProposeTests(kMediaCase):

    def Masked(self, Case, Code, Out, Err):
        self.Expect(Case, Code, MaskNumbers(Out), MaskNumbers(Err))

    def test_SilenceOnly(self):
        Code, Out, Err = self.Run("propose.py", "--in", "clip.mp4", "--out", os.path.join(self.Work, "p1.json"),
                                  "--no-llm")
        self.Masked("pro1", Code, Out, Err)
        self.ExpectJson("p1.json", 0.06)

    def test_TranscriptVadAudioEventsKeepUntil(self):
        Code, Out, Err = self.Run("propose.py", "--in", "clip.mp4", "--transcript", "transcript.json",
                                  "--out", os.path.join(self.Work, "p2.json"), "--no-llm", "--vad-engine",
                                  "transcript", "--silence-min-s", "1", "--trim-audio-events", "--keep-until", "11")
        self.Masked("pro2", Code, Out, Err)
        self.ExpectJson("p2.json", 0.06)

    def test_StaticScreenCheckNoSnap(self):
        Code, Out, Err = self.Run("propose.py", "--in", "clip.mp4", "--transcript", "transcript.json",
                                  "--out", os.path.join(self.Work, "p3.json"), "--no-llm", "--static",
                                  "--static-min-s", "0.5", "--silence-min-s", "1", "--screen-check",
                                  "--speed-up-threshold-s", "2", "--no-word-snap")
        self.Masked("pro3", Code, Out, Err)
        self.ExpectJson("p3.json", 0.06)

    def test_SilenceMaxAndPadding(self):
        Code, Out, Err = self.Run("propose.py", "--in", "clip.mp4", "--out", os.path.join(self.Work, "p4.json"),
                                  "--no-llm", "--silence-min-s", "1", "--silence-max-s", "2",
                                  "--keep-padding-s", "0.1")
        self.Masked("pro4", Code, Out, Err)
        self.ExpectJson("p4.json", 0.06)

    def test_LlmWithoutKeyIsBadInput(self):
        Code, Out, Err = self.Run("propose.py", "--in", "clip.mp4", "--transcript", "transcript.json",
                                  "--out", "p5.json")
        self.ExpectBadInput(Code, Err, "GEMINI_API_KEY environment variable required")
        self.Masked("pro5", Code, Out, Err)

    def test_TranscriptVadNeedsTranscript(self):
        Code, Out, Err = self.Run("propose.py", "--in", "clip.mp4", "--out", "p6.json", "--no-llm",
                                  "--vad-engine", "transcript")
        self.ExpectBadInput(Code, Err, "--vad-engine transcript requires --transcript")

    def test_SpeedUpMax(self):
        Code, Out, Err = self.Run("propose.py", "--in", "clip.mp4", "--transcript", "transcript.json",
                                  "--out", os.path.join(self.Work, "p8.json"), "--no-llm", "--vad-engine",
                                  "transcript", "--silence-min-s", "1", "--screen-check", "--speed-up-threshold-s",
                                  "2", "--speed-up-max-s", "3", "--screen-change-thr", "300")
        self.Masked("pro8", Code, Out, Err)
        self.ExpectJson("p8.json", 0.06)

    def test_FakeGeminiRetakesAndFiller(self):
        Code, Out, Err = self.Run("propose.py", "--in", "clip.mp4", "--transcript", "transcript.json",
                                  "--out", os.path.join(self.Work, "g1.json"), "--validate-retakes", "--trim-filler",
                                  Env=H.KeyEnv(KSTUB_LOG=self.Log))
        self.Masked("g1", Code, Out, Err)
        self.ExpectJson("g1.json", 0.06)
        Kinds = [Call["kind"] for Call in H.StubLog(self.Log)]
        self.assertIn("generate_content", Kinds)

    def test_FakeGeminiNonJsonAnswer(self):
        Code, Out, Err = self.Run("propose.py", "--in", "clip.mp4", "--transcript", "transcript.json",
                                  "--out", os.path.join(self.Work, "g2.json"), "--trim-filler",
                                  Env=H.KeyEnv(STUB_MODE="bad"))
        self.Masked("g2", Code, Out, Err)
        self.ExpectJson("g2.json", 0.06)

    def test_TruncatedTranscriptIsBadInput(self):
        Code, _, Err = self.Run("propose.py", "--in", "clip.mp4", "--transcript", "edl_truncated.json",
                                "--out", "p.json", "--no-llm", "--vad-engine", "transcript")
        self.ExpectBadInput(Code, Err, "transcript is not valid JSON")


if __name__ == "__main__":
    unittest.main()
