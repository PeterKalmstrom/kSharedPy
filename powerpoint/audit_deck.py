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

Run via uvx if pywin32 is not installed system-wide:
    uvx --with pywin32 python audit_deck.py [args]

Path matching:
    --file accepts forward or backward slashes; both are normalized before
    matching against open Presentations. So all of these resolve to the same
    deck if it's open in PowerPoint:
        --file "C:/path/to/deck.pptx"
        --file "C:\\path\\to\\deck.pptx"
        --file "deck.pptx"            (basename match against open windows)
    If no open Presentation matches, the file is opened read-only without
    a window. (If PowerPoint cannot Open() the file — e.g., a stale lock —
    open it manually in PowerPoint first, then re-run this script.)
"""

import argparse
import io
import json
import sys

# Ensure UTF-8 output on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

try:
    import win32com.client
    import pythoncom
except ImportError:
    sys.stderr.write(
        "pywin32 not installed. Run with:\n"
        "  uvx --with pywin32 python audit_deck.py [args]\n"
    )
    sys.exit(1)


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


def is_vote_or_template(slide):
    """Detect voting / template slides by content cues."""
    for sh in slide.Shapes:
        try:
            if not sh.HasTextFrame or not sh.TextFrame.HasText:
                continue
            t = sh.TextFrame.TextRange.Text or ""
            t_lower = t.lower()
            # Common voting-slide markers
            if "rösta nu" in t_lower or "vote now" in t_lower:
                return True
            # "X — Hot eller hopp?" pattern
            if "hot eller hopp" in t_lower:
                return True
        except pythoncom.com_error:
            continue
    return False


def slide_metrics(slide):
    """Return (title, body_word_count, min_body_font_pt, body_sizes)."""
    title = ""
    body_words = 0
    min_size = None
    sizes = []

    for sh in slide.Shapes:
        try:
            if not sh.HasTextFrame or not sh.TextFrame.HasText:
                continue
            t = (sh.TextFrame.TextRange.Text or "").strip()
            if not t:
                continue
            try:
                sz = float(sh.TextFrame.TextRange.Font.Size)
            except Exception:
                sz = None

            # First text shape is treated as the title (don't count its words or size)
            if not title:
                title = t.replace("\r", " | ").replace("\n", " | ")[:60]
                continue

            words = len([w for w in t.split() if w not in ("|", "—", "-", "·")])
            body_words += words
            if sz:
                sizes.append(sz)
                if min_size is None or sz < min_size:
                    min_size = sz
        except pythoncom.com_error:
            continue

    return title, body_words, min_size or 0.0, sizes


def status_for(slide_num, title, body_words, min_size,
               skip_numbers, skip_patterns):
    """Classify a slide's status."""
    if slide_num in skip_numbers:
        return "[skip]"

    title_lower = (title or "").lower()
    for pat in skip_patterns:
        if pat.lower() in title_lower:
            return f"[skip-{pat[:8]}]"

    over_budget = body_words > 12
    below_floor = 0 < min_size < BODY_FLOOR

    if over_budget or below_floor:
        return "** AUDIT"
    if body_words > 8:
        return "ok-tight"
    return "OK"


def audit(pres, skip_numbers, skip_patterns):
    """Produce audit records for every slide."""
    results = []
    for i in range(1, pres.Slides.Count + 1):
        s = pres.Slides(i)
        title, body_words, min_size, sizes = slide_metrics(s)
        # Voting / template slides — detect after metrics
        if status_for(i, title, body_words, min_size,
                      skip_numbers, skip_patterns) in ("[skip]",) or \
           any(p.lower() in (title or "").lower() for p in skip_patterns):
            status = status_for(i, title, body_words, min_size,
                                skip_numbers, skip_patterns)
        elif is_vote_or_template(s):
            status = "[vote]"
        else:
            status = status_for(i, title, body_words, min_size,
                                skip_numbers, skip_patterns)
        results.append({
            "slide": i,
            "title": title,
            "body_words": body_words,
            "min_font_pt": round(min_size, 1),
            "status": status,
        })
    return results


def print_table(results, show_all=False):
    """Print a tab-aligned report. Skipped slides hidden unless show_all."""
    print(f"{'#':>3} {'words':>5} {'min_pt':>7}  {'status':<12} title")
    print("-" * 100)
    for r in results:
        if not show_all and r["status"].startswith("[skip"):
            continue
        title = r["title"] or "(no title)"
        print(f"{r['slide']:>3} {r['body_words']:>5} {r['min_font_pt']:>5.0f}pt"
              f"   {r['status']:<12} {title[:65]}")

    # Summary
    total = len(results)
    audit_n = sum(1 for r in results if r["status"] == "** AUDIT")
    tight_n = sum(1 for r in results if r["status"] == "ok-tight")
    ok_n = sum(1 for r in results if r["status"] == "OK")
    skip_n = sum(1 for r in results if r["status"].startswith("[skip"))
    vote_n = sum(1 for r in results if r["status"] == "[vote]")
    print("-" * 100)
    print(f"  {total} slides total: {audit_n} need audit, {tight_n} tight, "
          f"{ok_n} ok, {vote_n} vote, {skip_n} skipped")


def main():
    p = argparse.ArgumentParser(
        description="Audit a PowerPoint deck for word-budget and font-floor defects.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--file", help="Path to .pptx (default: active presentation)")
    p.add_argument("--skip", default="",
                   help="Comma-separated extra title-substrings to skip "
                        "(beyond defaults like HOPP, HOT Svar)")
    p.add_argument("--all", action="store_true",
                   help="Include skipped slides in output")
    p.add_argument("--json", action="store_true",
                   help="Output JSON instead of table")
    args = p.parse_args()

    skip_patterns = list(DEFAULT_SKIP_PATTERNS)
    if args.skip:
        skip_patterns.extend(s.strip() for s in args.skip.split(",") if s.strip())

    pythoncom.CoInitialize()
    try:
        try:
            app = win32com.client.GetActiveObject("PowerPoint.Application")
        except pythoncom.com_error:
            app = win32com.client.Dispatch("PowerPoint.Application")
            app.Visible = True

        if args.file:
            # Normalize path separators for substring matching
            file_norm = args.file.replace("/", "\\").lower()
            file_basename = file_norm.split("\\")[-1]
            target = None
            for pr in app.Presentations:
                pr_full = pr.FullName.replace("/", "\\").lower()
                if file_norm in pr_full or file_basename == pr.Name.lower():
                    target = pr
                    break
            if target is None:
                target = app.Presentations.Open(args.file, ReadOnly=True,
                                                WithWindow=False)
        else:
            target = app.ActivePresentation

        results = audit(target, DEFAULT_SKIP_NUMBERS, skip_patterns)

        if args.json:
            print(json.dumps(results, indent=2, ensure_ascii=False))
        else:
            print_table(results, show_all=args.all)
    finally:
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    main()
