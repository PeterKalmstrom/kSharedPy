"""Review or restore-cuts on an EDL.

Two modes:

1. **Review mode** (default): render a markdown summary of the EDL. For each
   proposed cut, print transcript context (before / inside / after) so you
   can decide whether the cut is correct before running apply.py.

2. **Restore mode** (`--restore CATEGORY`): write a NEW EDL with all cuts in
   the given category flipped back to `keep: true`. Useful for wholesale
   rejection of one cut class (e.g. when you don't trust LLM retakes for a
   particular recording).

Run via uvx (no extra deps — only stdlib + the EDL/transcript JSONs):

    # Review
    uvx python review.py --edl edl.json --transcript transcript.json --out review.md

    # Restore: write a cleaned EDL with all retake cuts un-cut
    uvx python review.py --edl edl.json --restore retake --out edl.cleaned.json

Flags:
    --edl PATH         EDL JSON path. Required.
    --transcript PATH  Transcript JSON path. Required for review mode.
    --out PATH         Output path. Markdown (review mode) or EDL JSON
                       (restore mode). Default: stdout.
    --only CATEGORY    (review mode) Filter to: retake | silence | static.
    --restore CATEGORY (restore mode) Flip all cuts whose reason matches
                       CATEGORY back to keep:true, then merge adjacent
                       kept segments. Output is a new EDL JSON.
    --context-s FLOAT  Seconds of transcript context to show before/after each
                       cut. Default: 3.0.
    --max-line LEN     Wrap transcript lines longer than this. Default: 100.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)


def overlapping(transcript_segs: list[dict], a: float, b: float) -> list[dict]:
    """Transcript segments that overlap [a, b] at all."""
    return [s for s in transcript_segs if s["end"] > a and s["start"] < b]


def words_inside(transcript_segs: list[dict], a: float, b: float) -> str:
    """Extract just the words whose midpoints fall within [a, b].

    Uses word-level timestamps when available; falls back to whole-segment
    text for segments that overlap the cut span.
    """
    words: list[str] = []
    saw_words = False
    for s in transcript_segs:
        if s["end"] <= a or s["start"] >= b:
            continue
        if s.get("words"):
            saw_words = True
            for w in s["words"]:
                mid = (w["start"] + w["end"]) / 2
                if a <= mid <= b:
                    words.append(w["word"])
    if saw_words:
        return "".join(words).strip()
    # Fallback: concat overlapping segment texts
    return " ".join(s["text"] for s in overlapping(transcript_segs, a, b)).strip()


def fmt_time(t: float) -> str:
    m, s = divmod(t, 60)
    return f"{int(m):02d}:{s:05.2f}"


def categorize(reason: str) -> str:
    if "retake" in reason:
        return "retake"
    if "filler" in reason:
        return "filler"
    if "static" in reason:
        return "static"
    if "silence" in reason:
        return "silence"
    return "other"


def render(edl: dict, transcript: dict, only: str | None, context_s: float, max_line: int) -> str:
    segs = edl["segments"]
    cuts = [s for s in segs if not s["keep"]]
    keeps = [s for s in segs if s["keep"]]
    total = float(edl["duration_s"])
    cut_dur = sum(s["end"] - s["start"] for s in cuts)
    keep_dur = sum(s["end"] - s["start"] for s in keeps)

    by_cat: dict[str, int] = {}
    for c in cuts:
        by_cat[categorize(c["reason"])] = by_cat.get(categorize(c["reason"]), 0) + 1

    out: list[str] = []
    out.append(f"# EDL review: {Path(edl['source']).name}")
    out.append("")
    out.append(f"- **Source:** `{edl['source']}`")
    out.append(f"- **Duration:** {fmt_time(total)} → {fmt_time(keep_dur)} "
               f"(cut {cut_dur:.1f}s, {100 * cut_dur / total:.1f}%)")
    out.append(f"- **{len(cuts)} cuts** by category: " +
               ", ".join(f"{n} {k}" for k, n in sorted(by_cat.items(), key=lambda x: -x[1])))
    if only:
        out.append(f"- **Filtered to:** `{only}` only")
    out.append("")

    tsegs = transcript["segments"]
    shown = 0
    for i, c in enumerate(cuts, 1):
        cat = categorize(c["reason"])
        if only and only not in cat:
            continue
        shown += 1
        a, b = c["start"], c["end"]
        cut_text = words_inside(tsegs, a, b)
        # "before" = segments that end at or before cut start (no overlap with cut)
        before = [s for s in tsegs if s["end"] <= a + 0.05 and s["end"] > a - context_s]
        # "after"  = segments that start at or after cut end (no overlap with cut)
        after = [s for s in tsegs if s["start"] >= b - 0.05 and s["start"] < b + context_s]

        out.append(f"## Cut #{i} — {fmt_time(a)} → {fmt_time(b)} ({b-a:.1f}s)")
        out.append(f"**Reason:** {c['reason']}")
        out.append("")
        out.append("```")
        for s in before[-3:]:
            line = f"  [{s['start']:6.1f}] {s['text']}"
            out.append(line[:max_line])
        if cut_text:
            line = f"✂ [{a:6.1f}] {cut_text}    ← CUT"
            out.append(line[:max_line])
        else:
            out.append(f"✂ [{a:6.1f}] (silence — no transcript)    ← CUT ({b-a:.1f}s)")
        for s in after[:3]:
            line = f"  [{s['start']:6.1f}] {s['text']}"
            out.append(line[:max_line])
        out.append("```")
        out.append("")

    if only and shown == 0:
        out.append(f"*(no cuts matched filter `{only}`)*")
    out.append("---")
    out.append("")
    out.append("To restore a cut, edit the EDL JSON and flip its `keep` from `false` to `true`.")
    out.append("Then re-run `apply.py`. The EDL is the source of truth — `review.py` is read-only.")
    return "\n".join(out) + "\n"


def restore(edl: dict, category: str) -> tuple[dict, int]:
    """Return (new_edl, n_restored) with cuts whose reason contains CATEGORY
    flipped to keep:true. Adjacent kept segments are then merged so the
    EDL stays clean."""
    n = 0
    flipped: list[dict] = []
    for s in edl["segments"]:
        if not s["keep"] and category in categorize(s["reason"]):
            flipped.append({**s, "keep": True, "reason": f"restored from: {s['reason']}"})
            n += 1
        else:
            flipped.append(dict(s))

    # Merge adjacent kept segments
    merged: list[dict] = []
    for s in flipped:
        if merged and merged[-1]["keep"] and s["keep"]:
            merged[-1] = {
                "start": merged[-1]["start"],
                "end": s["end"],
                "keep": True,
                "reason": "" if not merged[-1].get("reason") and not s.get("reason") else
                          (merged[-1].get("reason", "") or s.get("reason", "")),
            }
        else:
            merged.append(s)

    new_edl = {**edl, "segments": merged}
    return new_edl, n


def main() -> int:
    ap = argparse.ArgumentParser(description="Review or restore-cuts on an EDL.")
    ap.add_argument("--edl", required=True)
    ap.add_argument("--transcript", default=None,
                    help="Transcript JSON path. Required for review mode.")
    ap.add_argument("--out", default=None)
    ap.add_argument("--only", default=None,
                    help="(review) Filter to category: retake | silence | static")
    ap.add_argument("--restore", default=None,
                    help="(restore) Flip cuts in CATEGORY to keep:true, write new EDL.")
    ap.add_argument("--context-s", type=float, default=3.0)
    ap.add_argument("--max-line", type=int, default=100)
    args = ap.parse_args()

    edl_path = Path(args.edl)
    if not edl_path.exists():
        sys.stderr.write(f"ERROR: EDL not found: {edl_path}\n"); return 2
    edl = json.loads(edl_path.read_text(encoding="utf-8"))

    if args.restore:
        new_edl, n = restore(edl, args.restore)
        out_text = json.dumps(new_edl, ensure_ascii=False, indent=2)
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(out_text, encoding="utf-8")
            sys.stderr.write(
                f"[vedit/review] restored {n} '{args.restore}' cuts → {args.out}\n"
            )
        else:
            sys.stdout.write(out_text)
        return 0

    # Review mode
    if not args.transcript:
        sys.stderr.write("ERROR: --transcript required for review mode\n"); return 2
    tpath = Path(args.transcript)
    if not tpath.exists():
        sys.stderr.write(f"ERROR: transcript not found: {tpath}\n"); return 2
    transcript = json.loads(tpath.read_text(encoding="utf-8"))

    md = render(edl, transcript, args.only, args.context_s, args.max_line)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(md, encoding="utf-8")
        sys.stderr.write(f"[vedit/review] wrote {args.out}\n")
    else:
        sys.stdout.write(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
