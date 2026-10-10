"""Apply an EDL: cut+concat the keep ranges from a video.

Reads an edl.json (see vedit/README.md for format), pulls out every segment
with `keep: true`, and stitches them into a single output video.

Default mode re-encodes for precise cuts at arbitrary timestamps. Use
`--copy` for stream-copy (fast, but cuts snap to keyframe boundaries — only
safe if segment boundaries already align with keyframes).

Run with uv (the script header declares static-ffmpeg):

    uv run apply.py \
        --in "F:\\path\\input.mp4" --edl edl.json --out edited.mp4

    (without inline-metadata support: uvx --with static-ffmpeg python apply.py ...)

Flags:
    --in PATH           Input video. Required.
    --edl PATH          EDL JSON path. Required.
    --out PATH          Output video. Required.
    --copy              Stream-copy mode (no re-encode). Default: re-encode.
    --vcodec NAME       Video codec for re-encode. Default: libx264.
    --acodec NAME       Audio codec for re-encode. Default: aac.
    --crf N             x264 CRF (lower = higher quality). Default: 20.
    --preset NAME       x264 preset. Default: medium.
    --keep-padding-s F  Add this much padding to each keep range (re-grow
                        before each kept boundary). Default: 0.0.
    --audio-stream N    Which audio stream to keep (0-indexed). Default: 0.
                        Camtasia recordings often have 2 streams (mic + system).
    --mix-audio         Mix all audio streams into one (overrides --audio-stream).
    --audio-fade-ms N   Audio fade-in/out at each kept-segment boundary,
                        in milliseconds. Eliminates click/pop artifacts at
                        cut points. Default: 30. Set to 0 to disable.
    --fps N             Output frame rate (CFR). Default: 30. Required to
                        normalize VFR sources like tscc2 (Camtasia).
    --pix-fmt FMT       Output pixel format. Default: yuv420p. Camtasia
                        tscc2 records yuv444p which most consumer players
                        cannot decode — yuv420p is the safe default.
    --quiet             Less verbose output.
"""
# /// script
# requires-python = ">=3.10"
# dependencies = ["static-ffmpeg"]
# ///

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from kFfmpeg import kFfmpeg
from _vedit_data import kVeditData
from kShared import ToolInputException, ToolReportableException, kRun, kS, kToolException


