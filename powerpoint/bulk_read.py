"""
Bulk-read a PowerPoint deck via COM automation.

Connects to the running PowerPoint instance (or opens a file) and dumps
text + speaker notes for a range of slides to JSON.

Usage:
    # Read the currently-active presentation, all slides, to stdout
    python bulk_read.py

    # Specific file + slide range + output file
    python bulk_read.py --file "C:\\path\\to\\deck.pptx" --start 10 --end 20 --out dump.json

    # Only text (no notes)
    python bulk_read.py --no-notes

Run via uvx if pywin32 is not installed system-wide:
    uvx --with pywin32 python bulk_read.py [args]
"""

import argparse
import io
import json
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows (handles ₂, →, ° etc in PowerPoint text)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

try:
    import win32com.client
    import pythoncom
except ImportError:
    sys.stderr.write(
        "pywin32 not installed. Run with:\n"
        "  uvx --with pywin32 python bulk_read.py [args]\n"
    )
    sys.exit(1)


def get_presentation(file_path: str | None):
    """Return a PowerPoint Presentation COM object.

    If file_path is given, open it (or attach if already open).
    If None, use the active presentation.
    """
    pythoncom.CoInitialize()
    ppt = win32com.client.GetActiveObject("PowerPoint.Application")

    if file_path:
        target = Path(file_path).resolve()
        # Check if already open
        for i in range(1, ppt.Presentations.Count + 1):
            p = ppt.Presentations(i)
            if Path(p.FullName).resolve() == target:
                return p
        return ppt.Presentations.Open(str(target), ReadOnly=True, WithWindow=False)

    if ppt.Presentations.Count == 0:
        raise RuntimeError("No presentation open and no --file given.")
    return ppt.ActivePresentation


def read_slide(slide, include_notes: bool) -> dict:
    """Extract title, body text, and speaker notes from a single slide."""
    title = ""
    body_parts = []

    for j in range(1, slide.Shapes.Count + 1):
        try:
            sh = slide.Shapes(j)
            if not sh.HasTextFrame:
                continue
            if not sh.TextFrame.HasText:
                continue
            text = (sh.TextFrame.TextRange.Text or "").strip()
            if not text:
                continue
            if not title and "Title" in sh.Name:
                title = text
            else:
                body_parts.append(text)
        except Exception:
            # Some shapes throw on .HasTextFrame/.HasText — skip them
            continue

    notes = ""
    if include_notes and slide.HasNotesPage:
        for j in range(1, slide.NotesPage.Shapes.Count + 1):
            try:
                nsh = slide.NotesPage.Shapes(j)
                if nsh.HasTextFrame and nsh.TextFrame.HasText and "Notes" in nsh.Name:
                    notes = (nsh.TextFrame.TextRange.Text or "").strip()
                    break
            except Exception:
                continue

    return {
        "slide": slide.SlideIndex,
        "title": title,
        "body": " | ".join(p for p in body_parts if p != title),
        "notes": notes,
    }


def bulk_read(
    file_path: str | None,
    start: int,
    end: int | None,
    include_notes: bool,
) -> list[dict]:
    presentation = get_presentation(file_path)
    total = presentation.Slides.Count
    first = max(1, start)
    last = total if end is None else min(total, end)

    return [
        read_slide(presentation.Slides(i), include_notes)
        for i in range(first, last + 1)
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--file", help="Path to .pptx (uses active presentation if omitted)")
    parser.add_argument("--start", type=int, default=1, help="First slide (1-based, default 1)")
    parser.add_argument("--end", type=int, default=None, help="Last slide (default: end of deck)")
    parser.add_argument("--out", help="Output JSON file (default: stdout)")
    parser.add_argument("--no-notes", action="store_true", help="Skip speaker notes")
    parser.add_argument("--pretty", action="store_true", help="Indent JSON output")
    args = parser.parse_args()

    data = bulk_read(
        file_path=args.file,
        start=args.start,
        end=args.end,
        include_notes=not args.no_notes,
    )

    output = json.dumps(data, ensure_ascii=False, indent=2 if args.pretty else None)

    if args.out:
        Path(args.out).write_text(output, encoding="utf-8")
        print(f"Wrote {len(data)} slides to {args.out}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
