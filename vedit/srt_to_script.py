"""Convert an .srt subtitle file to a clean reading script.

Strips cue numbers and timestamps, leaving just the spoken text. Useful for
re-recording: the speaker reads back from the cleaned-up script and the
producer matches the new audio to the existing edit timeline (or just makes
a fresh recording from it).

Run via uvx (stdlib only):

    uvx python srt_to_script.py --in edited.srt --out script.txt

Flags:
    --in PATH           Input .srt file. Required.
    --out PATH          Output .txt file. Default: stdout.
    --paragraphs        Group cues into paragraphs at >=1.5s gaps in the source
                         timeline. Default: one line per cue.
    --width N           Wrap output text at N characters. Default: 80. Set 0
                         to disable wrapping.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import textwrap
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)


TS_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)


def srt_to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_srt(text: str) -> list[dict]:
    """Return list of cues: [{start, end, text}, ...]."""
    cues: list[dict] = []
    current: dict | None = None
    lines: list[str] = []

    def flush():
        nonlocal current, lines
        if current is not None and lines:
            current["text"] = " ".join(l.strip() for l in lines).strip()
            cues.append(current)
        current = None
        lines = []

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            flush()
            continue
        m = TS_RE.search(line)
        if m:
            current = {
                "start": srt_to_seconds(m.group(1), m.group(2), m.group(3), m.group(4)),
                "end":   srt_to_seconds(m.group(5), m.group(6), m.group(7), m.group(8)),
            }
            lines = []
            continue
        if re.match(r"^\d+$", line) and current is None:
            # Cue number — skip
            continue
        if current is not None:
            lines.append(line)
    flush()
    return cues


def format_script(cues: list[dict], paragraphs: bool, width: int) -> str:
    if not cues:
        return ""
    out_lines: list[str] = []
    if paragraphs:
        paragraph: list[str] = []
        for i, c in enumerate(cues):
            paragraph.append(c["text"])
            gap_to_next = (cues[i + 1]["start"] - c["end"]) if i + 1 < len(cues) else 999
            if gap_to_next >= 1.5:
                out_lines.append(" ".join(paragraph))
                out_lines.append("")
                paragraph = []
        if paragraph:
            out_lines.append(" ".join(paragraph))
    else:
        for c in cues:
            out_lines.append(c["text"])

    if width > 0:
        wrapped: list[str] = []
        for line in out_lines:
            if line:
                wrapped.extend(textwrap.wrap(
                    line, width=width, break_long_words=False, break_on_hyphens=False
                ) or [""])
            else:
                wrapped.append("")
        out_lines = wrapped
    return "\n".join(out_lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Strip .srt to a clean reading script.")
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--paragraphs", action="store_true",
                    help="Group cues into paragraphs at 1.5s+ gaps.")
    ap.add_argument("--width", type=int, default=80, help="Wrap width. 0=off.")
    args = ap.parse_args()

    inp = Path(args.inp)
    if not inp.exists():
        sys.stderr.write(f"ERROR: input not found: {inp}\n"); return 2

    cues = parse_srt(inp.read_text(encoding="utf-8"))
    text = format_script(cues, args.paragraphs, args.width)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        sys.stderr.write(f"[vedit/srt_to_script] {len(cues)} cues → {out}\n")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
