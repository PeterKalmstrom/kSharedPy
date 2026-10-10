"""Emit .srt or .vtt subtitles from a transcript JSON.

If --edl is provided, timestamps are re-mapped to the post-cut timeline so
the subtitle file aligns with the edited video produced by apply.py.

**Readability rule** (default behavior): cue texts are polished via Gemini
LLM so they read smoothly. Verbal restarts, filler ("you know"), and grammar
artifacts from speech are removed. The polished cues are NOT word-for-word
faithful to the audio, by design — viewers reading subtitles want clean
sentences, not a literal transcript. Disable with --no-polish if you need
a verbatim transcript-style .srt.

Run via uvx:

    # Polished subtitles (default; needs GEMINI_API_KEY)
    uvx --with google-genai python subtitles.py \\
        --transcript transcript.json --edl edl.json --out edited.srt

    # Raw verbatim transcript (no LLM call)
    uvx python subtitles.py --no-polish \\
        --transcript transcript.json --edl edl.json --out edited.srt

Flags:
    --transcript PATH  Transcript JSON (output of transcribe.py). Required.
    --edl PATH         EDL JSON. Optional. When given, timestamps are
                       re-mapped to the cut timeline.
    --out PATH         Output file. Format inferred from extension
                       (.srt or .vtt). Required.
    --format FMT       Override format: srt | vtt. Default: from extension.
    --max-line LEN     Soft-wrap subtitle text at this character length.
                       Default: 80.
    --no-polish        Skip the LLM polish pass and emit verbatim transcript.
    --polish-model M   Gemini model for polish. Default: gemini-2.5-flash.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)


def remap_to_cut_timeline(
    transcript_segs: list[dict],
    keep_ranges: list[tuple[float, float]],
) -> list[dict]:
    """Re-map transcript segments to a timeline where everything outside
    keep_ranges has been removed and kept ranges are concatenated.

    Each input segment gets clipped to each kept range it overlaps. The
    text is clipped too — only the WORDS whose midpoint falls inside the
    kept portion are emitted. This prevents duplicate text when a segment
    straddles a cut boundary (which is common after word-snap).
    """
    out: list[dict] = []
    cumulative = 0.0  # post-cut time at which the current keep range starts
    for ks, ke in keep_ranges:
        for s in transcript_segs:
            if s["end"] <= ks or s["start"] >= ke:
                continue
            clipped_start = max(s["start"], ks)
            clipped_end = min(s["end"], ke)
            if clipped_end <= clipped_start + 0.01:
                continue

            # Pick words by midpoint inside [clipped_start, clipped_end].
            words = s.get("words") or []
            if words:
                kept = [w for w in words
                        if clipped_start <= (w["start"] + w["end"]) / 2 <= clipped_end]
                if not kept:
                    continue
                text = "".join(w["word"] for w in kept).strip()
                # Use the kept words' tight time bounds for better cue alignment
                wstart = max(clipped_start, kept[0]["start"])
                wend = min(clipped_end, kept[-1]["end"])
            else:
                text = s["text"]
                wstart = clipped_start
                wend = clipped_end

            if not text:
                continue
            new_start = cumulative + (wstart - ks)
            new_end = cumulative + (wend - ks)
            out.append({"start": new_start, "end": new_end, "text": text})
        cumulative += ke - ks
    return out


POLISH_PROMPT = """You are cleaning up the subtitle text for a screen-recording tutorial. Each input line is one subtitle cue, marked with [N] where N is the cue number. Output the same cues with the same numbers, but with the TEXT polished for readability.

# Rules

1. Output EVERY cue. Same numbers. Same count. Do not merge cues, do not skip cues, do not insert cues.
2. Polish each cue's text:
   - Remove verbal restarts and stutters ("So I told it, after that I told it" → "So I told it")
   - Remove filler ("you know", "actually", "I mean", "or something like that")
   - Fix grammar artifacts from spoken English
3. Readability is more important than literal accuracy. You may slightly rewrite to make a cue read well on its own.
4. Keep the casual first-person tutorial voice. Do NOT formalize the tone.
5. Keep proper nouns and product names EXACT: Claude, Claude Code, CLAUDE.md, SKILL.md, Visual Studio Code, Azure, Peter Kalmström (note: Kalmström, NOT "Kammstrom" — fix this if it appears).
6. Keep each cue's text length similar to the input. If the speaker's words are already clean, return them unchanged.
7. If a sentence spans multiple cues, polish coherently across them — but keep the cue boundaries.

# Output format (STRICT)

[1] <polished text for cue 1>
[2] <polished text for cue 2>
[3] <polished text for cue 3>
...

No preamble, no explanation, no markdown. Just the [N] lines.

# Input cues

