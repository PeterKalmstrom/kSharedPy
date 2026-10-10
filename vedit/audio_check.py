"""Audio quality gate. Refuses to process sub-standard recordings.

The pipeline assumes consistent audio (same mic, same room, same gain). Even
a small mic change ("oh, I used the laptop mic this time") produces a video
that needs to be re-recorded — silence detection thresholds shift, Whisper
accuracy drops, and the final video sounds different from the rest of a
series. This script catches that BEFORE transcription so you don't burn
12 minutes of CPU and then notice.

# Two modes

**Calibrate** (build a baseline from a known-good recording):

    uv run audio_check.py \\
        --in good_recording.trec --calibrate --profile-out profile.json

**Check** (compare a new recording against the baseline; non-zero exit on fail):

    uv run audio_check.py \\
        --in new_recording.trec --profile profile.json

The script header declares static-ffmpeg, so `uv run` installs it (without inline-metadata
support: `uvx --with static-ffmpeg python audio_check.py ...`).

# Metrics

- **mean_volume_db** (`volumedetect`) — average level. Mic gain proxy.
- **max_volume_db** — peak level. Clipping warning if near 0.
- **integrated_lufs** (`ebur128`) — perceived loudness. Most stable cross-mic.
- **true_peak_db** — true (inter-sample) peak.
- **loudness_range_lu** — dynamic range. Mic change often shifts this.

# Tolerances

Default tolerances are tight (±2 dB / ±2 LUFS) — designed to catch mic
changes, not minor speaking-loudness variation. Adjust per-metric via the
profile JSON if a metric is genuinely noisy on your setup.

Flags:
    --in PATH           Input media file. Required.
    --audio-stream N    Which audio stream to measure (0-indexed). Default: 0.
                        Camtasia recordings often have 2 streams (mic + system);
                        stream 0 is usually the mic.
    --calibrate         Build a profile from this recording (write to --profile-out).
    --profile-out PATH  Where to write the profile (calibrate mode).
    --profile PATH      Profile JSON to check against (default mode).
    --tolerance-db F    Override default tolerance for all metrics. Default: 2.0.
    --quiet             Suppress per-metric pass/fail rows on success.
"""
# /// script
# requires-python = ">=3.10"
# dependencies = ["static-ffmpeg"]
# ///

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from kFfmpeg import kFfmpeg
from kJsonFile import kJsonFile
from kShared import ToolInputException, ToolReportableException, kRun, kS, kToolException

DEFAULT_TOLERANCE = {
    "mean_volume_db":   2.0,
    "max_volume_db":    3.0,
    "integrated_lufs":  2.0,
    "true_peak_db":     3.0,
    "loudness_range_lu": 2.5,
}

# Metrics where -inf means "no audio detected" → always fail
NEVER_NEG_INF = {"mean_volume_db", "max_volume_db", "integrated_lufs", "true_peak_db"}


