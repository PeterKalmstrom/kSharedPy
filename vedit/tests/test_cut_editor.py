"""Unit tests for propose.py's pure logic, in process on small in-memory data (no ffmpeg, no Gemini):
kCutEditor (vedit/_cut_editor.py), the parsing and interval parts of kSilenceDetector (vedit/_silence.py) and the
answer handling of kGeminiCuts (vedit/_gemini_cuts.py).

Run: python -m unittest discover -s vedit/tests -v
"""
import io
import os
import sys
import unittest
from contextlib import redirect_stderr

VEDIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, VEDIT)
from _cut_editor import kCutEditor  # noqa: E402
from _gemini_cuts import kGeminiCuts  # noqa: E402
from _silence import kSilenceDetector  # noqa: E402
from kConsole import kConsole  # noqa: E402
from kShared import kS  # noqa: E402


def Transcript(*Words):
    """A transcript with one segment holding the (start, end) words."""
    return {"segments": [{"start": 0.0, "end": 0.0, "text": "",
                          "words": [{"start": Start, "end": End, "word": " w"} for Start, End in Words]}]}


class kPureCase(unittest.TestCase):

    def setUp(self):
        kS.Reset()
        kConsole.Quiet = True

    def tearDown(self):
        self.assertFalse(kS.ErrorMode, "a reported error halted the code under test")
        kConsole.Quiet = False


class kIntersectTests(kPureCase):

    def test_Overlaps(self):
        self.assertEqual(kCutEditor.Intersect([(0, 5), (8, 12)], [(3, 9), (11, 20)]), [(3, 5), (8, 9), (11, 12)])

    def test_NoOverlapAndTouching(self):
        self.assertEqual(kCutEditor.Intersect([(0, 2)], [(2, 4)]), [])
        self.assertEqual(kCutEditor.Intersect([], [(0, 1)]), [])

    def test_StaticCutsRespectMinAndMax(self):
        Short = [(10, 20), (0, 3)]
        Freezes = [(1, 2.5), (12, 19)]
        self.assertEqual(kCutEditor.StaticCuts(Short, Freezes, 1.0, 0.0),
                         [(1, 2.5, "static+silent"), (12, 19, "static+silent")])
        self.assertEqual(kCutEditor.StaticCuts(Short, Freezes, 1.0, 5.0), [(1, 2.5, "static+silent")])
        self.assertEqual(kCutEditor.StaticCuts(Short, Freezes, 2.0, 0.0), [(12, 19, "static+silent")])


class kSnapTests(kPureCase):

    Words = [{"start": 0.0, "end": 1.0}, {"start": 1.4, "end": 2.0}, {"start": 2.0, "end": 3.0},
             {"start": 4.0, "end": 5.0}]

    def test_SnapsToNearestGapMiddle(self):
        self.assertAlmostEqual(kCutEditor.SnapToSilenceGap(1.1, self.Words, 0.4), 1.2)
        self.assertAlmostEqual(kCutEditor.SnapToSilenceGap(3.8, self.Words, 0.4), 3.5)

    def test_TouchingWordsAreNoGap(self):
        # 2.0 lies between two words that touch: the nearest real gaps (1.2, 3.5) are beyond the drift
        self.assertEqual(kCutEditor.SnapToSilenceGap(2.0, self.Words, 0.4), 2.0)

    def test_NoWordsOrTooFar(self):
        self.assertEqual(kCutEditor.SnapToSilenceGap(7.0, [], 0.4), 7.0)
        self.assertEqual(kCutEditor.SnapToSilenceGap(7.0, self.Words, 0.4), 7.0)

    def test_WordSnapDropsCollapsedCuts(self):
        Cuts = [(1.1, 3.8, "silence>3s"), (1.15, 1.25, "retake: x")]
        self.assertEqual(kCutEditor.WordSnapCuts(Cuts, self.Words, 0.4), [(1.2, 3.5, "silence>3s")])

    def test_FlattenWordsSortsAcrossSegments(self):
        Data = {"segments": [{"words": [{"start": 5, "end": 6}]}, {"words": None},
                             {"words": [{"start": 1, "end": 2}]}]}
        self.assertEqual(kCutEditor.FlattenWords(Data), [{"start": 1.0, "end": 2.0}, {"start": 5.0, "end": 6.0}])

    def test_PadCuts(self):
        self.assertEqual(kCutEditor.PadCuts([(1.0, 3.0, "a"), (5.0, 5.4, "b")], 0.25), [(1.25, 2.75, "a")])