class kEdlCutter:
    """Cuts and concatenates the keep ranges of one EDL out of one video with ffmpeg."""

    @staticmethod
    def LoadEdl(EdlPath: Path) -> tuple[str, float, list[dict[str, Any]]]:
        """Return (source, duration_s, segments); a gap or an empty segment is a spec mistake (exit 2)."""
        if kS.ErrorMode:
            return ("", 0.0, [])
        try:
            Data = kVeditData.LoadEdl(EdlPath)
            if kS.ErrorMode:
                return ("", 0.0, [])
            Segments = Data["segments"]
            for Index, Segment in enumerate(Segments):
                if Segment["end"] <= Segment["start"]:
                    raise ToolInputException(f"segment {Index}: end <= start ({Segment})")
                if Index > 0 and abs(Segments[Index - 1]["end"] - Segment["start"]) > 0.001:
                    raise ToolInputException(f"segment {Index}: not contiguous with previous")
            return (Data["source"], float(Data.get("duration_s", Segments[-1]["end"])), Segments)
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kEdlCutter.LoadEdl(file={EdlPath})")
            return ("", 0.0, [])

    @staticmethod
    def KeepRanges(Segments: list[dict[str, Any]], Padding: float, Total: float) -> list[tuple[float, float]]:
        """Collapse adjacent kept segments and apply padding (clipped to [0, Total])."""
        if kS.ErrorMode:
            return []
        try:
            Ranges: list[tuple[float, float]] = []
            for Segment in Segments:
                if not Segment["keep"]:
                    continue
                Start = max(0.0, Segment["start"] - Padding)
                End = min(Total, Segment["end"] + Padding)
                if Ranges and Start <= Ranges[-1][1] + 0.01:
                    Ranges[-1] = (Ranges[-1][0], max(Ranges[-1][1], End))
                else:
                    Ranges.append((Start, End))
            return Ranges
        except Exception as e:
            kS.GlobalErrorHandler(e, "kEdlCutter.KeepRanges")
            return []

    @staticmethod
    def BuildFilterComplex(Ranges: list[tuple[float, float]], AudioSource: str, AudioFadeSeconds: float = 0.030,
                           Fps: float = 30.0) -> str:
        """AudioSource is the filter-graph label for the audio source — e.g.
        "[0:a:0]" for stream 0, or "[amix]" if upstream amerged the streams.

        AudioFadeSeconds adds a per-segment fade-in/fade-out on the audio to suppress
        click/pop artifacts at cut boundaries. If a segment is shorter than
        2*AudioFadeSeconds, fades are skipped for that segment.

        Fps forces a constant frame rate on the concat output. Required because
        screen-recording codecs (e.g. tscc2 from Camtasia) carry VFR timing
        through trim+concat, leaving the encoded output at low avg_frame_rate
        which confuses Windows Media metadata and some players.
        """
        if kS.ErrorMode:
            return ""
        try:
            Parts = []
            Labels = []
            for Index, (Start, End) in enumerate(Ranges):
                Duration = End - Start
                Parts.append(f"[0:v]trim=start={Start:.3f}:end={End:.3f},setpts=PTS-STARTPTS[v{Index}]")
                AudioChain = f"{AudioSource}atrim=start={Start:.3f}:end={End:.3f},asetpts=PTS-STARTPTS"
                if AudioFadeSeconds > 0 and Duration > 2 * AudioFadeSeconds + 0.01:
                    AudioChain += (f",afade=t=in:st=0:d={AudioFadeSeconds:.3f}"
                                   f",afade=t=out:st={Duration - AudioFadeSeconds:.3f}:d={AudioFadeSeconds:.3f}")
                AudioChain += f"[a{Index}]"
                Parts.append(AudioChain)
                Labels.append(f"[v{Index}][a{Index}]")
            Parts.append(f"{''.join(Labels)}concat=n={len(Ranges)}:v=1:a=1[concatv][outa]")
            # Force CFR on the concatenated video to keep avg_frame_rate sane.
            Parts.append(f"[concatv]fps={Fps:g}[outv]")
            return ";\n".join(Parts)
        except Exception as e:
            kS.GlobalErrorHandler(e, "kEdlCutter.BuildFilterComplex")
            return ""

    @staticmethod
    def AddQuiet(Command: list[str], Quiet: bool) -> list[str]:
        """Insert `-loglevel error` after the program name when Quiet."""
        if kS.ErrorMode:
            return Command
        try:
            if Quiet:
                Command.insert(1, "-loglevel")
                Command.insert(2, "error")
            return Command
        except Exception as e:
            kS.GlobalErrorHandler(e, "kEdlCutter.AddQuiet")
            return Command

    @staticmethod
    def RunReencode(Input: Path, Out: Path, Ranges: list[tuple[float, float]], VideoCodec: str, AudioCodec: str,
                    Crf: int, Preset: str, AudioStream: int, MixAudio: bool, AudioStreamCount: int,
                    AudioFadeSeconds: float, Fps: float, PixelFormat: str, Quiet: bool) -> int:
        """Re-encode the keep ranges into Out; returns ffmpeg's exit code."""
        if kS.ErrorMode:
            return 1
        try:
            if not Ranges:
                raise ToolReportableException("no keep ranges in EDL — nothing to output.")
            if MixAudio and AudioStreamCount > 1:
                # amerge all audio streams, then feed into the trim/concat chain
                AmergeInputs = "".join(f"[0:a:{Index}]" for Index in range(AudioStreamCount))
                Prefix = f"{AmergeInputs}amerge=inputs={AudioStreamCount}[amix]"
                FilterComplex = Prefix + ";\n" + kEdlCutter.BuildFilterComplex(Ranges, "[amix]", AudioFadeSeconds,
                                                                               Fps)
            else:
                FilterComplex = kEdlCutter.BuildFilterComplex(Ranges, f"[0:a:{AudioStream}]", AudioFadeSeconds, Fps)
            if kS.ErrorMode:
                return 1
            # Write the filter to a temp file so we don't blow up command-line length on Windows
            with tempfile.NamedTemporaryFile("w", suffix=".filter", delete=False, encoding="utf-8") as File:
                File.write(FilterComplex)
                FilterPath = File.name
            Command = kEdlCutter.AddQuiet([
                "ffmpeg", "-y",
                "-i", str(Input),
                "-filter_complex_script", FilterPath,
                "-map", "[outv]", "-map", "[outa]",
                "-c:v", VideoCodec, "-crf", str(Crf), "-preset", Preset,
                "-pix_fmt", PixelFormat,
                "-c:a", AudioCodec, "-b:a", "192k",
                "-movflags", "+faststart",
                str(Out),
            ], Quiet)
            if not Quiet:
                print(f"[vedit/apply] re-encode: {len(Ranges)} keep ranges → {Out}", file=sys.stderr)
            try:
                return subprocess.run(Command).returncode
            finally:
                Path(FilterPath).unlink(missing_ok=True)
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kEdlCutter.RunReencode(file={Input})")
            return 1

    @staticmethod
    def RunCopy(Input: Path, Out: Path, Ranges: list[tuple[float, float]], Quiet: bool) -> int:
        """Stream-copy mode: extract each range to a temp file, then concat. Fast,
        but cut precision is keyframe-bound. Returns ffmpeg's exit code."""
        if kS.ErrorMode:
            return 1
        try:
            if not Ranges:
                raise ToolReportableException("no keep ranges in EDL — nothing to output.")
            Out.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="vedit_") as TempDir:
                TempPath = Path(TempDir)
                ListPath = TempPath / "concat.txt"
                Parts = []
                for Index, (Start, End) in enumerate(Ranges):
                    Part = TempPath / f"part_{Index:04d}.mp4"
                    Command = kEdlCutter.AddQuiet([
                        "ffmpeg", "-y",
                        "-ss", f"{Start:.3f}", "-to", f"{End:.3f}",
                        "-i", str(Input),
                        "-c", "copy",
                        "-avoid_negative_ts", "make_zero",
                        str(Part),
                    ], Quiet)
                    ReturnCode = subprocess.run(Command).returncode
                    if ReturnCode != 0:
                        sys.stderr.write(f"failed to extract range {Index} ({Start}–{End})\n")
                        return ReturnCode
                    Parts.append(Part)
                ListPath.write_text("\n".join(f"file '{Part.as_posix()}'" for Part in Parts), encoding="utf-8")
                Command = kEdlCutter.AddQuiet([
                    "ffmpeg", "-y",
                    "-f", "concat", "-safe", "0",
                    "-i", str(ListPath),
                    "-c", "copy",
                    "-movflags", "+faststart",
                    str(Out),
                ], Quiet)
                return subprocess.run(Command).returncode
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kEdlCutter.RunCopy(file={Input})")
            return 1


