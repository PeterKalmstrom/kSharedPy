"""
Render a contact-sheet PNG of every slide in a deck.

Renders every slide via PowerPoint's Save As JPEG (COM SaveCopyAs, format 17 --
NOT Slide.Export, which draws embedded fonts in a fallback face), then stitches them into a
single grid image so you can review a whole deck at-a-glance. Useful for
spotting layout/density imbalances and monotone-anchor stretches that don't
show up in per-slide audits.

Usage:
    # Default: 4 columns, 320px slide width, output next to deck
    python contact_sheet.py --file "C:\\path\\to\\deck.pptx"

    # Custom grid + output
    python contact_sheet.py --file deck.pptx --cols 6 --slide-width 240 \\
        --out C:\\out\\sheet.png

    # Range of slides only
    python contact_sheet.py --file deck.pptx --start 10 --end 30

Run via uvx (pywin32 + Pillow installed on demand):
    uvx --with pywin32 --with pillow python contact_sheet.py --file deck.pptx

Output: a single PNG, slides laid out left-to-right, top-to-bottom, with a
small slide-number caption under each thumbnail. Background is white.

Notes:
    - Per-slide PNGs are written to a temp folder and deleted after stitching.
    - Aspect ratio is taken from the deck (16:9, 4:3, etc.) — not assumed.
    - The deck does NOT need to be open in PowerPoint; this opens it
      read-only without a window.
"""

import argparse
import io
import os
import sys
import tempfile
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
        "  uvx --with pywin32 --with pillow python contact_sheet.py [args]\n"
    )
    sys.exit(1)

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.stderr.write(
        "Pillow not installed. Run with:\n"
        "  uvx --with pywin32 --with pillow python contact_sheet.py [args]\n"
    )
    sys.exit(1)


PP_SAVE_AS_JPG = 17  # PowerPoint's own File > Save As > JPEG


def render_all_jpg(target, out_dir):
    """Render every slide via SaveCopyAs(JPEG). Returns {slide_num: jpg_path}.

    NOT Slide.Export: runtime-confirmed 2026-09-24, Slide.Export draws an EMBEDDED
    (not installed) font in an Arial-like fallback, so a real mid-word break in the
    embedded display face rendered clean. SaveCopyAs(folder, 17) uses the embedded
    font, and unlike SaveAs it never rebinds an already-open presentation.
    """
    jpg_dir = os.path.join(out_dir, "_jpg")
    target.SaveCopyAs(jpg_dir, PP_SAVE_AS_JPG)
    found = {}
    for name in os.listdir(jpg_dir):
        digits = "".join(ch for ch in name if ch.isdigit())
        if digits and name.lower().endswith(".jpg"):
            found[int(digits)] = os.path.join(jpg_dir, name)
    if len(found) != target.Slides.Count:
        raise RuntimeError("Save As JPEG wrote %d images for %d slides"
                           % (len(found), target.Slides.Count))
    return found


def export_slides(file_path, out_dir, slide_width, start, end):
    """Render each slide to a PNG thumbnail. Returns list of (slide_num, png_path)."""
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
            total = target.Slides.Count
            first = max(1, start)
            last = total if end is None else min(total, end)

            # Compute height from deck aspect ratio
            page_w = target.PageSetup.SlideWidth   # points
            page_h = target.PageSetup.SlideHeight  # points
            slide_height = int(slide_width * page_h / page_w)

            rendered = render_all_jpg(target, out_dir)
            results = []
            for i in range(first, last + 1):
                png_path = os.path.join(out_dir, f"slide_{i:03d}.png")
                Image.open(rendered[i]).resize((slide_width, slide_height),
                                               Image.LANCZOS).save(png_path)
                results.append((i, png_path))
            return results, slide_height
        finally:
            if opened:
                target.Close()
    finally:
        pythoncom.CoUninitialize()


def stitch(slides, slide_width, slide_height, cols, out_path,
           gap=8, caption_h=18):
    """Compose slides into a grid PNG with slide-number captions."""
    rows = (len(slides) + cols - 1) // cols
    cell_w = slide_width + gap
    cell_h = slide_height + caption_h + gap
    canvas = Image.new("RGB",
                       (cols * cell_w + gap, rows * cell_h + gap),
                       "white")
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("arial.ttf", 12)
    except OSError:
        font = ImageFont.load_default()

    for idx, (slide_num, png_path) in enumerate(slides):
        r, c = divmod(idx, cols)
        x = gap + c * cell_w
        y = gap + r * cell_h
        thumb = Image.open(png_path)
        canvas.paste(thumb, (x, y))
        draw.text((x + 4, y + slide_height + 2), f"#{slide_num}",
                  fill="black", font=font)

    canvas.save(out_path, "PNG", optimize=True)


def main():
    p = argparse.ArgumentParser(
        description="Render a contact-sheet PNG of every slide in a deck.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--file", required=True, help="Path to .pptx")
    p.add_argument("--out", help="Output PNG path (default: <deck>_contact.png)")
    p.add_argument("--cols", type=int, default=4, help="Columns in grid (default 4)")
    p.add_argument("--slide-width", type=int, default=320,
                   help="Per-slide width in pixels (default 320)")
    p.add_argument("--start", type=int, default=1, help="First slide (1-based)")
    p.add_argument("--end", type=int, default=None, help="Last slide (default: end of deck)")
    p.add_argument("--quiet", action="store_true", help="Suppress progress output")
    args = p.parse_args()

    file_path = str(Path(args.file).resolve())
    out_path = args.out or str(Path(file_path).with_name(
        Path(file_path).stem + "_contact.png"))

    with tempfile.TemporaryDirectory() as tmp:
        if not args.quiet:
            print(f"Exporting slides to {tmp}...", flush=True)
        slides, slide_h = export_slides(
            file_path, tmp, args.slide_width, args.start, args.end)
        if not args.quiet:
            print(f"Stitching {len(slides)} slides into {args.cols} cols...",
                  flush=True)
        stitch(slides, args.slide_width, slide_h, args.cols, out_path)
    if not args.quiet:
        size_kb = os.path.getsize(out_path) / 1024
        print(f"Wrote {out_path} ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