class kMergeTests(kPureCase):

    def test_MergesOverlappingAndTouching(self):
        Cuts = [(5.0, 6.0, "retake: b"), (0.0, 2.0, "silence>3s"), (1.5, 3.0, "filler: a"), (3.0005, 4.0, "silence>3s")]
        self.assertEqual(kCutEditor.MergeCuts(Cuts),
                         [(0.0, 4.0, "silence>3s+filler: a"), (5.0, 6.0, "retake: b")])  # a reason already joined is not repeated

    def test_SameReasonIsNotRepeated(self):
        self.assertEqual(kCutEditor.MergeCuts([(0, 2, "x"), (1, 3, "x")]), [(0, 3, "x")])

    def test_Empty(self):
        self.assertEqual(kCutEditor.MergeCuts([]), [])


class kClassifyTests(kPureCase):

    def Classify(self, Silences, Changes=(), MaxS=0.0, ScreenCheck=False, MaxSpeedS=0.0):
        return kCutEditor.ClassifySilences(Silences, list(Changes), 3.0, MaxS, ScreenCheck, 2.0, 4.0, 5.0, MaxSpeedS)

    def test_PlainSilencesAreCut(self):
        Cuts, Speed, Counts = self.Classify([(0, 3.5), (10, 20)])
        self.assertEqual(Cuts, [(0, 3.5, "silence>3s"), (10, 20, "silence>3s")])
        self.assertEqual(Speed, [])
        self.assertEqual(Counts, {"SkippedLong": 0, "ScreenActiveSkipped": 0, "DemoPayoffKept": 0})

    def test_SilenceMaxSkipsLongOnes(self):
        Cuts, _, Counts = self.Classify([(0, 3.5), (10, 20)], MaxS=5.0)
        self.assertEqual(Cuts, [(0, 3.5, "silence>3s")])
        self.assertEqual(Counts["SkippedLong"], 1)

    def test_ScreenCheckPreservesSpeedsUpAndKeeps(self):
        # (0,3.5): short static -> cut; (10,16): screen change at 17 (within the 2 s window) -> preserved;
        # (30,36): static and >= 4 s -> sped up; (50,80): static but beyond the 10 s cap -> kept at 1x
        Cuts, Speed, Counts = self.Classify([(0, 3.5), (10, 16), (30, 36), (50, 80)], Changes=[17.0],
                                            ScreenCheck=True, MaxSpeedS=10.0)
        self.assertEqual(Cuts, [(0, 3.5, "silence>3s")])
        self.assertEqual(Speed, [(30, 36, "screen_inactive>4s", 5.0)])
        self.assertEqual(Counts, {"SkippedLong": 0, "ScreenActiveSkipped": 1, "DemoPayoffKept": 1})

    def test_HasScreenChangeWindow(self):
        self.assertTrue(kSilenceDetector.HasScreenChange(10, 16, [17.9], 2.0))
        self.assertFalse(kSilenceDetector.HasScreenChange(10, 16, [18.0], 2.0))
        self.assertFalse(kSilenceDetector.HasScreenChange(10, 16, [], 2.0))


class kAudioEventTests(kPureCase):

    def test_IsFullySilent(self):
        Silences = [(0.0, 1.0), (1.01, 2.0), (3.0, 4.0)]
        self.assertTrue(kCutEditor.IsFullySilent(Silences, 0.5, 1.9))
        self.assertFalse(kCutEditor.IsFullySilent(Silences, 1.5, 3.5))
        self.assertFalse(kCutEditor.IsFullySilent([], 0.0, 1.0))

    def test_WordGapsWindow(self):
        Data = Transcript((0, 1), (1.05, 2), (2.5, 3), (6, 7))
        self.assertEqual(kCutEditor.WordGaps(Data, 0.1, 2.0), [(2, 2.5)])

    def test_GapsWithSoundAreClassifiedByLength(self):
        Gaps = [(1.0, 1.2), (2.0, 2.5), (3.0, 4.0), (5.0, 5.5)]
        Silences = [(4.9, 5.6)]  # only the last gap is silent
        self.assertEqual(kCutEditor.AudioEventsInGaps(Gaps, Silences), [
            (1.0, 1.2, "audio_event: mouth_sound"),
            (2.0, 2.5, "audio_event: filler_sound"),
            (3.0, 4.0, "audio_event: throat_clear"),
        ])


