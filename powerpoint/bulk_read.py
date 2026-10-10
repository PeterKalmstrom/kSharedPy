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

Run with uv if pywin32 is not installed system-wide (the script header declares it):
    uv run bulk_read.py [args]
    (without inline-metadata support: uvx --with pywin32 python bulk_read.py [args])

A deck that is not open in PowerPoint is opened read-only without a window and closed again afterwards.
"""
# /// script
# requires-python = ">=3.10"
# dependencies = ["pywin32; sys_platform == 'win32'"]
# ///

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from _ppt import kPptLocator, kPptSession
from kShared import kRun, kS, kToolException


class kSlideReader:
    """Reads title, body text and speaker notes from slides."""

    @staticmethod
    def ShapeText(Shape: Any) -> str:
        """The shape's stripped text, or "" when it has none or PowerPoint refuses to read it."""
        if kS.ErrorMode:
            return ""
        try:
            try:
                if not Shape.HasTextFrame:
                    return ""
                if not Shape.TextFrame.HasText:
                    return ""
                return (Shape.TextFrame.TextRange.Text or "").strip()
            except Exception:  # ERROR-SUPPRESSED-JUSTIFIED: some shapes throw on .HasTextFrame/.HasText - they carry no readable text
                return ""
        except Exception as e:
            kS.GlobalErrorHandler(e, "kSlideReader.ShapeText")
            return ""

    @staticmethod
    def NotesText(Slide: Any) -> str:
        """The text of the first notes-page shape named "Notes...", or ""."""
        if kS.ErrorMode:
            return ""
        try:
            if not Slide.HasNotesPage:
                return ""
            Shapes = Slide.NotesPage.Shapes
            for Index in range(1, Shapes.Count + 1):
                Shape = Shapes(Index)
                if "Notes" in Shape.Name:
                    Text = kSlideReader.ShapeText(Shape)
                    if Text:
                        return Text
            return ""
        except Exception as e:
            kS.GlobalErrorHandler(e, "kSlideReader.NotesText")
            return ""

    @staticmethod
    def ReadSlide(Slide: Any, IncludeNotes: bool) -> dict[str, Any]:
        """Extract title, body text, and speaker notes from a single slide."""
        if kS.ErrorMode:
            return {}
        try:
            Title = ""
            BodyParts = []
            Shapes = Slide.Shapes
            for Index in range(1, Shapes.Count + 1):
                Shape = Shapes(Index)
                Text = kSlideReader.ShapeText(Shape)
                if not Text:
                    continue
                if not Title and "Title" in Shape.Name:
                    Title = Text
                else:
                    BodyParts.append(Text)
            Notes = kSlideReader.NotesText(Slide) if IncludeNotes else ""
            return {
                "slide": Slide.SlideIndex,
                "title": Title,
                "body": " | ".join(Part for Part in BodyParts if Part != Title),
                "notes": Notes,
            }
        except Exception as e:
            kS.GlobalErrorHandler(e, "kSlideReader.ReadSlide")
            return {}

    @staticmethod
    def BulkRead(Pres: Any, Start: int, End: int | None, IncludeNotes: bool) -> list[dict[str, Any]]:
        """One record per slide from Start to End (clamped to the deck)."""
        if kS.ErrorMode:
            return []
        try:
            Total = Pres.Slides.Count
            First = max(1, Start)
            Last = Total if End is None else min(Total, End)
            Records = []
            for Index in range(First, Last + 1):
                Records.append(kSlideReader.ReadSlide(Pres.Slides(Index), IncludeNotes))
            return Records
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, "kSlideReader.BulkRead")
            return []


class kBulkReadApp:
    """Command line."""

    def Run(self) -> int:
        if kS.ErrorMode:
            return 1
        try:
            Parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
            Parser.add_argument("--file", help="Path to .pptx (uses active presentation if omitted)")
            Parser.add_argument("--start", type=int, default=1, help="First slide (1-based, default 1)")
            Parser.add_argument("--end", type=int, default=None, help="Last slide (default: end of deck)")
            Parser.add_argument("--out", help="Output JSON file (default: stdout)")
            Parser.add_argument("--no-notes", action="store_true", help="Skip speaker notes")
            Parser.add_argument("--pretty", action="store_true", help="Indent JSON output")
            Args = Parser.parse_args()

            kPptLocator.RequirePywin32("bulk_read.py")
            DeckPath = kPptLocator.Resolve(Args.file or "", MatchOpen=False)
            if kS.ErrorMode:
                return 1
            Data = []
            with kPptSession(DeckPath) as Pres:
                if Pres is None:
                    return 1
                Data = kSlideReader.BulkRead(Pres, Args.start, Args.end, not Args.no_notes)
            if kS.ErrorMode:
                return 1

            Output = json.dumps(Data, ensure_ascii=False, indent=2 if Args.pretty else None)
            if Args.out:
                Path(Args.out).write_text(Output, encoding="utf-8")
                print(f"Wrote {len(Data)} slides to {Args.out}", file=sys.stderr)
            else:
                print(Output)
            return 0
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, "kBulkReadApp.Run")
            return 1


if __name__ == "__main__":
    kRun.Main(kBulkReadApp)
