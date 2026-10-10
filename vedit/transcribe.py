"""Transcribe a video/audio file with faster-whisper (local, no API).

Outputs a JSON transcript with segment-level and word-level timestamps,
plus per-segment confidence/no-speech-probability fields used by propose.py
to detect retakes and low-quality stretches.

Run with uv (no venv required; the script header declares faster-whisper and static-ffmpeg):

    uv run transcribe.py \
        --in "F:\\path\\input.mp4" --out transcript.json

    (without inline-metadata support: uvx --with faster-whisper --with static-ffmpeg python transcribe.py ...)

Flags:
    --in PATH        Input media file (video or audio). Required.
    --out PATH       Output JSON path. Required.
    --model NAME     Whisper model: small.en | medium.en | large-v3.
                     Default: small.en (~0.5x realtime on CPU, good for screencasts).
                     Use medium.en for ~2x slower but more accurate transcription.
    --language CODE  Language hint (default: en). Use "auto" to detect.
    --device DEV     cpu | cuda. Default: cpu.
    --compute-type T Quantization. Default: int8 (cpu) / float16 (cuda).
    --quiet          Suppress per-segment progress lines. Errors still on stderr.

First run downloads the model (~470MB for medium.en) to the HF cache.
"""
# /// script
# requires-python = ">=3.10"
# dependencies = ["faster-whisper", "static-ffmpeg"]
# ///
import argparse
import sys
import time
from pathlib import Path
from typing import Any

from kFfmpeg import kDependency, kFfmpeg
from kJsonFile import kJsonFile
from kShared import ToolInputException, kRun, kS, kToolException


class kTranscriber:
    """One faster-whisper run over one media file."""

    def __init__(self, Input: Path, Model: str, Language: str, Device: str, ComputeType: str | None,
                 Quiet: bool) -> None:
        try:
            self.Input = Input
            self.Model = Model
            self.Language = Language
            self.Device = Device
            self.ComputeType = ComputeType or ("int8" if Device == "cpu" else "float16")
            self.Quiet = Quiet
        except Exception as e:
            kS.GlobalErrorHandler(e, "kTranscriber.__init__")

    @staticmethod
    def WordToDict(Word: Any) -> dict[str, Any]:
        """One recognised word as its JSON form."""
        if kS.ErrorMode:
            return {}
        try:
            return {
                "start": round(Word.start, 3),
                "end": round(Word.end, 3),
                "word": Word.word,
                "probability": round(Word.probability, 4),
            }
        except Exception as e:
            kS.GlobalErrorHandler(e, "kTranscriber.WordToDict")
            return {}

    @staticmethod
    def SegmentToDict(Segment: Any) -> dict[str, Any]:
        """One recognised segment (with its words) as its JSON form."""
        if kS.ErrorMode:
            return {}
        try:
            Words = []
            if Segment.words:
                for Word in Segment.words:
                    Words.append(kTranscriber.WordToDict(Word))
            return {
                "start": round(Segment.start, 3),
                "end": round(Segment.end, 3),
                "text": Segment.text.strip(),
                "avg_logprob": round(Segment.avg_logprob, 4),
                "no_speech_prob": round(Segment.no_speech_prob, 4),
                "words": Words,
            }
        except Exception as e:
            kS.GlobalErrorHandler(e, "kTranscriber.SegmentToDict")
            return {}

    def Transcribe(self) -> dict[str, Any] | None:
        """The transcript payload (dict) written by the command line; None after a reported error."""
        if kS.ErrorMode:
            return None
        try:
            from faster_whisper import WhisperModel  # optional package, checked by the App
            if not self.Quiet:
                print(f"[vedit/transcribe] loading {self.Model} on {self.Device} ({self.ComputeType})",
                      file=sys.stderr)
            self.StartTime = time.monotonic()
            Model = WhisperModel(self.Model, device=self.Device, compute_type=self.ComputeType)
            Language = None if self.Language == "auto" else self.Language
            SegmentsIter, Info = Model.transcribe(
                str(self.Input),
                language=Language,
                beam_size=5,
                word_timestamps=True,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
            )
            if not self.Quiet:
                print(f"[vedit/transcribe] detected language={Info.language} "
                      f"prob={Info.language_probability:.2f} duration={Info.duration:.1f}s",
                      file=sys.stderr)
            OutSegments = []
            LastPrint = 0.0
            for Segment in SegmentsIter:
                OutSegments.append(kTranscriber.SegmentToDict(Segment))
                if not self.Quiet and Segment.end - LastPrint >= 30.0:
                    Elapsed = time.monotonic() - self.StartTime
                    Speed = Segment.end / Elapsed if Elapsed > 0 else 0
                    print(f"  {Segment.end:7.1f}s / {Info.duration:.1f}s "
                          f"({100*Segment.end/Info.duration:5.1f}%, {Speed:.2f}x realtime)",
                          file=sys.stderr)
                    LastPrint = Segment.end
            self.Duration = Info.duration
            return {
                "source": str(self.Input),
                "language": Info.language,
                "duration_s": round(Info.duration, 3),
                "model": self.Model,
                "device": self.Device,
                "compute_type": self.ComputeType,
                "segments": OutSegments,
            }
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kTranscriber.Transcribe(file={self.Input})")
            return None


class kTranscribeApp:
    """Command line."""

    def Run(self) -> int:
        if kS.ErrorMode:
            return 1
        try:
            Parser = argparse.ArgumentParser(description="Transcribe with faster-whisper.")
            Parser.add_argument("--in", dest="inp", required=True, help="Input media file")
            Parser.add_argument("--out", dest="out", required=True, help="Output JSON path")
            Parser.add_argument("--model", default="small.en")
            Parser.add_argument("--language", default="en")
            Parser.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
            Parser.add_argument("--compute-type", default=None,
                                help="Override quantization (int8/float16/float32)")
            Parser.add_argument("--quiet", action="store_true")
            Args = Parser.parse_args()

            Input = Path(Args.inp)
            Out = Path(Args.out)
            if not Input.exists():
                raise ToolInputException(f"input not found: {Input}")
            kFfmpeg.Ensure()  # faster-whisper decodes through ffmpeg
            if not kDependency.Has("faster_whisper"):
                raise ToolInputException(
                    "faster-whisper not installed.\n"
                    "Run with: uv run transcribe.py ... (the script declares faster-whisper and static-ffmpeg)")
            Transcriber = kTranscriber(Input, Args.model, Args.language, Args.device, Args.compute_type,
                                       Args.quiet)
            Payload = Transcriber.Transcribe()
            if kS.ErrorMode or Payload is None:
                return 1
            if not kJsonFile.Save(Out, Payload):
                return 1
            Elapsed = time.monotonic() - Transcriber.StartTime
            if not Args.quiet:
                Speed = Transcriber.Duration / Elapsed if Elapsed > 0 else 0
                print(f"[vedit/transcribe] done in {Elapsed:.1f}s ({Speed:.2f}x realtime). "
                      f"{len(Payload['segments'])} segments → {Out}",
                      file=sys.stderr)
            return 0
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, "kTranscribeApp.Run")
            return 1


if __name__ == "__main__":
    kRun.Main(kTranscribeApp)