class kAudioMeter:
    """ffmpeg loudness probes of one audio stream."""

    @staticmethod
    def RunFilter(Input: Path, AudioStream: int, Filter: str, Name: str) -> str:
        """Run ffmpeg with an analysis filter and return its stderr. ffmpeg refusing the stream (no such audio
        stream, unreadable file) is an expected state: its output is shown and the tool stops (exit 1)."""
        if kS.ErrorMode:
            return ""
        try:
            Process = subprocess.run(
                ["ffmpeg", "-nostdin", "-hide_banner",
                 "-i", str(Input),
                 "-map", f"0:a:{AudioStream}",
                 "-af", Filter,
                 "-f", "null", "-"],
                capture_output=True, text=True,
            )
            if Process.returncode != 0:
                sys.stderr.write(Process.stderr[-2000:])
                raise ToolReportableException(f"{Name} failed")
            return Process.stderr
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kAudioMeter.RunFilter(file={Input}, filter={Name})")
            return ""

    @staticmethod
    def ProbeVolume(Input: Path, AudioStream: int) -> dict[str, float]:
        """Run ffmpeg volumedetect, parse mean/max volume from stderr."""
        if kS.ErrorMode:
            return {}
        try:
            Output = kAudioMeter.RunFilter(Input, AudioStream, "volumedetect", "volumedetect")
            if kS.ErrorMode:
                return {}
            Mean = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", Output)
            Peak = re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", Output)
            if not Mean or not Peak:
                raise RuntimeError("could not parse volumedetect output")
            return {
                "mean_volume_db": float(Mean.group(1)),
                "max_volume_db":  float(Peak.group(1)),
            }
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kAudioMeter.ProbeVolume(file={Input})")
            return {}

    @staticmethod
    def ToFloat(Text: str) -> float:
        """A parsed level as a float; "-inf" is minus infinity."""
        if kS.ErrorMode:
            return 0.0
        try:
            return float("-inf") if Text == "-inf" else float(Text)
        except Exception as e:
            kS.GlobalErrorHandler(e, "kAudioMeter.ToFloat")
            return 0.0

    @staticmethod
    def ProbeEbur128(Input: Path, AudioStream: int) -> dict[str, float]:
        """Run ffmpeg ebur128, parse integrated loudness / LRA / true peak."""
        if kS.ErrorMode:
            return {}
        try:
            Text = kAudioMeter.RunFilter(Input, AudioStream, "ebur128=peak=true", "ebur128")
            if kS.ErrorMode:
                return {}
            # Parse the Summary block at the end of stderr
            SummaryIndex = Text.rfind("Summary:")
            if SummaryIndex < 0:
                raise RuntimeError("could not find ebur128 Summary in output")
            Summary = Text[SummaryIndex:]
            Integrated = re.search(r"I:\s*(-?[\d.]+|-inf)\s*LUFS", Summary)
            Range = re.search(r"LRA:\s*([\d.]+)\s*LU\b", Summary)
            Peak = re.search(r"Peak:\s*(-?[\d.]+|-inf)\s*dBFS", Summary)
            if not Integrated or not Range or not Peak:
                raise RuntimeError(f"could not parse ebur128 Summary fields:\n{Summary[:800]}")
            return {
                "integrated_lufs":   kAudioMeter.ToFloat(Integrated.group(1)),
                "loudness_range_lu": float(Range.group(1)),
                "true_peak_db":      kAudioMeter.ToFloat(Peak.group(1)),
            }
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kAudioMeter.ProbeEbur128(file={Input})")
            return {}

    @staticmethod
    def Measure(Input: Path, AudioStream: int) -> dict[str, float]:
        """All five metrics of one audio stream."""
        if kS.ErrorMode:
            return {}
        try:
            Metrics: dict[str, float] = {}
            Metrics.update(kAudioMeter.ProbeVolume(Input, AudioStream))
            Metrics.update(kAudioMeter.ProbeEbur128(Input, AudioStream))
            return Metrics
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kAudioMeter.Measure(file={Input})")
            return {}

    @staticmethod
    def CheckAgainst(Metrics: dict[str, float], Profile: dict[str, Any]) -> tuple[bool, list[str]]:
        """Return (passed, report_rows) for the metrics against a calibrated profile."""
        if kS.ErrorMode:
            return (False, [])
        try:
            Baseline = Profile.get("baseline", {})
            Tolerance = {**DEFAULT_TOLERANCE, **Profile.get("tolerance", {})}
            Failures = []
            Rows = []
            for Key, Want in Baseline.items():
                Got = Metrics.get(Key)
                if Got is None:
                    Failures.append(f"missing metric: {Key}")
                    continue
                if Got == float("-inf") and Key in NEVER_NEG_INF:
                    Failures.append(f"{Key}: silent/-inf (no usable audio)")
                    Rows.append(f"  {Key:20} want {Want:>7.2f}  got -inf       FAIL")
                    continue
                Delta = Got - Want
                Allowed = Tolerance.get(Key, 2.0)
                Ok = abs(Delta) <= Allowed
                Marker = "OK  " if Ok else "FAIL"
                Rows.append(f"  {Key:20} want {Want:>7.2f}  got {Got:>7.2f}  Δ {Delta:+6.2f}  (±{Allowed})  {Marker}")
                if not Ok:
                    Failures.append(f"{Key}: Δ={Delta:+.2f} dB exceeds tolerance ±{Allowed}")
            return (not Failures, Rows)
        except Exception as e:
            kS.GlobalErrorHandler(e, "kAudioMeter.CheckAgainst")
            return (False, [])


