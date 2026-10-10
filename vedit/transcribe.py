"""Transcribe a video/audio file with faster-whisper (local, no API).

Outputs a JSON transcript with segment-level and word-level timestamps,
plus per-segment confidence/no-speech-probability fields used by propose.py
to detect retakes and low-quality stretches.

Run via uvx (no venv required):

    uvx --with faster-whisper --with static-ffmpeg python transcribe.py \
        --in "F:\\path\\input.mp4" --out transcript.json

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

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from pathlib import Path

# Force UTF-8 stdout (Windows safe)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Transcribe with faster-whisper.")
    ap.add_argument("--in", dest="inp", required=True, help="Input media file")
    ap.add_argument("--out", dest="out", required=True, help="Output JSON path")
    ap.add_argument("--model", default="small.en")
    ap.add_argument("--language", default="en")
    ap.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    ap.add_argument("--compute-type", default=None,
                    help="Override quantization (int8/float16/float32)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    inp = Path(args.inp)
    out = Path(args.out)
    if not inp.exists():
        sys.stderr.write(f"ERROR: input not found: {inp}\n")
        return 2

    # Make sure ffmpeg is reachable (faster-whisper uses it for decoding)
    try:
        from _ffmpeg import ensure_ffmpeg
    except ImportError:
        sys.path.insert(0, str(Path(__file__).parent))
        from _ffmpeg import ensure_ffmpeg
    ensure_ffmpeg()

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        sys.stderr.write(
            "ERROR: faster-whisper not installed.\n"
            "Run via: uvx --with faster-whisper --with static-ffmpeg python transcribe.py ...\n"
        )
        return 2

    compute_type = args.compute_type or ("int8" if args.device == "cpu" else "float16")
    if not args.quiet:
        print(f"[vedit/transcribe] loading {args.model} on {args.device} ({compute_type})",
              file=sys.stderr)

    t0 = time.monotonic()
    model = WhisperModel(args.model, device=args.device, compute_type=compute_type)

    language = None if args.language == "auto" else args.language
    segments_iter, info = model.transcribe(
        str(inp),
        language=language,
        beam_size=5,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )

    if not args.quiet:
        print(f"[vedit/transcribe] detected language={info.language} "
              f"prob={info.language_probability:.2f} duration={info.duration:.1f}s",
              file=sys.stderr)

    out_segments = []
    last_print = 0.0
    for s in segments_iter:
        words = []
        if s.words:
            for w in s.words:
                words.append({
                    "start": round(w.start, 3),
                    "end": round(w.end, 3),
                    "word": w.word,
                    "probability": round(w.probability, 4),
                })
        out_segments.append({
            "start": round(s.start, 3),
            "end": round(s.end, 3),
            "text": s.text.strip(),
            "avg_logprob": round(s.avg_logprob, 4),
            "no_speech_prob": round(s.no_speech_prob, 4),
            "words": words,
        })
        if not args.quiet and s.end - last_print >= 30.0:
            elapsed = time.monotonic() - t0
            speed = s.end / elapsed if elapsed > 0 else 0
            print(f"  {s.end:7.1f}s / {info.duration:.1f}s "
                  f"({100*s.end/info.duration:5.1f}%, {speed:.2f}x realtime)",
                  file=sys.stderr)
            last_print = s.end

    payload = {
        "source": str(inp),
        "language": info.language,
        "duration_s": round(info.duration, 3),
        "model": args.model,
        "device": args.device,
        "compute_type": compute_type,
        "segments": out_segments,
    }

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    elapsed = time.monotonic() - t0
    if not args.quiet:
        speed = info.duration / elapsed if elapsed > 0 else 0
        print(f"[vedit/transcribe] done in {elapsed:.1f}s ({speed:.2f}x realtime). "
              f"{len(out_segments)} segments → {out}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