class kBuildEdlTests(kPureCase):

    def test_CutsAndSpeedChainOverTheWholeTimeline(self):
        Edl = kCutEditor.BuildEdl([(2.0, 3.0, "silence>3s"), (-1.0, 0.5, "retake: x"), (9.0, 12.0, "tail")], 10.0,
                                  "in.mp4", SpeedSegments=[(5.0, 7.0, "screen_inactive>4s", 5.0)])
        self.assertEqual(Edl["source"], "in.mp4")
        self.assertEqual(Edl["duration_s"], 10.0)
        self.assertEqual(Edl["version"], 1)
        self.assertEqual(Edl["segments"], [
            {"start": 0.0, "end": 0.5, "keep": False, "reason": "retake: x"},
            {"start": 0.5, "end": 2.0, "keep": True, "reason": ""},
            {"start": 2.0, "end": 3.0, "keep": False, "reason": "silence>3s"},
            {"start": 3.0, "end": 5.0, "keep": True, "reason": ""},
            {"start": 5.0, "end": 7.0, "keep": True, "reason": "", "speed": 5.0},
            {"start": 7.0, "end": 9.0, "keep": True, "reason": ""},
            {"start": 9.0, "end": 10.0, "keep": False, "reason": "tail"},
        ])

    def test_NoCutsIsOneKeepSegment(self):
        self.assertEqual(kCutEditor.BuildEdl([], 4.25, "x")["segments"],
                         [{"start": 0.0, "end": 4.25, "keep": True, "reason": ""}])


class kSilenceParsingTests(kPureCase):

    def test_ParseSilenceLog(self):
        Log = ("[silencedetect @ 0x1] silence_start: 3.002\n"
               "[silencedetect @ 0x1] silence_end: 7.01 | silence_duration: 4.008\n"
               "[silencedetect @ 0x1] silence_end: 9.0 | stray end without start\n"
               "[silencedetect @ 0x1] silence_start: 11.5\n")  # still open at EOF: dropped
        self.assertEqual(kSilenceDetector.ParseSilenceLog(Log), [(3.002, 7.01)])

    def test_ParseFreezeLog(self):
        Log = ("lavfi.freezedetect.freeze_start: 1.5\nlavfi.freezedetect.freeze_duration: 2\n"
               "lavfi.freezedetect.freeze_end: 3.5\nlavfi.freezedetect.freeze_start: 9\n")
        self.assertEqual(kSilenceDetector.ParseFreezeLog(Log), [(1.5, 3.5)])

    def test_FromTranscriptLeadingInnerTrailing(self):
        Data = Transcript((4.0, 5.0), (5.5, 6.0), (9.0, 10.0))
        self.assertEqual(kSilenceDetector.FromTranscript(Data, 3.0, 14.0), [(0.0, 4.0), (6.0, 9.0), (10.0, 14.0)])

    def test_FromTranscriptWithoutWords(self):
        self.assertEqual(kSilenceDetector.FromTranscript({"segments": []}, 3.0, 12.0), [(0.0, 12.0)])
        self.assertEqual(kSilenceDetector.FromTranscript({"segments": []}, 3.0, 2.0), [])

    def test_FrameDifference(self):
        self.assertEqual(kSilenceDetector.FrameDifference(bytes([0, 0, 0, 0]), bytes([8, 0, 8, 0]), 4, 1), 4.0)
        self.assertEqual(kSilenceDetector.FrameDifference(bytes([0, 0, 0, 0]), bytes([8, 0, 8, 0]), 4, 2), 8.0)


class kGeminiAnswerTests(kPureCase):

    def test_TranscriptText(self):
        Segments = [{"start": 0.0, "end": 1.25, "text": "hello"}, {"start": 1.25, "end": 3.0, "text": "world"}]
        self.assertEqual(kGeminiCuts.TranscriptText(Segments), "[0.0-1.2] hello\n[1.2-3.0] world")

    def test_ParseJson(self):
        self.assertEqual(kGeminiCuts.ParseJson('[{"start": 1}]'), ([{"start": 1}], True))
        self.assertEqual(kGeminiCuts.ParseJson("not json"), ([], False))

    def test_ToSpansSkipsMalformedItems(self):
        Items = [{"start": 1, "end": 2, "reason": "x" * 100}, {"start": 3}, {"start": "a", "end": 4},
                 {"start": 5, "end": 5}, None, {"start": "6", "end": "7.5"}]
        self.assertEqual(kGeminiCuts.ToSpans(Items, "retake"),
                         [(1.0, 2.0, "retake: " + "x" * 80), (6.0, 7.5, "retake: ")])


class kQuietTests(kPureCase):

    def test_ProgressFollowsQuiet(self):
        kConsole.Quiet = False
        Err = io.StringIO()
        with redirect_stderr(Err):
            kSilenceDetector.FromTranscript(Transcript((4.0, 5.0)), 3.0, 5.0)
        self.assertIn("[vedit/propose] transcript-VAD: 1 silence ranges", Err.getvalue())
        kConsole.Quiet = True
        Err = io.StringIO()
        with redirect_stderr(Err):
            kSilenceDetector.FromTranscript(Transcript((4.0, 5.0)), 3.0, 5.0)
        self.assertEqual(Err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
