"""Silence, freeze and screen-activity detection for propose.py (sibling module, imported by the script).

The ffmpeg runs are thin wrappers; the parsing and the interval logic are separate static methods
(ParseSilenceLog, ParseFreezeLog, FromTranscript, FrameDifference, HasScreenChange) so they can be tested on
in-memory data without a subprocess.
"""
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from kConsole import kConsole
from kFfmpeg import kDependency
from kShared import ToolInputException, ToolReportableException, kS, kToolException


class kSilenceDetector:
    """Silence, freeze and screen-activity detection over one recording."""

    @staticmethod
    def RunAnalysis(Command: list[str], Name: str) -> str:
        """Run an ffmpeg analysis pass and return its stderr. ffmpeg refusing the input is an expected state: its
        output is shown and the tool stops (exit 1)."""
        if kS.ErrorMode:
            return ""
        try:
            Process = subprocess.run(Command, capture_output=True, text=True)
            if Process.returncode != 0:
                kConsole.Warn(Process.stderr[-2000:], End="")
                raise ToolReportableException(f"{Name} failed")
            return Process.stderr
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kSilenceDetector.RunAnalysis(step={Name})")
            return ""

    @staticmethod
    def ParseSilenceLog(Output: str) -> list[tuple[float, float]]:
        """The (start, end) pairs in ffmpeg silencedetect's log; a silence still open at the end is dropped."""
        if kS.ErrorMode:
            return []
        try:
            Ranges: list[tuple[float, float]] = []
            CurrentStart: float | None = None
            for Line in Output.splitlines():
                Match = re.search(r"silence_start:\s*([\d.]+)", Line)
                if Match:
                    CurrentStart = float(Match.group(1))
                    continue
                Match = re.search(r"silence_end:\s*([\d.]+)", Line)
                if Match and CurrentStart is not None:
                    Ranges.append((CurrentStart, float(Match.group(1))))
                    CurrentStart = None
            return Ranges
        except Exception as e:
            kS.GlobalErrorHandler(e, "kSilenceDetector.ParseSilenceLog")
            return []

    @staticmethod
    def ParseFreezeLog(Output: str) -> list[tuple[float, float]]:
        """The (start, end) pairs in ffmpeg freezedetect's log; a freeze still active at EOF has no end and is
        dropped."""
        if kS.ErrorMode:
            return []
        try:
            Starts = []
            Ends = []
            for Line in Output.splitlines():
                Match = re.search(r"freeze_start:\s*([\d.]+)", Line)
                if Match:
                    Starts.append(float(Match.group(1)))
                    continue
                Match = re.search(r"freeze_end:\s*([\d.]+)", Line)
                if Match:
                    Ends.append(float(Match.group(1)))
            return list(zip(Starts, Ends))
        except Exception as e:
            kS.GlobalErrorHandler(e, "kSilenceDetector.ParseFreezeLog")
            return []

    @staticmethod
    def Ffmpeg(Input: Path, Db: float, MinSeconds: float, Announce: bool = True) -> list[tuple[float, float]]:
        """Return list of (start, end) for silent intervals >= MinSeconds (ffmpeg silencedetect). Announce=False
        runs it without the progress line (an inner pass of another detector)."""
        if kS.ErrorMode:
            return []
        try:
            Command = [
                "ffmpeg", "-nostdin", "-hide_banner",
                "-i", str(Input),
                "-af", f"silencedetect=noise={Db}dB:d={MinSeconds:.3f}",
                "-f", "null", "-",
            ]
            if Announce:
                kConsole.Progress(f"[vedit/propose] ffmpeg silencedetect (noise={Db}dB d={MinSeconds}s)…")
            Output = kSilenceDetector.RunAnalysis(Command, "silencedetect")
            return kSilenceDetector.ParseSilenceLog(Output)
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kSilenceDetector.Ffmpeg(file={Input})")
            return []

    @staticmethod
    def FromTranscript(Transcript: dict[str, Any], MinSeconds: float, Total: float) -> list[tuple[float, float]]:
        """Derive silence ranges from the Whisper transcript: a 'silence' is any
        span of Total that contains no transcribed words for at least MinSeconds.

        This sidesteps dB-threshold calibration entirely — Whisper's VAD already
        decided what's speech. Quiet narration that ffmpeg silencedetect would
        miss-classify as silence is correctly treated as speech here.
        """
        if kS.ErrorMode:
            return []
        try:
            WordIntervals = []
            for Segment in Transcript["segments"]:
                for Word in Segment.get("words") or []:
                    WordIntervals.append((float(Word["start"]), float(Word["end"])))
            WordIntervals.sort()
            if not WordIntervals:
                return [(0.0, Total)] if Total >= MinSeconds else []
            Silences = []
            if WordIntervals[0][0] >= MinSeconds:
                Silences.append((0.0, WordIntervals[0][0]))
            for Index in range(len(WordIntervals) - 1):
                GapStart = WordIntervals[Index][1]
                GapEnd = WordIntervals[Index + 1][0]
                if GapEnd - GapStart >= MinSeconds:
                    Silences.append((GapStart, GapEnd))
            if Total - WordIntervals[-1][1] >= MinSeconds:
                Silences.append((WordIntervals[-1][1], Total))
            kConsole.Progress(f"[vedit/propose] transcript-VAD: {len(Silences)} silence ranges "
                              f"≥{MinSeconds}s from {len(WordIntervals)} words")
            return Silences
        except Exception as e:
            kS.GlobalErrorHandler(e, "kSilenceDetector.FromTranscript")
            return []

    @staticmethod
    def Silero(Input: Path, MinSeconds: float, Total: float) -> list[tuple[float, float]]:
        """Detect silences using silero-vad (NN-based VAD).

        Speech timestamps are inverted to silence ranges, including any leading
        silence before the first speech and trailing silence after the last.
        Requires the optional `silero-vad` package (pulls torch): `uv run --with silero-vad propose.py`.
        """
        if kS.ErrorMode:
            return []
        try:
            if not kDependency.Has("silero_vad"):
                raise ToolInputException(
                    "silero-vad not installed.\n"
                    "Run with: uv run --with silero-vad propose.py --vad-engine silero ...")
            from silero_vad import get_speech_timestamps, load_silero_vad, read_audio  # optional, checked above
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as File:
                WavPath = File.name
            try:
                kConsole.Progress("[vedit/propose] extracting 16kHz mono audio for silero-vad…")
                kSilenceDetector.RunAnalysis(
                    ["ffmpeg", "-y", "-nostdin", "-loglevel", "error",
                     "-i", str(Input), "-ac", "1", "-ar", "16000", "-vn", WavPath],
                    "audio extraction for silero")
                if kS.ErrorMode:
                    return []
                kConsole.Progress("[vedit/propose] silero-vad…")
                Model = load_silero_vad(onnx=True)
                Wav = read_audio(WavPath, sampling_rate=16000)
                Speech = get_speech_timestamps(
                    Wav, Model, sampling_rate=16000, return_seconds=True,
                    min_silence_duration_ms=int(MinSeconds * 1000),
                )
                Silences = []
                PreviousEnd = 0.0
                for Span in Speech:
                    SpeechStart, SpeechEnd = float(Span["start"]), float(Span["end"])
                    if SpeechStart - PreviousEnd >= MinSeconds:
                        Silences.append((PreviousEnd, SpeechStart))
                    PreviousEnd = SpeechEnd
                if Total - PreviousEnd >= MinSeconds:
                    Silences.append((PreviousEnd, Total))
                return Silences
            finally:
                Path(WavPath).unlink(missing_ok=True)
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kSilenceDetector.Silero(file={Input})")
            return []

    @staticmethod
    def Freezes(Input: Path, Noise: float, MinSeconds: float) -> list[tuple[float, float]]:
        """Return list of (start, end) for static-screen intervals >= MinSeconds.

        Uses ffmpeg freezedetect: marks a range frozen when consecutive frames
        differ by less than `Noise` (mean abs pixel diff) for at least `MinSeconds`.
        """
        if kS.ErrorMode:
            return []
        try:
            Command = [
                "ffmpeg", "-nostdin", "-hide_banner",
                "-i", str(Input),
                "-vf", f"freezedetect=n={Noise}:d={MinSeconds:.3f}",
                "-an", "-f", "null", "-",
            ]
            kConsole.Progress(f"[vedit/propose] ffmpeg freezedetect (n={Noise} d={MinSeconds}s)…")
            Output = kSilenceDetector.RunAnalysis(Command, "freezedetect")
            return kSilenceDetector.ParseFreezeLog(Output)
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kSilenceDetector.Freezes(file={Input})")
            return []

    @staticmethod
    def FrameDifference(Previous: bytes, Current: bytes, FrameBytes: int, Step: int) -> float:
        """Mean absolute pixel difference (0-255) of two grayscale frames, sampled every Step pixels."""
        if kS.ErrorMode:
            return 0.0
        try:
            return (
                sum(abs(Current[Pixel] - Previous[Pixel]) for Pixel in range(0, FrameBytes, Step))
                * Step / FrameBytes
            )
        except Exception as e:
            kS.GlobalErrorHandler(e, "kSilenceDetector.FrameDifference")
            return 0.0

    @staticmethod
    def ScreenActivity(Input: Path, IntervalSeconds: float = 2.0, ChangeThreshold: float = 8.0) -> list[float]:
        """Extract one grayscale frame per IntervalSeconds (128×72), compare consecutive
        frames via mean absolute pixel difference.  Returns timestamps of frames where
        the change exceeds ChangeThreshold (0–255 scale).

        A threshold of ~8 catches UI updates and text changes while ignoring codec
        noise on static screen content.  No extra dependencies — uses ffmpeg rawvideo
        pipe and plain Python byte arithmetic.
        """
        if kS.ErrorMode:
            return []
        try:
            Width, Height = 128, 72
            FrameBytes = Width * Height
            Step = max(1, FrameBytes // 1024)  # subsample to ~1024 pixels for speed
            Command = [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-i", str(Input),
                "-vf", f"fps={1.0 / IntervalSeconds:.4f},scale={Width}:{Height}:flags=fast_bilinear,format=gray",
                "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
            ]
            Process = subprocess.Popen(Command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            Stdout = Process.stdout
            if Stdout is None:  # cannot happen with stdout=PIPE; narrows the type
                raise RuntimeError("ffmpeg frame pipe has no stdout")
            Changes: list[float] = []
            Previous: bytes | None = None
            FrameIndex = 0
            try:
                while True:
                    Chunk = Stdout.read(FrameBytes)
                    if len(Chunk) < FrameBytes:
                        break
                    if Previous is not None:
                        if kSilenceDetector.FrameDifference(Previous, Chunk, FrameBytes, Step) > ChangeThreshold:
                            Changes.append(FrameIndex * IntervalSeconds)
                    Previous = Chunk
                    FrameIndex += 1
            finally:
                Stdout.close()
                Process.wait()
            kConsole.Progress(f"[vedit/propose] screen-check: {FrameIndex} frames × {IntervalSeconds}s, "
                              f"{len(Changes)} changes detected")
            return Changes
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kSilenceDetector.ScreenActivity(file={Input})")
            return []

    @staticmethod
    def HasScreenChange(Start: float, End: float, Changes: list[float], IntervalSeconds: float) -> bool:
        """True iff any screen-change timestamp falls within [Start, End].

        A change recorded at timestamp t means the screen differed between frames at
        t−IntervalSeconds and t, so we extend the window by ±IntervalSeconds on each side.
        """
        if kS.ErrorMode:
            return False
        try:
            for Change in Changes:
                if Start - IntervalSeconds < Change < End + IntervalSeconds:
                    return True
            return False
        except Exception as e:
            kS.GlobalErrorHandler(e, "kSilenceDetector.HasScreenChange")
            return False