{cues}
"""


def polish_cues_with_llm(
    cues: list[dict], model: str,
) -> list[dict]:
    """Polish the .text of each cue via Gemini. Cue boundaries and timing are preserved.

    On any error (no key, API failure, bad response), returns the input cues
    unchanged and prints a warning — the .srt should still be produced.
    """
    if not cues:
        return cues
    if "GEMINI_API_KEY" not in os.environ and "GOOGLE_API_KEY" not in os.environ:
        sys.stderr.write(
            "[vedit/subtitles] WARNING: GEMINI_API_KEY not set; skipping polish "
            "(raw transcript will be used). Pass --no-polish to suppress this warning.\n"
        )
        return cues
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        sys.stderr.write(
            "[vedit/subtitles] WARNING: google-genai not installed; skipping polish.\n"
            "  Run with: uvx --with google-genai python subtitles.py ...\n"
        )
        return cues

    numbered = "\n".join(f"[{i+1}] {c['text']}" for i, c in enumerate(cues))
    prompt = POLISH_PROMPT.format(cues=numbered)

    sys.stderr.write(
        f"[vedit/subtitles] polishing {len(cues)} cues via {model} "
        f"({len(numbered):,} chars)…\n"
    )

    try:
        client = genai.Client()
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.2),
        )
    except Exception as ex:
        sys.stderr.write(
            f"[vedit/subtitles] WARNING: Gemini polish failed ({ex}); using raw text.\n"
        )
        return cues

    raw = response.text or ""
    pattern = re.compile(r"^\[(\d+)\]\s*(.*)$")
    polished_by_index: dict[int, str] = {}
    for line in raw.splitlines():
        m = pattern.match(line.strip())
        if not m:
            continue
        idx = int(m.group(1))
        polished_by_index[idx] = m.group(2).strip()

    out: list[dict] = []
    missing = 0
    for i, c in enumerate(cues):
        polished_text = polished_by_index.get(i + 1)
        if polished_text:
            out.append({**c, "text": polished_text})
        else:
            missing += 1
            out.append(c)

    if missing:
        sys.stderr.write(
            f"[vedit/subtitles] WARNING: {missing}/{len(cues)} cues missing from "
            f"polish response; left raw.\n"
        )
    return out


def fmt_srt_time(t: float) -> str:
    h = int(t // 3600); t -= h * 3600
    m = int(t // 60);   t -= m * 60
    s = int(t);         ms = int(round((t - s) * 1000))
    if ms == 1000:
        s += 1; ms = 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fmt_vtt_time(t: float) -> str:
    return fmt_srt_time(t).replace(",", ".")


def wrap_text(text: str, max_line: int) -> str:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_line:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w) if cur else w
    if cur:
        lines.append(cur)
    return "\n".join(lines)


def to_srt(segments: list[dict], max_line: int) -> str:
    out: list[str] = []
    for i, s in enumerate(segments, 1):
        out.append(str(i))
        out.append(f"{fmt_srt_time(s['start'])} --> {fmt_srt_time(s['end'])}")
        out.append(wrap_text(s["text"].strip(), max_line))
        out.append("")
    return "\n".join(out)


def to_vtt(segments: list[dict], max_line: int) -> str:
    out: list[str] = ["WEBVTT", ""]
    for s in segments:
        out.append(f"{fmt_vtt_time(s['start'])} --> {fmt_vtt_time(s['end'])}")
        out.append(wrap_text(s["text"].strip(), max_line))
        out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Emit .srt/.vtt from transcript ± EDL.")
    ap.add_argument("--transcript", required=True)
    ap.add_argument("--edl", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--format", default=None, choices=["srt", "vtt"])
    ap.add_argument("--max-line", type=int, default=80)
    ap.add_argument("--no-polish", action="store_true",
                    help="Skip the LLM polish pass (default: polish on).")
    ap.add_argument("--polish-model", default="gemini-2.5-flash")
    args = ap.parse_args()

    tpath = Path(args.transcript)
    if not tpath.exists():
        sys.stderr.write(f"ERROR: transcript not found: {tpath}\n"); return 2
    transcript = json.loads(tpath.read_text(encoding="utf-8"))

    segments = transcript["segments"]

    if args.edl:
        epath = Path(args.edl)
        if not epath.exists():
            sys.stderr.write(f"ERROR: EDL not found: {epath}\n"); return 2
        edl = json.loads(epath.read_text(encoding="utf-8"))
        keep_ranges = [(float(s["start"]), float(s["end"]))
                       for s in edl["segments"] if s["keep"]]
        segments = remap_to_cut_timeline(segments, keep_ranges)

    if not args.no_polish:
        segments = polish_cues_with_llm(segments, args.polish_model)

    out_path = Path(args.out)
    fmt = args.format or out_path.suffix.lstrip(".").lower()
    if fmt not in ("srt", "vtt"):
        sys.stderr.write(f"ERROR: unsupported format '{fmt}' "
                         f"(use --format srt|vtt or .srt/.vtt extension)\n")
        return 2

    text = to_srt(segments, args.max_line) if fmt == "srt" else to_vtt(segments, args.max_line)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    sys.stderr.write(f"[vedit/subtitles] wrote {len(segments)} cues "
                     f"({'cut-timeline' if args.edl else 'source-timeline'}) → {out_path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