class kAudioCheckApp:
    """Command line."""

    @staticmethod
    def Calibrate(Input: Path, Metrics: dict[str, float], ToleranceDb: float | None, ProfileOut: str) -> int:
        """Write the profile built from Metrics to ProfileOut and print it."""
        if kS.ErrorMode:
            return 1
        try:
            Tolerance = DEFAULT_TOLERANCE.copy()
            if ToleranceDb is not None:
                Tolerance = {Key: ToleranceDb for Key in Tolerance}
            Profile = {
                "version": 1,
                "calibrated_from": str(Input),
                "baseline": {Key: round(Value, 3) for Key, Value in Metrics.items()},
                "tolerance": Tolerance,
            }
            Out = Path(ProfileOut)
            if not kJsonFile.Save(Out, Profile):
                return 1
            print(f"[vedit/audio_check] calibrated profile → {Out}", file=sys.stderr)
            print(json.dumps(Profile, indent=2))
            return 0
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kAudioCheckApp.Calibrate(file={ProfileOut})")
            return 1

    @staticmethod
    def Check(Metrics: dict[str, float], ProfilePath: str) -> int:
        """Print the comparison with the profile: 0 PASS, 1 FAIL."""
        if kS.ErrorMode:
            return 1
        try:
            Profile = kJsonFile.Load(ProfilePath, "profile")
            if kS.ErrorMode:
                return 1
            Ok, Rows = kAudioMeter.CheckAgainst(Metrics, Profile)
            if kS.ErrorMode:
                return 1
            print()
            print(f"Profile: {Profile.get('calibrated_from', '?')}")
            print("Comparison:")
            for Row in Rows:
                print(Row)
            print()
            if Ok:
                print("PASS — audio quality within tolerance.")
                return 0
            print("FAIL — audio quality outside tolerance. Refuse to process.")
            print("  Re-record with the correct mic, or recalibrate the profile if")
            print("  your setup has genuinely changed.")
            return 1
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kAudioCheckApp.Check(profile={ProfilePath})")
            return 1

    def Run(self) -> int:
        if kS.ErrorMode:
            return 1
        try:
            Parser = argparse.ArgumentParser(description="Audio quality gate for vedit pipeline.")
            Parser.add_argument("--in", dest="inp", required=True)
            Parser.add_argument("--audio-stream", type=int, default=0)
            Parser.add_argument("--calibrate", action="store_true")
            Parser.add_argument("--profile-out", default=None,
                                help="Where to write the profile (calibrate mode).")
            Parser.add_argument("--profile", default=None,
                                help="Profile JSON to check against (default mode).")
            Parser.add_argument("--tolerance-db", type=float, default=None,
                                help="Override all tolerances (calibrate mode writes this into profile).")
            Parser.add_argument("--quiet", action="store_true")
            Args = Parser.parse_args()

            Input = Path(Args.inp)
            if not Input.exists():
                raise ToolInputException(f"input not found: {Input}")
            kFfmpeg.Ensure()
            if not Args.quiet:
                print(f"[vedit/audio_check] measuring {Input.name} (stream a:{Args.audio_stream})…",
                      file=sys.stderr)
            Metrics = kAudioMeter.Measure(Input, Args.audio_stream)
            if kS.ErrorMode:
                return 1
            if Args.calibrate:
                if not Args.profile_out:
                    raise ToolInputException("--profile-out required in calibrate mode")
                return kAudioCheckApp.Calibrate(Input, Metrics, Args.tolerance_db, Args.profile_out)
            # Default mode: probe + optionally check
            print(f"Measured audio (a:{Args.audio_stream}):")
            for Key, Value in Metrics.items():
                ValueText = "-inf" if Value == float("-inf") else f"{Value:.2f}"
                print(f"  {Key:20} {ValueText}")
            if Args.profile:
                if not Path(Args.profile).exists():
                    raise ToolInputException(f"profile not found: {Path(Args.profile)}")
                return kAudioCheckApp.Check(Metrics, Args.profile)
            return 0
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, "kAudioCheckApp.Run")
            return 1


if __name__ == "__main__":
    kRun.Main(kAudioCheckApp)