class kApplyApp:
    """Command line."""

    def Run(self) -> int:
        if kS.ErrorMode:
            return 1
        try:
            Parser = argparse.ArgumentParser(description="Apply an EDL: cut+concat keep ranges.")
            Parser.add_argument("--in", dest="inp", required=True)
            Parser.add_argument("--edl", required=True)
            Parser.add_argument("--out", required=True)
            Parser.add_argument("--copy", action="store_true",
                                help="Stream-copy (fast, keyframe-bound). Default: re-encode.")
            Parser.add_argument("--vcodec", default="libx264")
            Parser.add_argument("--acodec", default="aac")
            Parser.add_argument("--crf", type=int, default=20)
            Parser.add_argument("--preset", default="medium")
            Parser.add_argument("--keep-padding-s", type=float, default=0.0)
            Parser.add_argument("--audio-stream", type=int, default=0,
                                help="Which audio stream to keep (0-indexed). Default: 0.")
            Parser.add_argument("--mix-audio", action="store_true",
                                help="Mix all audio streams into one (overrides --audio-stream).")
            Parser.add_argument("--audio-fade-ms", type=int, default=30,
                                help="Audio fade-in/out at each cut boundary, ms. Default: 30. 0=disabled.")
            Parser.add_argument("--fps", type=float, default=30.0,
                                help="Output frame rate (CFR). Default: 30.")
            Parser.add_argument("--pix-fmt", default="yuv420p",
                                help="Output pixel format. Default: yuv420p (broad compatibility).")
            Parser.add_argument("--quiet", action="store_true")
            Args = Parser.parse_args()

            Input = Path(Args.inp)
            Out = Path(Args.out)
            EdlPath = Path(Args.edl)
            if not Input.exists():
                raise ToolInputException(f"input not found: {Input}")
            if not EdlPath.exists():
                raise ToolInputException(f"EDL not found: {EdlPath}")
            kFfmpeg.Ensure()
            _Source, Total, Segments = kEdlCutter.LoadEdl(EdlPath)
            Ranges = kEdlCutter.KeepRanges(Segments, Args.keep_padding_s, Total)
            if kS.ErrorMode:
                return 1
            Kept = sum(End - Start for Start, End in Ranges)
            if not Args.quiet:
                print(f"[vedit/apply] EDL: {len(Segments)} segments, {len(Ranges)} kept ranges, "
                      f"{Kept:.1f}s of {Total:.1f}s ({100*Kept/Total:.1f}%)", file=sys.stderr)
            Out.parent.mkdir(parents=True, exist_ok=True)
            StartTime = time.monotonic()
            if Args.copy:
                ReturnCode = kEdlCutter.RunCopy(Input, Out, Ranges, Args.quiet)
            else:
                AudioStreamCount = kFfmpeg.ProbeAudioStreamCount(Input)
                if not Args.quiet and AudioStreamCount != 1:
                    print(f"[vedit/apply] {AudioStreamCount} audio streams; using "
                          f"{'amix' if Args.mix_audio else f'stream {Args.audio_stream}'}",
                          file=sys.stderr)
                ReturnCode = kEdlCutter.RunReencode(Input, Out, Ranges,
                                                    Args.vcodec, Args.acodec, Args.crf, Args.preset,
                                                    Args.audio_stream, Args.mix_audio, AudioStreamCount,
                                                    Args.audio_fade_ms / 1000.0,
                                                    Args.fps, Args.pix_fmt,
                                                    Args.quiet)
            if kS.ErrorMode:
                return 1
            if ReturnCode == 0 and not Args.quiet:
                print(f"[vedit/apply] done in {time.monotonic() - StartTime:.1f}s → {Out}", file=sys.stderr)
            return ReturnCode
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, "kApplyApp.Run")
            return 1


if __name__ == "__main__":
    kRun.Main(kApplyApp)
