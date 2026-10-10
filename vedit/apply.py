"""Apply an EDL: cut+concat the keep ranges from a video.

Reads an edl.json (see vedit/README.md for format), pulls out every segment
with `keep: true`, and stitches them into a single output video.

Default mode re-encodes for precise cuts at arbitrary timestamps. Use
`--copy` for stream-copy (fast, but cuts snap to keyframe boundaries — only
safe if segment boundaries already align with keyframes).

Run via uvx:

    uvx --with static-ffmpeg python apply.py \
        --in "F:\\path\\input.mp4" --edl edl.json --out edited.mp4

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

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)


def load_edl(edl_path: Path) -> tuple[str, float, list[dict]]:
    data = json.loads(edl_path.read_text(encoding="utf-8"))
    segs = data["segments"]
    # Validate contiguity
    for i, s in enumerate(segs):
        if s["end"] <= s["start"]:
            raise ValueError(f"segment {i}: end <= start ({s})")
        if i > 0 and abs(segs[i - 1]["end"] - s["start"]) > 0.001:
            raise ValueError(f"segment {i}: not contiguous with previous")
    return data["source"], float(data.get("duration_s", segs[-1]["end"])), segs


def keep_ranges(segments: list[dict], pad: float, total: float) -> list[tuple[float, float]]:
    """Collapse adjacent kept segments and apply padding (clipped to [0, total])."""
    ranges: list[tuple[float, float]] = []
    for s in segments:
        if not s["keep"]:
            continue
        a = max(0.0, s["start"] - pad)
        b = min(total, s["end"] + pad)
        if ranges and a <= ranges[-1][1] + 0.01:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], b))
        else:
            ranges.append((a, b))
    return ranges


def build_filter_complex(
    ranges: list[tuple[float, float]],
    audio_src: str,
    audio_fade_s: float = 0.030,
    fps: float = 30.0,
) -> str:
    """audio_src is the filter-graph label for the audio source — e.g.
    "[0:a:0]" for stream 0, or "[amix]" if upstream amerged the streams.

    audio_fade_s adds a per-segment fade-in/fade-out on the audio to suppress
    click/pop artifacts at cut boundaries. If a segment is shorter than
    2*audio_fade_s, fades are skipped for that segment.

    fps forces a constant frame rate on the concat output. Required because
    screen-recording codecs (e.g. tscc2 from Camtasia) carry VFR timing
    through trim+concat, leaving the encoded output at low avg_frame_rate
    which confuses Windows Media metadata and some players.
    """
    parts = []
    labels = []
    for i, (a, b) in enumerate(ranges):
        dur = b - a
        parts.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}]")
        a_chain = f"{audio_src}atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS"
        if audio_fade_s > 0 and dur > 2 * audio_fade_s + 0.01:
            a_chain += (f",afade=t=in:st=0:d={audio_fade_s:.3f}"
                        f",afade=t=out:st={dur - audio_fade_s:.3f}:d={audio_fade_s:.3f}")
        a_chain += f"[a{i}]"
        parts.append(a_chain)
        labels.append(f"[v{i}][a{i}]")
    parts.append(f"{''.join(labels)}concat=n={len(ranges)}:v=1:a=1[concatv][outa]")
    # Force CFR on the concatenated video to keep avg_frame_rate sane.
    parts.append(f"[concatv]fps={fps:g}[outv]")
    return ";\n".join(parts)


def run_reencode(
    inp: Path, out: Path, ranges: list[tuple[float, float]],
    vcodec: str, acodec: str, crf: int, preset: str,
    audio_stream: int, mix_audio: bool, num_audio_streams: int,
    audio_fade_s: float, fps: float, pix_fmt: str,
    quiet: bool,
) -> int:
    if not ranges:
        sys.stderr.write("ERROR: no keep ranges in EDL — nothing to output.\n")
        return 1

    if mix_audio and num_audio_streams > 1:
        # amerge all audio streams, then feed into the trim/concat chain
        amerge_inputs = "".join(f"[0:a:{i}]" for i in range(num_audio_streams))
        prefix = f"{amerge_inputs}amerge=inputs={num_audio_streams}[amix]"
        fc = prefix + ";\n" + build_filter_complex(ranges, "[amix]", audio_fade_s, fps)
    else:
        fc = build_filter_complex(ranges, f"[0:a:{audio_stream}]", audio_fade_s, fps)
    # Write filter to a temp file so we don't blow up command-line length on Windows
    with tempfile.NamedTemporaryFile("w", suffix=".filter", delete=False, encoding="utf-8") as f:
        f.write(fc)
        filter_path = f.name

    cmd = [
        "ffmpeg", "-y",
        "-i", str(inp),
        "-filter_complex_script", filter_path,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", vcodec, "-crf", str(crf), "-preset", preset,
        "-pix_fmt", pix_fmt,
        "-c:a", acodec, "-b:a", "192k",
        "-movflags", "+faststart",
        str(out),
    ]
    if quiet:
        cmd.insert(1, "-loglevel"); cmd.insert(2, "error")

    if not quiet:
        print(f"[vedit/apply] re-encode: {len(ranges)} keep ranges → {out}", file=sys.stderr)
    rc = subprocess.run(cmd).returncode
    Path(filter_path).unlink(missing_ok=True)
    return rc


def probe_audio_stream_count(inp: Path) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", str(inp)],
        capture_output=True, text=True, check=True,
    )
    return len([ln for ln in out.stdout.splitlines() if ln.strip()])


def run_copy(
    inp: Path, out: Path, ranges: list[tuple[float, float]], quiet: bool,
) -> int:
    """Stream-copy mode: extract each range to a temp file, then concat. Fast,
    but cut precision is keyframe-bound."""
    if not ranges:
        sys.stderr.write("ERROR: no keep ranges in EDL — nothing to output.\n")
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vedit_") as td:
        td_path = Path(td)
        list_path = td_path / "concat.txt"
        parts: list[Path] = []
        for i, (a, b) in enumerate(ranges):
            part = td_path / f"part_{i:04d}.mp4"
            cmd = [
                "ffmpeg", "-y",
                "-ss", f"{a:.3f}", "-to", f"{b:.3f}",
                "-i", str(inp),
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                str(part),
            ]
            if quiet:
                cmd.insert(1, "-loglevel"); cmd.insert(2, "error")
            rc = subprocess.run(cmd).returncode
            if rc != 0:
                sys.stderr.write(f"ERROR: failed to extract range {i} ({a}–{b})\n")
                return rc
            parts.append(part)

        list_path.write_text(
            "\n".join(f"file '{p.as_posix()}'" for p in parts), encoding="utf-8"
        )
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_path),
            "-c", "copy",
            "-movflags", "+faststart",
            str(out),
        ]
        if quiet:
            cmd.insert(1, "-loglevel"); cmd.insert(2, "error")
        return subprocess.run(cmd).returncode


def main() -> int:
    ap = argparse.ArgumentParser(description="Apply an EDL: cut+concat keep ranges.")
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--edl", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--copy", action="store_true",
                    help="Stream-copy (fast, keyframe-bound). Default: re-encode.")
    ap.add_argument("--vcodec", default="libx264")
    ap.add_argument("--acodec", default="aac")
    ap.add_argument("--crf", type=int, default=20)
    ap.add_argument("--preset", default="medium")
    ap.add_argument("--keep-padding-s", type=float, default=0.0)
    ap.add_argument("--audio-stream", type=int, default=0,
                    help="Which audio stream to keep (0-indexed). Default: 0.")
    ap.add_argument("--mix-audio", action="store_true",
                    help="Mix all audio streams into one (overrides --audio-stream).")
    ap.add_argument("--audio-fade-ms", type=int, default=30,
                    help="Audio fade-in/out at each cut boundary, ms. Default: 30. 0=disabled.")
    ap.add_argument("--fps", type=float, default=30.0,
                    help="Output frame rate (CFR). Default: 30.")
    ap.add_argument("--pix-fmt", default="yuv420p",
                    help="Output pixel format. Default: yuv420p (broad compatibility).")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    inp = Path(args.inp)
    out = Path(args.out)
    edl_path = Path(args.edl)
    if not inp.exists():
        sys.stderr.write(f"ERROR: input not found: {inp}\n"); return 2
    if not edl_path.exists():
        sys.stderr.write(f"ERROR: EDL not found: {edl_path}\n"); return 2

    try:
        from _ffmpeg import ensure_ffmpeg
    except ImportError:
        sys.path.insert(0, str(Path(__file__).parent))
        from _ffmpeg import ensure_ffmpeg
    ensure_ffmpeg()

    src, total, segs = load_edl(edl_path)
    ranges = keep_ranges(segs, args.keep_padding_s, total)
    kept = sum(b - a for a, b in ranges)
    if not args.quiet:
        print(f"[vedit/apply] EDL: {len(segs)} segments, {len(ranges)} kept ranges, "
              f"{kept:.1f}s of {total:.1f}s ({100*kept/total:.1f}%)", file=sys.stderr)

    out.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    if args.copy:
        rc = run_copy(inp, out, ranges, args.quiet)
    else:
        n_audio = probe_audio_stream_count(inp)
        if not args.quiet and n_audio != 1:
            print(f"[vedit/apply] {n_audio} audio streams; using "
                  f"{'amix' if args.mix_audio else f'stream {args.audio_stream}'}",
                  file=sys.stderr)
        rc = run_reencode(inp, out, ranges,
                          args.vcodec, args.acodec, args.crf, args.preset,
                          args.audio_stream, args.mix_audio, n_audio,
                          args.audio_fade_ms / 1000.0,
                          args.fps, args.pix_fmt,
                          args.quiet)

    if rc == 0 and not args.quiet:
        print(f"[vedit/apply] done in {time.monotonic() - t0:.1f}s → {out}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
