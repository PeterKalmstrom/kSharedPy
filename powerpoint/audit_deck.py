"""
Audit a PowerPoint deck for word-budget and font-floor defects.

Connects to the running PowerPoint instance (or opens a file) and walks every
slide. For each slide it computes:

  - Visible body word count (excluding the slide title)
  - Minimum body font size in points
  - Inferred status against the per-anchor word budget and 18pt body floor

Output is a flat tab-aligned table. Statuses:

  OK         — within budget, no font issues
  ok-tight   — within budget but close (8–12 words)
  ** AUDIT   — over budget OR has body text below the 18pt floor
  [skip]     — cover, agenda, rules, or template slide (low slide numbers)
  [vote]     — voting / template slide (HOT/HOPP split, "Rösta nu", etc.)
  [skip-X]   — opponent slides or section dividers (HOPP, Hot Svar, etc.)

Usage:
    # Audit the currently-active presentation
    python audit_deck.py

    # Audit a specific file (opens read-only, no UI)
    python audit_deck.py --file "C:\\path\\to\\deck.pptx"

    # Custom skip patterns (comma-separated, case-insensitive substring match)
    python audit_deck.py --skip "Magnus,Reviewer"

    # JSON output (machine-readable)
    python audit_deck.py --json

Run with uv if pywin32 is not installed system-wide (the script header declares it):
    uv run audit_deck.py [args]
    (without inline-metadata support: uvx --with pywin32 python audit_deck.py [args])

Path matching:
    --file accepts forward or backward slashes; both are normalized before
    matching against open Presentations. So all of these resolve to the same
    deck if it's open in PowerPoint:
        --file "C:/path/to/deck.pptx"
        --file "C:\\path\\to\\deck.pptx"
        --file "deck.pptx"            (basename match against open windows)
    If no open Presentation matches, the file is opened read-only without
    a window and closed again afterwards. (If PowerPoint cannot Open() the
    file — e.g., a stale lock — open it manually in PowerPoint first, then
    re-run this script.)
"""
# /// script
# requires-python = ">=3.10"
# dependencies = ["pywin32; sys_platform == 'win32'"]
# ///

import argparse
import json
from typing import Any

from _ppt import kPptLocator, kPptSession
from kShared import kRun, kS, kToolException

# Default skip patterns (case-insensitive substring match against title)
# Customize with --skip for non-default decks
DEFAULT_SKIP_PATTERNS = [
    "HOPP",          # opponent's hope intro slides in debate decks
    "HOT Svar",      # opponent's rebuttal
    "HOPP Svar",     # opponent's rebuttal
    "Tack",          # closing
    "Avslutning",    # closing
]

# Slide numbers always skipped (covers, hosts, agenda, rules — typically low numbers)
DEFAULT_SKIP_NUMBERS = {1, 2, 3, 4, 5}

# Body floor in points (slide medium)
BODY_FLOOR = 18.0

# Words that are separators, not content
NON_WORDS = ("|", "—", "-", "·")


