"""
Export a PowerPoint deck as PDF — slides, notes pages, or handouts.

Use for the speaker handout (notes-pages PDF) or for sending the deck to
someone without PowerPoint. Wraps the COM `ExportAsFixedFormat` call so
you don't have to look up the magic constants.

Usage:
    # Default: slides PDF next to the deck
    python export_pdf.py --file deck.pptx

    # Speaker handout (notes pages — slide image on top, notes below)
    python export_pdf.py --file deck.pptx --mode notes --out C:\\out\\handout.pdf

    # Audience handout (6-up grid, two columns)
    python export_pdf.py --file deck.pptx --mode handout --slides-per-page 6

    # Range only
    python export_pdf.py --file deck.pptx --start 1 --end 20

Modes:
    slides   — one slide per page (default; for sending the deck around)
    notes    — slide thumbnail + speaker notes per page (the speaker handout)
    handout  — multiple slides per page in a grid (for audience print-outs)

Run via uvx:
    uvx --with pywin32 python export_pdf.py --file deck.pptx --mode notes

Notes:
    - The deck does NOT need to be open in PowerPoint; this opens it
      read-only without a window.
    - Uses Print intent (higher fidelity than Screen). Embeds fonts.
"""

import argparse
import io
import sys
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

try:
    import win32com.client
    import pythoncom
except ImportError:
    sys.stderr.write(
        "pywin32 not installed. Run with:\n"
        "  uvx --with pywin32 python export_pdf.py [args]\n"
    )
    sys.exit(1)


# PpFixedFormatType
FIXED_PDF = 2
# PpFixedFormatIntent
INTENT_PRINT = 2
# PpPrintOutputType
OUTPUT_SLIDES = 1
OUTPUT_NOTES = 5
OUTPUT_HANDOUTS = {
    1: 9,   # ppPrintOutputOneSlideHandouts
    2: 7,   # ppPrintOutputTwoSlideHandouts
    3: 8,   # ppPrintOutputThreeSlideHandouts
    4: 2,   # ppPrintOutputFourSlideHandouts
    6: 3,   # ppPrintOutputSixSlideHandouts
    9: 4,   # ppPrintOutputNineSlideHandouts
}
# PpPrintRangeType
RANGE_ALL = 1
RANGE_SLIDES = 4


def export(file_path, out_path, mode, slides_per_page, start, end):
    pythoncom.CoInitialize()
    try:
        try:
            app = win32com.client.GetActiveObject("PowerPoint.Application")
        except pythoncom.com_error:
            app = win32com.client.Dispatch("PowerPoint.Application")
            app.Visible = True

        file_norm = file_path.replace("/", "\\").lower()
        target = None
        for pr in app.Presentations:
            if pr.FullName.replace("/", "\\").lower() == file_norm:
                target = pr
                break
        opened = False
        if target is None:
            target = app.Presentations.Open(file_path, ReadOnly=True, WithWindow=False)
            opened = True

        try:
            if mode == "slides":
                output_type = OUTPUT_SLIDES
            elif mode == "notes":
                output_type = OUTPUT_NOTES
            elif mode == "handout":
                if slides_per_page not in OUTPUT_HANDOUTS:
                    raise ValueError(
                        f"--slides-per-page must be one of "
                        f"{sorted(OUTPUT_HANDOUTS)}; got {slides_per_page}")
                output_type = OUTPUT_HANDOUTS[slides_per_page]
            else:
                raise ValueError(f"Unknown mode: {mode}")

            kwargs = dict(
                Path=out_path,
                FixedFormatType=FIXED_PDF,
                Intent=INTENT_PRINT,
                FrameSlides=False,
                HandoutOrder=1,  # horizontal first (irrelevant for slides/notes)
                OutputType=output_type,
                PrintHiddenSlides=False,
                PrintRange=None,
                RangeType=RANGE_ALL,
                IncludeDocProperties=False,
                KeepIRMSettings=True,
                DocStructureTags=True,
                BitmapMissingFonts=True,
                UseISO19005_1=False,
            )
            if start or end:
                first = start or 1
                last = end or target.Slides.Count
                pr = target.PrintOptions.Ranges.Add(first, last)
                kwargs["PrintRange"] = pr
                kwargs["RangeType"] = RANGE_SLIDES

            target.ExportAsFixedFormat(**kwargs)
        finally:
            if opened:
                target.Close()
    finally:
        pythoncom.CoUninitialize()


def main():
    p = argparse.ArgumentParser(
        description="Export a PowerPoint deck as PDF.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--file", required=True, help="Path to .pptx")
    p.add_argument("--out", help="Output PDF path (default: <deck>.<mode>.pdf)")
    p.add_argument("--mode", choices=["slides", "notes", "handout"],
                   default="slides", help="Layout mode (default slides)")
    p.add_argument("--slides-per-page", type=int, default=6,
                   help="Slides per page when --mode handout (1,2,3,4,6,9)")
    p.add_argument("--start", type=int, default=None, help="First slide")
    p.add_argument("--end", type=int, default=None, help="Last slide")
    p.add_argument("--quiet", action="store_true", help="Suppress success output")
    args = p.parse_args()

    file_path = str(Path(args.file).resolve())
    if args.out:
        out_path = str(Path(args.out).resolve())
    else:
        suffix = {"slides": "", "notes": ".notes", "handout": ".handout"}[args.mode]
        out_path = str(Path(file_path).with_suffix("").as_posix() + suffix + ".pdf")
        out_path = str(Path(out_path).resolve())

    export(file_path, out_path, args.mode, args.slides_per_page,
           args.start, args.end)

    if not args.quiet:
        size_kb = Path(out_path).stat().st_size / 1024
        print(f"Wrote {out_path} ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
