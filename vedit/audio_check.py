"""Audio quality gate. Refuses to process sub-standard recordings.

The pipeline assumes consistent audio (same mic, same room, same gain). Even
a small mic change ("oh, I used the laptop mic this time") produces a video
that needs to be re-recorded — silence detection thresholds shift, Whisper
accuracy drops, and the final video sounds different from the rest of a
series. This script catches that BEFORE transcription so you don't burn
12 minutes of CPU and then notice.

# Two modes

**Calibrate** (build a baseline from a known-good recording):

    uvx --with static-ffmpeg python audio_check.py \\
        --in good_recording.trec --calibrate --profile-out profile.json

**Check** (compare a new recording against the baseline; non-zero exit on fail):

    uvx --with static-ffmpeg python audio_check.py \\
        --in new_recording.trec --profile profile.json

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

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)


# ---------- ffmpeg probes ---------------------------------------------------

def probe_volume(inp: Path, audio_stream: int) -> dict[str, float]:
    """Run ffmpeg volumedetect, parse mean/max volume from stderr."""
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner",
         "-i", str(inp),
         "-map", f"0:a:{audio_stream}",
         "-af", "volumedetect",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-2000:])
        raise RuntimeError("volumedetect failed")

    mean = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", proc.stderr)
    peak = re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", proc.stderr)
    if not mean or not peak:
        raise RuntimeError("could not parse volumedetect output")
    return {
        "mean_volume_db": float(mean.group(1)),
        "max_volume_db":  float(peak.group(1)),
    }


def probe_ebur128(inp: Path, audio_stream: int) -> dict[str, float]:
    """Run ffmpeg ebur128, parse integrated loudness / LRA / true peak."""
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner",
         "-i", str(inp),
         "-map", f"0:a:{audio_stream}",
         "-af", "ebur128=peak=true",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-2000:])
        raise RuntimeError("ebur128 failed")

    # Parse the Summary block at the end of stderr
    text = proc.stderr
    summary_idx = text.rfind("Summary:")
    if summary_idx < 0:
        raise RuntimeError("could not find ebur128 Summary in output")
    summary = text[summary_idx:]

    integrated = re.search(r"I:\s*(-?[\d.]+|-inf)\s*LUFS", summary)
    lra = re.search(r"LRA:\s*([\d.]+)\s*LU\b", summary)
    peak = re.search(r"Peak:\s*(-?[\d.]+|-inf)\s*dBFS", summary)
    if not integrated or not lra or not peak:
        raise RuntimeError(
            f"could not parse ebur128 Summary fields:\n{summary[:800]}"
        )

    def f(s: str) -> float:
        return float("-inf") if s == "-inf" else float(s)

    return {
        "integrated_lufs":   f(integrated.group(1)),
        "loudness_range_lu": float(lra.group(1)),
        "true_peak_db":      f(peak.group(1)),
    }


def measure(inp: Path, audio_stream: int) -> dict[str, float]:
    m: dict[str, float] = {}
    m.update(probe_volume(inp, audio_stream))
    m.update(probe_ebur128(inp, audio_stream))
    return m


# ---------- check against profile -------------------------------------------

DEFAULT_TOLERANCE: dict[str, float] = {
    "mean_volume_db":   2.0,
    "max_volume_db":    3.0,
    "integrated_lufs":  2.0,
    "true_peak_db":     3.0,
    "loudness_range_lu": 2.5,
}

# Metrics where -inf means "no audio detected" → always fail
NEVER_NEG_INF = {"mean_volume_db", "max_volume_db", "integrated_lufs", "true_peak_db"}


def check_against(metrics: dict[str, float], profile: dict) -> tuple[bool, list[str]]:
    baseline = profile.get("baseline", {})
    tol = {**DEFAULT_TOLERANCE, **profile.get("tolerance", {})}
    failures: list[str] = []
    rows: list[str] = []
    for k, want in baseline.items():
        got = metrics.get(k)
        if got is None:
            failures.append(f"missing metric: {k}")
            continue
        if got == float("-inf") and k in NEVER_NEG_INF:
            failures.append(f"{k}: silent/-inf (no usable audio)")
            rows.append(f"  {k:20} want {want:>7.2f}  got -inf       FAIL")
            continue
        delta = got - want
        t = tol.get(k, 2.0)
        ok = abs(delta) <= t
        marker = "OK  " if ok else "FAIL"
        rows.append(f"  {k:20} want {want:>7.2f}  got {got:>7.2f}  Δ {delta:+6.2f}  (±{t})  {marker}")
        if not ok:
            failures.append(f"{k}: Δ={delta:+.2f} dB exceeds tolerance ±{t}")
    return (not failures, rows)


# ---------- main ------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Audio quality gate for vedit pipeline.")
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--audio-stream", type=int, default=0)
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--profile-out", default=None,
                    help="Where to write the profile (calibrate mode).")
    ap.add_argument("--profile", default=None,
                    help="Profile JSON to check against (default mode).")
    ap.add_argument("--tolerance-db", type=float, default=None,
                    help="Override all tolerances (calibrate mode writes this into profile).")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    inp = Path(args.inp)
    if not inp.exists():
        sys.stderr.write(f"ERROR: input not found: {inp}\n"); return 2

    try:
        from _ffmpeg import ensure_ffmpeg
    except ImportError:
        sys.path.insert(0, str(Path(__file__).parent))
        from _ffmpeg import ensure_ffmpeg
    ensure_ffmpeg()

    if not args.quiet:
        print(f"[vedit/audio_check] measuring {inp.name} (stream a:{args.audio_stream})…",
              file=sys.stderr)
    metrics = measure(inp, args.audio_stream)

    if args.calibrate:
        if not args.profile_out:
            sys.stderr.write("ERROR: --profile-out required in calibrate mode\n"); return 2
        tol = DEFAULT_TOLERANCE.copy()
        if args.tolerance_db is not None:
            tol = {k: args.tolerance_db for k in tol}
        profile = {
            "version": 1,
            "calibrated_from": str(inp),
            "baseline": {k: round(v, 3) for k, v in metrics.items()},
            "tolerance": tol,
        }
        out = Path(args.profile_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(profile, indent=2), encoding="utf-8")
        print(f"[vedit/audio_check] calibrated profile → {out}", file=sys.stderr)
        print(json.dumps(profile, indent=2))
        return 0

    # Default mode: probe + optionally check
    print(f"Measured audio (a:{args.audio_stream}):")
    for k, v in metrics.items():
        vstr = "-inf" if v == float("-inf") else f"{v:.2f}"
        print(f"  {k:20} {vstr}")

    if args.profile:
        pp = Path(args.profile)
        if not pp.exists():
            sys.stderr.write(f"ERROR: profile not found: {pp}\n"); return 2
        profile = json.loads(pp.read_text(encoding="utf-8"))
        ok, rows = check_against(metrics, profile)
        print()
        print(f"Profile: {profile.get('calibrated_from', '?')}")
        print("Comparison:")
        for r in rows:
            print(r)
        print()
        if ok:
            print("PASS — audio quality within tolerance.")
            return 0
        else:
            print("FAIL — audio quality outside tolerance. Refuse to process.")
            print("  Re-record with the correct mic, or recalibrate the profile if")
            print("  your setup has genuinely changed.")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