class kDeckAuditor:
    """Word-budget and font-floor audit of one presentation."""

    def __init__(self, SkipNumbers: set[int], SkipPatterns: list[str]) -> None:
        try:
            self.SkipNumbers = SkipNumbers
            self.SkipPatterns = SkipPatterns
        except Exception as e:
            kS.GlobalErrorHandler(e, "kDeckAuditor.__init__")

    @staticmethod
    def ShapeText(Shape: Any) -> str | None:
        """The shape's text, or None when it has none or PowerPoint refuses to read it."""
        if kS.ErrorMode:
            return None
        try:
            import pythoncom

            try:
                if not Shape.HasTextFrame or not Shape.TextFrame.HasText:
                    return None
                return Shape.TextFrame.TextRange.Text or ""
            except pythoncom.com_error:  # ERROR-SUPPRESSED-JUSTIFIED: some shapes throw on HasTextFrame/HasText - they carry no audit-relevant text
                return None
        except Exception as e:
            kS.GlobalErrorHandler(e, "kDeckAuditor.ShapeText")
            return None

    @staticmethod
    def FontSize(Shape: Any) -> float | None:
        """The shape's font size in points, or None when mixed or unreadable."""
        if kS.ErrorMode:
            return None
        try:
            try:
                return float(Shape.TextFrame.TextRange.Font.Size)
            except Exception:  # ERROR-SUPPRESSED-JUSTIFIED: mixed sizes or no size - the shape just adds no size to the minimum
                return None
        except Exception as e:
            kS.GlobalErrorHandler(e, "kDeckAuditor.FontSize")
            return None

    @staticmethod
    def IsVoteOrTemplate(Slide: Any) -> bool:
        """Detect voting / template slides by content cues."""
        if kS.ErrorMode:
            return False
        try:
            for Shape in Slide.Shapes:
                Text = kDeckAuditor.ShapeText(Shape)
                if Text is None:
                    continue
                TextLower = Text.lower()
                # Common voting-slide markers
                if "rösta nu" in TextLower or "vote now" in TextLower:
                    return True
                # "X — Hot eller hopp?" pattern
                if "hot eller hopp" in TextLower:
                    return True
            return False
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, "kDeckAuditor.IsVoteOrTemplate")
            return False

    @staticmethod
    def SlideMetrics(Slide: Any) -> tuple[str, int, float, list[float]]:
        """Return (Title, BodyWordCount, MinBodyFontPt, BodySizes)."""
        if kS.ErrorMode:
            return "", 0, 0.0, []
        try:
            Title = ""
            BodyWords = 0
            MinSize = None
            Sizes = []
            for Shape in Slide.Shapes:
                Text = (kDeckAuditor.ShapeText(Shape) or "").strip()
                if not Text:
                    continue
                Size = kDeckAuditor.FontSize(Shape)
                # First text shape is treated as the title (don't count its words or size)
                if not Title:
                    Title = Text.replace("\r", " | ").replace("\n", " | ")[:60]
                    continue
                BodyWords += len([Word for Word in Text.split() if Word not in NON_WORDS])
                if Size:
                    Sizes.append(Size)
                    if MinSize is None or Size < MinSize:
                        MinSize = Size
            return Title, BodyWords, MinSize or 0.0, Sizes
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, "kDeckAuditor.SlideMetrics")
            return "", 0, 0.0, []

    def SkipPatternHit(self, Title: str) -> bool:
        """True when the title contains one of the skip patterns."""
        if kS.ErrorMode:
            return False
        try:
            TitleLower = (Title or "").lower()
            for Pattern in self.SkipPatterns:
                if Pattern.lower() in TitleLower:
                    return True
            return False
        except Exception as e:
            kS.GlobalErrorHandler(e, "kDeckAuditor.SkipPatternHit")
            return False

    def StatusFor(self, SlideNum: int, Title: str, BodyWords: int, MinSize: float) -> str:
        """Classify a slide's status."""
        if kS.ErrorMode:
            return ""
        try:
            if SlideNum in self.SkipNumbers:
                return "[skip]"
            TitleLower = (Title or "").lower()
            for Pattern in self.SkipPatterns:
                if Pattern.lower() in TitleLower:
                    return f"[skip-{Pattern[:8]}]"
            OverBudget = BodyWords > 12
            BelowFloor = 0 < MinSize < BODY_FLOOR
            if OverBudget or BelowFloor:
                return "** AUDIT"
            if BodyWords > 8:
                return "ok-tight"
            return "OK"
        except Exception as e:
            kS.GlobalErrorHandler(e, "kDeckAuditor.StatusFor")
            return ""

    def Audit(self, Pres: Any) -> list[dict[str, Any]]:
        """Produce audit records for every slide."""
        if kS.ErrorMode:
            return []
        try:
            Results = []
            for Index in range(1, Pres.Slides.Count + 1):
                Slide = Pres.Slides(Index)
                Title, BodyWords, MinSize, _Sizes = kDeckAuditor.SlideMetrics(Slide)
                # Skipped slides keep their skip status; voting / template slides are detected after that
                if Index in self.SkipNumbers or self.SkipPatternHit(Title):
                    Status = self.StatusFor(Index, Title, BodyWords, MinSize)
                elif kDeckAuditor.IsVoteOrTemplate(Slide):
                    Status = "[vote]"
                else:
                    Status = self.StatusFor(Index, Title, BodyWords, MinSize)
                Results.append({
                    "slide": Index,
                    "title": Title,
                    "body_words": BodyWords,
                    "min_font_pt": round(MinSize, 1),
                    "status": Status,
                })
            return Results
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, "kDeckAuditor.Audit")
            return []


