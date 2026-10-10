"""Cut arithmetic and the EDL for propose.py (sibling module, imported by the script).

Everything here except DetectAudioEvents is pure interval arithmetic on in-memory lists, testable without ffmpeg:
Intersect, StaticCuts, ClassifySilences, FlattenWords, SnapToSilenceGap, WordSnapCuts, PadCuts, IsFullySilent,
AudioEventsInGaps, MergeCuts, BuildEdl.
"""
from pathlib import Path
from typing import Any

from _silence import kSilenceDetector
from kConsole import kConsole
from kShared import kS, kToolException


class kCutEditor:
    """Interval arithmetic on cuts, word snapping, audio events, and the EDL itself."""

    @staticmethod
    def Intersect(First: list[tuple[float, float]],
                  Second: list[tuple[float, float]]) -> list[tuple[float, float]]:
        """Interval-list intersection. Both inputs sorted, non-overlapping."""
        if kS.ErrorMode:
            return []
        try:
            Out = []
            I = J = 0
            while I < len(First) and J < len(Second):
                Start = max(First[I][0], Second[J][0])
                End = min(First[I][1], Second[J][1])
                if Start < End:
                    Out.append((Start, End))
                if First[I][1] < Second[J][1]:
                    I += 1
                else:
                    J += 1
            return Out
        except Exception as e:
            kS.GlobalErrorHandler(e, "kCutEditor.Intersect")
            return []

    @staticmethod
    def StaticCuts(ShortSilences: list[tuple[float, float]], Freezes: list[tuple[float, float]],
                   StaticMinSeconds: float, SilenceMaxSeconds: float) -> list[tuple[float, float, str]]:
        """"static+silent" cuts: where a (short) silence and a frozen screen overlap for at least StaticMinSeconds,
        and - when SilenceMaxSeconds > 0 - for at most SilenceMaxSeconds."""
        if kS.ErrorMode:
            return []
        try:
            Cuts = []
            for Start, End in kCutEditor.Intersect(sorted(ShortSilences), sorted(Freezes)):
                if End - Start >= StaticMinSeconds and (SilenceMaxSeconds <= 0 or (End - Start) <= SilenceMaxSeconds):
                    Cuts.append((Start, End, "static+silent"))
            return Cuts
        except Exception as e:
            kS.GlobalErrorHandler(e, "kCutEditor.StaticCuts")
            return []

    @staticmethod
    def ClassifySilences(Silences: list[tuple[float, float]], ScreenChanges: list[float], SilenceMinSeconds: float,
                         SilenceMaxSeconds: float, ScreenCheck: bool, ScreenIntervalSeconds: float,
                         SpeedUpThresholdSeconds: float, SpeedUpFactor: float, SpeedUpMaxSeconds: float
                         ) -> tuple[list[tuple[float, float, str]], list[tuple[float, float, str, float]], dict[str, int]]:
        """Decide each long silence: skipped (longer than the cap), preserved (screen active), kept at 1x (demo
        payoff), sped up (static and long) or cut. Returns (cuts, speed_segments, counts) where counts holds
        "SkippedLong", "ScreenActiveSkipped" and "DemoPayoffKept"."""
        if kS.ErrorMode:
            return ([], [], {})
        try:
            Cuts: list[tuple[float, float, str]] = []
            SpeedSegments: list[tuple[float, float, str, float]] = []
            Counts = {"SkippedLong": 0, "ScreenActiveSkipped": 0, "DemoPayoffKept": 0}
            for Start, End in Silences:
                if SilenceMaxSeconds > 0 and (End - Start) > SilenceMaxSeconds:
                    Counts["SkippedLong"] += 1
                    continue
                if ScreenCheck and kSilenceDetector.HasScreenChange(Start, End, ScreenChanges, ScreenIntervalSeconds):
                    Counts["ScreenActiveSkipped"] += 1
                    continue
                if ScreenCheck and (End - Start) >= SpeedUpThresholdSeconds:
                    if SpeedUpMaxSeconds > 0 and (End - Start) > SpeedUpMaxSeconds:
                        Counts["DemoPayoffKept"] += 1  # long static silence — the demo is the point; keep at 1x
                    else:
                        SpeedSegments.append((Start, End, f"screen_inactive>{SpeedUpThresholdSeconds:.0f}s",
                                              SpeedUpFactor))
                else:
                    Cuts.append((Start, End, f"silence>{SilenceMinSeconds:.0f}s"))
            return (Cuts, SpeedSegments, Counts)
        except Exception as e:
            kS.GlobalErrorHandler(e, "kCutEditor.ClassifySilences")
            return ([], [], {})

    @staticmethod
    def StartKey(Item: dict[str, float]) -> float:
        """Sort key: a word dict's "start"."""
        if kS.ErrorMode:
            return 0.0
        try:
            return Item["start"]
        except Exception as e:
            kS.GlobalErrorHandler(e, "kCutEditor.StartKey")
            return 0.0

    @staticmethod
    def FirstKey(Item: tuple[float, float, str]) -> float:
        """Sort key: a cut tuple's start."""
        if kS.ErrorMode:
            return 0.0
        try:
            return Item[0]
        except Exception as e:
            kS.GlobalErrorHandler(e, "kCutEditor.FirstKey")
            return 0.0

    @staticmethod
    def FlattenWords(Transcript: dict[str, Any]) -> list[dict[str, float]]:
        """All words from all transcript segments, in time order."""
        if kS.ErrorMode:
            return []
        try:
            Words = []
            for Segment in Transcript["segments"]:
                for Word in Segment.get("words") or []:
                    Words.append({"start": float(Word["start"]), "end": float(Word["end"])})
            Words.sort(key=kCutEditor.StartKey)
            return Words
        except Exception as e:
            kS.GlobalErrorHandler(e, "kCutEditor.FlattenWords")
            return []

    @staticmethod
    def SnapToSilenceGap(Seconds: float, Words: list[dict[str, float]], MaxDrift: float) -> float:
        """Find the midpoint of the silence gap closest to Seconds (within MaxDrift).
        Returns Seconds unchanged if no gap is close enough.

        A 'silence gap' is the space between word i's end and word i+1's start,
        where end < start. Cut boundaries snapped to gap midpoints don't clip
        audible word edges.
        """
        if kS.ErrorMode:
            return Seconds
        try:
            if not Words:
                return Seconds
            BestMiddle: float | None = None
            BestDistance = MaxDrift + 1
            for Index in range(len(Words) - 1):
                GapStart = Words[Index]["end"]
                GapEnd = Words[Index + 1]["start"]
                if GapEnd <= GapStart + 0.005:
                    continue
                GapMiddle = (GapStart + GapEnd) / 2
                Distance = abs(GapMiddle - Seconds)
                if Distance < BestDistance:
                    BestDistance = Distance
                    BestMiddle = GapMiddle
            if BestMiddle is not None and BestDistance <= MaxDrift:
                return BestMiddle
            return Seconds
        except Exception as e:
            kS.GlobalErrorHandler(e, "kCutEditor.SnapToSilenceGap")
            return Seconds

    @staticmethod
    def WordSnapCuts(Cuts: list[tuple[float, float, str]], Words: list[dict[str, float]],
                     MaxDrift: float) -> list[tuple[float, float, str]]:
        """Every cut with both boundaries snapped to the nearest silence gap; cuts that collapse are dropped."""
        if kS.ErrorMode:
            return []
        try:
            Snapped = []
            for Start, End, Reason in Cuts:
                NewStart = kCutEditor.SnapToSilenceGap(Start, Words, MaxDrift)
                NewEnd = kCutEditor.SnapToSilenceGap(End, Words, MaxDrift)
                if NewEnd > NewStart:
                    Snapped.append((NewStart, NewEnd, Reason))
            return Snapped
        except Exception as e:
            kS.GlobalErrorHandler(e, "kCutEditor.WordSnapCuts")
            return []

    @staticmethod
    def PadCuts(Cuts: list[tuple[float, float, str]], Pad: float) -> list[tuple[float, float, str]]:
        """Every cut shrunk by Pad on both sides (no transcript to snap to); cuts not longer than 2*Pad are dropped."""
        if kS.ErrorMode:
            return []
        try:
            return [(Start + Pad, End - Pad, Reason) for Start, End, Reason in Cuts if End - Start > 2 * Pad]
        except Exception as e:
            kS.GlobalErrorHandler(e, "kCutEditor.PadCuts")
            return []

    @staticmethod
    def IsFullySilent(Silences: list[tuple[float, float]], Start: float, End: float) -> bool:
        """True iff every point in [Start, End] falls in some silence interval (Silences sorted)."""
        if kS.ErrorMode:
            return True
        try:
            Cursor = Start
            for SilenceStart, SilenceEnd in Silences:
                if SilenceEnd <= Start:
                    continue
                if SilenceStart > Cursor + 0.015:
                    return False
                Cursor = max(Cursor, SilenceEnd)
                if Cursor >= End - 0.015:
                    return True
            return Cursor >= End - 0.015
        except Exception as e:
            kS.GlobalErrorHandler(e, "kCutEditor.IsFullySilent")
            return True

    @staticmethod
    def WordGaps(Transcript: dict[str, Any], MinDuration: float, MaxDuration: float) -> list[tuple[float, float]]:
        """The gaps between consecutive transcript words whose length lies in [MinDuration, MaxDuration]."""
        if kS.ErrorMode:
            return []
        try:
            AllWords = []
            for Segment in Transcript["segments"]:
                for Word in Segment.get("words") or []:
                    AllWords.append((float(Word["start"]), float(Word["end"])))
            AllWords.sort()
            Gaps = []
            for Index in range(len(AllWords) - 1):
                GapStart = AllWords[Index][1]
                GapEnd = AllWords[Index + 1][0]
                if MinDuration <= GapEnd - GapStart <= MaxDuration:
                    Gaps.append((GapStart, GapEnd))
            return Gaps
        except Exception as e:
            kS.GlobalErrorHandler(e, "kCutEditor.WordGaps")
            return []

    @staticmethod
    def AudioEventsInGaps(Gaps: list[tuple[float, float]],
                          Silences: list[tuple[float, float]]) -> list[tuple[float, float, str]]:
        """An "audio_event: <hint>" cut for every gap not fully covered by Silences (sorted): something audible
        that is not a word. Hint by length: < 0.3 s mouth_sound, < 0.8 s filler_sound, else throat_clear."""
        if kS.ErrorMode:
            return []
        try:
            Events = []
            for GapStart, GapEnd in Gaps:
                if not kCutEditor.IsFullySilent(Silences, GapStart, GapEnd):
                    Duration = GapEnd - GapStart
                    if Duration < 0.3:
                        Hint = "mouth_sound"
                    elif Duration < 0.8:
                        Hint = "filler_sound"
                    else:
                        Hint = "throat_clear"
                    Events.append((GapStart, GapEnd, f"audio_event: {Hint}"))
            return Events
        except Exception as e:
            kS.GlobalErrorHandler(e, "kCutEditor.AudioEventsInGaps")
            return []

    @staticmethod
    def DetectAudioEvents(Input: Path, Transcript: dict[str, Any], MinDuration: float = 0.1,
                          MaxDuration: float = 2.0, SilenceDb: float = -40.0) -> list[tuple[float, float, str]]:
        """Find non-speech vocalizations: audible sounds in inter-word gaps.

        Strategy: any gap between consecutive transcript words that (a) falls in
        [MinDuration, MaxDuration] and (b) is not fully covered by silence detections
        must contain audible non-speech content — a throat clear, uh/um, or mouth
        sound. Runs one ffmpeg silencedetect call (min 0.05 s, same threshold as
        the main pass) to get the silence map; gap audio presence is then pure
        interval arithmetic, no per-gap ffmpeg calls.
        """
        if kS.ErrorMode:
            return []
        try:
            Gaps = kCutEditor.WordGaps(Transcript, MinDuration, MaxDuration)
            if not Gaps:
                return []
            # One ffmpeg silencedetect pass with a very short min-duration to build
            # a complete silence map. We deliberately use 0.05 so even brief
            # silent patches are captured (we're looking for ANY audible content).
            Silences = sorted(kSilenceDetector.Ffmpeg(Input, SilenceDb, 0.05, Announce=False))
            Events = kCutEditor.AudioEventsInGaps(Gaps, Silences)
            kConsole.Progress(
                f"[vedit/propose] audio-events: {len(Events)} vocalizations "
                f"({sum(1 for Event in Events if 'throat' in Event[2])} throat_clear, "
                f"{sum(1 for Event in Events if 'filler' in Event[2])} filler_sound, "
                f"{sum(1 for Event in Events if 'mouth' in Event[2])} mouth_sound)"
            )
            return Events
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kCutEditor.DetectAudioEvents(file={Input})")
            return []

    @staticmethod
    def MergeCuts(Cuts: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
        """Sort the cuts and merge overlapping or touching ones, joining their reasons."""
        if kS.ErrorMode:
            return []
        try:
            if not Cuts:
                return []
            Cuts = sorted(Cuts, key=kCutEditor.FirstKey)
            Merged = [Cuts[0]]
            for Start, End, Reason in Cuts[1:]:
                LastStart, LastEnd, LastReason = Merged[-1]
                if Start <= LastEnd + 0.001:
                    NewReason = LastReason if Reason == LastReason or Reason in LastReason else f"{LastReason}+{Reason}"
                    Merged[-1] = (LastStart, max(LastEnd, End), NewReason)
                else:
                    Merged.append((Start, End, Reason))
            return Merged
        except Exception as e:
            kS.GlobalErrorHandler(e, "kCutEditor.MergeCuts")
            return []

    @staticmethod
    def IntervalCut(Cuts: list[tuple[float, float, str]], Start: float, End: float) -> tuple[bool, str]:
        """(True, reason) when the middle of [Start, End] lies in a cut, else (False, "")."""
        if kS.ErrorMode:
            return (False, "")
        try:
            Middle = (Start + End) / 2
            for CutStart, CutEnd, CutReason in Cuts:
                if CutStart <= Middle < CutEnd:
                    return (True, CutReason)
            return (False, "")
        except Exception as e:
            kS.GlobalErrorHandler(e, "kCutEditor.IntervalCut")
            return (False, "")

    @staticmethod
    def IntervalSpeed(SpeedMap: list[tuple[float, float, float]], Start: float, End: float) -> float:
        """The speed of the speed-up span holding the middle of [Start, End], else 1.0."""
        if kS.ErrorMode:
            return 1.0
        try:
            Middle = (Start + End) / 2
            for SpeedStart, SpeedEnd, Speed in SpeedMap:
                if SpeedStart <= Middle < SpeedEnd:
                    return Speed
            return 1.0
        except Exception as e:
            kS.GlobalErrorHandler(e, "kCutEditor.IntervalSpeed")
            return 1.0

    @staticmethod
    def BuildEdl(Cuts: list[tuple[float, float, str]], Total: float, Source: str,
                 SpeedSegments: list[tuple[float, float, str, float]] | None = None) -> dict[str, Any]:
        """Build EDL from cuts (keep=False) and optional SpeedSegments (keep=True, speed>1).

        All boundary points from both cuts and SpeedSegments are collected first, then
        the timeline is split into atomic intervals so that a speed-up zone can never
        bleed into adjacent speech.  Consecutive same-type intervals are merged back.
        """
        if kS.ErrorMode:
            return {}
        try:
            Cuts = [(max(0.0, Start), min(Total, End), Reason) for Start, End, Reason in Cuts if End > Start]
            SpeedMap: list[tuple[float, float, float]] = []
            if SpeedSegments:
                for SpeedStart, SpeedEnd, _Reason, Speed in SpeedSegments:
                    if SpeedEnd > SpeedStart:
                        SpeedMap.append((max(0.0, SpeedStart), min(Total, SpeedEnd), Speed))
            # Gather every boundary so each interval is unambiguously one type
            Points = {0.0, Total}
            for Start, End, _Reason in Cuts:
                Points.add(Start)
                Points.add(End)
            for SpeedStart, SpeedEnd, _Speed in SpeedMap:
                Points.add(SpeedStart)
                Points.add(SpeedEnd)
            SortedPoints = sorted(Points)
            Raw: list[dict[str, Any]] = []
            for Index in range(len(SortedPoints) - 1):
                Start, End = SortedPoints[Index], SortedPoints[Index + 1]
                if End - Start < 0.001:
                    continue
                IsCut, Reason = kCutEditor.IntervalCut(Cuts, Start, End)
                if IsCut:
                    Raw.append({"start": Start, "end": End, "keep": False, "reason": Reason})
                else:
                    Speed = kCutEditor.IntervalSpeed(SpeedMap, Start, End)
                    Segment: dict[str, Any] = {"start": Start, "end": End, "keep": True, "reason": ""}
                    if Speed != 1.0:
                        Segment["speed"] = Speed
                    Raw.append(Segment)
            # Merge consecutive intervals with identical keep/speed/reason
            Segments: list[dict[str, Any]] = []
            for Segment in Raw:
                if (
                    Segments
                    and Segments[-1]["keep"] == Segment["keep"]
                    and Segments[-1].get("speed", 1.0) == Segment.get("speed", 1.0)
                    and Segments[-1]["reason"] == Segment["reason"]
                ):
                    Segments[-1]["end"] = Segment["end"]
                else:
                    Segments.append(dict(Segment))
            for Segment in Segments:
                Segment["start"] = round(Segment["start"], 3)
                Segment["end"] = round(Segment["end"], 3)
            return {
                "source": Source,
                "duration_s": round(Total, 3),
                "version": 1,
                "segments": Segments,
            }
        except Exception as e:
            kS.GlobalErrorHandler(e, "kCutEditor.BuildEdl")
            return {}