class kAuditReport:
    """Prints the audit records."""

    @staticmethod
    def PrintTable(Results: list[dict[str, Any]], ShowAll: bool = False) -> None:
        """Print a tab-aligned report. Skipped slides hidden unless ShowAll."""
        if kS.ErrorMode:
            return None
        try:
            print(f"{'#':>3} {'words':>5} {'min_pt':>7}  {'status':<12} title")
            print("-" * 100)
            for Record in Results:
                if not ShowAll and Record["status"].startswith("[skip"):
                    continue
                Title = Record["title"] or "(no title)"
                print(f"{Record['slide']:>3} {Record['body_words']:>5} {Record['min_font_pt']:>5.0f}pt"
                      f"   {Record['status']:<12} {Title[:65]}")
            # Summary
            Total = len(Results)
            AuditN = sum(1 for Record in Results if Record["status"] == "** AUDIT")
            TightN = sum(1 for Record in Results if Record["status"] == "ok-tight")
            OkN = sum(1 for Record in Results if Record["status"] == "OK")
            SkipN = sum(1 for Record in Results if Record["status"].startswith("[skip"))
            VoteN = sum(1 for Record in Results if Record["status"] == "[vote]")
            print("-" * 100)
            print(f"  {Total} slides total: {AuditN} need audit, {TightN} tight, "
                  f"{OkN} ok, {VoteN} vote, {SkipN} skipped")
            return None
        except Exception as e:
            kS.GlobalErrorHandler(e, "kAuditReport.PrintTable")
            return None


class kAuditDeckApp:
    """Command line."""

    def Run(self) -> int:
        if kS.ErrorMode:
            return 1
        try:
            Parser = argparse.ArgumentParser(
                description="Audit a PowerPoint deck for word-budget and font-floor defects.",
                formatter_class=argparse.RawDescriptionHelpFormatter,
                epilog=__doc__,
            )
            Parser.add_argument("--file", help="Path to .pptx (default: active presentation)")
            Parser.add_argument("--skip", default="",
                                help="Comma-separated extra title-substrings to skip "
                                     "(beyond defaults like HOPP, HOT Svar)")
            Parser.add_argument("--all", action="store_true",
                                help="Include skipped slides in output")
            Parser.add_argument("--json", action="store_true",
                                help="Output JSON instead of table")
            Args = Parser.parse_args()

            SkipPatterns = list(DEFAULT_SKIP_PATTERNS)
            if Args.skip:
                SkipPatterns.extend(Part.strip() for Part in Args.skip.split(",") if Part.strip())

            kPptLocator.RequirePywin32("audit_deck.py")
            Path = kPptLocator.Resolve(Args.file or "")
            if kS.ErrorMode:
                return 1
            Auditor = kDeckAuditor(DEFAULT_SKIP_NUMBERS, SkipPatterns)
            Results = []
            with kPptSession(Path) as Pres:
                if Pres is None:
                    return 1
                Results = Auditor.Audit(Pres)
            if kS.ErrorMode:
                return 1
            if Args.json:
                print(json.dumps(Results, indent=2, ensure_ascii=False))
            else:
                kAuditReport.PrintTable(Results, ShowAll=Args.all)
            return 0
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, "kAuditDeckApp.Run")
            return 1


if __name__ == "__main__":
    kRun.Main(kAuditDeckApp)
