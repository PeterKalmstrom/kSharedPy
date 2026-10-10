"""Review or restore-cuts on an EDL.

Two modes:

1. **Review mode** (default): render a markdown summary of the EDL. For each
   proposed cut, print transcript context (before / inside / after) so you
   can decide whether the cut is correct before running apply.py.

2. **Restore mode** (`--restore CATEGORY`): write a NEW EDL with all cuts in
   the given category flipped back to `keep: true`. Useful for wholesale
   rejection of one cut class (e.g. when you don't trust LLM retakes for a
   particular recording).

Run with uv or plain python (no extra deps — only stdlib + the EDL/transcript JSONs):

    # Review
    uv run review.py --edl edl.json --transcript transcript.json --out review.md

    # Restore: write a cleaned EDL with all retake cuts un-cut
    uv run review.py --edl edl.json --restore retake --out edl.cleaned.json

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
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
import argparse
import json
import sys
from pathlib import Path
from typing import Any

from _transcript import kTranscript
from _vedit_data import kVeditData
from kShared import ToolInputException, kRun, kS, kToolException


class kEdlReview:
    """Markdown review of an EDL's cuts with transcript context, and the restore of one cut class."""

    @staticmethod
    def FormatTime(Seconds: float) -> str:
        """Seconds as MM:SS.ss."""
        if kS.ErrorMode:
            return ""
        try:
            Minutes, Rest = divmod(Seconds, 60)
            return f"{int(Minutes):02d}:{Rest:05.2f}"
        except Exception as e:
            kS.GlobalErrorHandler(e, "kEdlReview.FormatTime")
            return ""

    @staticmethod
    def Categorize(Reason: str) -> str:
        """The cut class of a reason text: retake, filler, static, silence or other."""
        if kS.ErrorMode:
            return "other"
        try:
            if "retake" in Reason:
                return "retake"
            if "filler" in Reason:
                return "filler"
            if "static" in Reason:
                return "static"
            if "silence" in Reason:
                return "silence"
            return "other"
        except Exception as e:
            kS.GlobalErrorHandler(e, "kEdlReview.Categorize")
            return "other"

    @staticmethod
    def CountSortKey(Item: tuple[str, int]) -> int:
        """Sort key for (category, count) pairs: the largest count first."""
        if kS.ErrorMode:
            return 0
        try:
            return -Item[1]
        except Exception as e:
            kS.GlobalErrorHandler(e, "kEdlReview.CountSortKey")
            return 0

    @staticmethod
    def Render(Edl: dict[str, Any], Transcript: dict[str, Any], Only: str | None, ContextSeconds: float,
               MaxLine: int) -> str:
        """The markdown review."""
        if kS.ErrorMode:
            return ""
        try:
            Segments = Edl["segments"]
            Cuts = [Segment for Segment in Segments if not Segment["keep"]]
            Keeps = [Segment for Segment in Segments if Segment["keep"]]
            Total = float(Edl["duration_s"])
            CutDuration = sum(Segment["end"] - Segment["start"] for Segment in Cuts)
            KeepDuration = sum(Segment["end"] - Segment["start"] for Segment in Keeps)
            ByCategory: dict[str, int] = {}
            for Cut in Cuts:
                Category = kEdlReview.Categorize(Cut["reason"])
                ByCategory[Category] = ByCategory.get(Category, 0) + 1
            Out = []
            Out.append(f"# EDL review: {Path(Edl['source']).name}")
            Out.append("")
            Out.append(f"- **Source:** `{Edl['source']}`")
            Out.append(f"- **Duration:** {kEdlReview.FormatTime(Total)} → {kEdlReview.FormatTime(KeepDuration)} "
                       f"(cut {CutDuration:.1f}s, {100 * CutDuration / Total:.1f}%)")
            Out.append(f"- **{len(Cuts)} cuts** by category: " +
                       ", ".join(f"{Count} {Name}" for Name, Count
                                 in sorted(ByCategory.items(), key=kEdlReview.CountSortKey)))
            if Only:
                Out.append(f"- **Filtered to:** `{Only}` only")
            Out.append("")
            TranscriptSegments = Transcript["segments"]
            Shown = 0
            for Number, Cut in enumerate(Cuts, 1):
                Category = kEdlReview.Categorize(Cut["reason"])
                if Only and Only not in Category:
                    continue
                Shown += 1
                Start, End = Cut["start"], Cut["end"]
                CutText = kTranscript.WordsInside(TranscriptSegments, Start, End)
                # "before" = segments that end at or before cut start (no overlap with cut)
                Before = [Segment for Segment in TranscriptSegments
                          if Segment["end"] <= Start + 0.05 and Segment["end"] > Start - ContextSeconds]
                # "after"  = segments that start at or after cut end (no overlap with cut)
                After = [Segment for Segment in TranscriptSegments
                         if Segment["start"] >= End - 0.05 and Segment["start"] < End + ContextSeconds]
                Out.append(f"## Cut #{Number} — {kEdlReview.FormatTime(Start)} → {kEdlReview.FormatTime(End)} "
                           f"({End-Start:.1f}s)")
                Out.append(f"**Reason:** {Cut['reason']}")
                Out.append("")
                Out.append("```")
                for Segment in Before[-3:]:
                    Out.append(f"  [{Segment['start']:6.1f}] {Segment['text']}"[:MaxLine])
                if CutText:
                    Out.append(f"✂ [{Start:6.1f}] {CutText}    ← CUT"[:MaxLine])
                else:
                    Out.append(f"✂ [{Start:6.1f}] (silence — no transcript)    ← CUT ({End-Start:.1f}s)")
                for Segment in After[:3]:
                    Out.append(f"  [{Segment['start']:6.1f}] {Segment['text']}"[:MaxLine])
                Out.append("```")
                Out.append("")
            if Only and Shown == 0:
                Out.append(f"*(no cuts matched filter `{Only}`)*")
            Out.append("---")
            Out.append("")
            Out.append("To restore a cut, edit the EDL JSON and flip its `keep` from `false` to `true`.")
            Out.append("Then re-run `apply.py`. The EDL is the source of truth — `review.py` is read-only.")
            return "\n".join(Out) + "\n"
        except Exception as e:
            kS.GlobalErrorHandler(e, "kEdlReview.Render")
            return ""

    @staticmethod
    def Restore(Edl: dict[str, Any], Category: str) -> tuple[dict[str, Any] | None, int]:
        """Return (new_edl, restored_count) with cuts whose reason contains Category flipped to keep:true.
        Adjacent kept segments are then merged so the EDL stays clean."""
        if kS.ErrorMode:
            return (None, 0)
        try:
            Restored = 0
            Flipped = []
            for Segment in Edl["segments"]:
                if not Segment["keep"] and Category in kEdlReview.Categorize(Segment["reason"]):
                    Flipped.append({**Segment, "keep": True, "reason": f"restored from: {Segment['reason']}"})
                    Restored += 1
                else:
                    Flipped.append(dict(Segment))
            Merged: list[dict[str, Any]] = []
            for Segment in Flipped:
                if Merged and Merged[-1]["keep"] and Segment["keep"]:
                    Merged[-1] = {
                        "start": Merged[-1]["start"],
                        "end": Segment["end"],
                        "keep": True,
                        "reason": "" if not Merged[-1].get("reason") and not Segment.get("reason") else
                                  (Merged[-1].get("reason", "") or Segment.get("reason", "")),
                    }
                else:
                    Merged.append(Segment)
            return ({**Edl, "segments": Merged}, Restored)
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kEdlReview.Restore(category={Category})")
            return (None, 0)


class kReviewApp:
    """Command line."""

    @staticmethod
    def WriteOrPrint(OutPath: str | None, Text: str) -> bool:
        """Write Text to OutPath (creating its folder); without a path print it. True when written to a file."""
        if kS.ErrorMode:
            return False
        try:
            if OutPath:
                Path(OutPath).parent.mkdir(parents=True, exist_ok=True)
                Path(OutPath).write_text(Text, encoding="utf-8")
                return True
            sys.stdout.write(Text)
            return False
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kReviewApp.WriteOrPrint(file={OutPath})")
            return False

    def Run(self) -> int:
        if kS.ErrorMode:
            return 1
        try:
            Parser = argparse.ArgumentParser(description="Review or restore-cuts on an EDL.")
            Parser.add_argument("--edl", required=True)
            Parser.add_argument("--transcript", default=None,
                                help="Transcript JSON path. Required for review mode.")
            Parser.add_argument("--out", default=None)
            Parser.add_argument("--only", default=None,
                                help="(review) Filter to category: retake | silence | static")
            Parser.add_argument("--restore", default=None,
                                help="(restore) Flip cuts in CATEGORY to keep:true, write new EDL.")
            Parser.add_argument("--context-s", type=float, default=3.0)
            Parser.add_argument("--max-line", type=int, default=100)
            Args = Parser.parse_args()

            Edl = kVeditData.LoadEdl(Args.edl)
            if kS.ErrorMode:
                return 1

            if Args.restore:
                NewEdl, Restored = kEdlReview.Restore(Edl, Args.restore)
                if kS.ErrorMode:
                    return 1
                if kReviewApp.WriteOrPrint(Args.out, json.dumps(NewEdl, ensure_ascii=False, indent=2)):
                    sys.stderr.write(f"[vedit/review] restored {Restored} '{Args.restore}' cuts → {Args.out}\n")
                return 0

            if not Args.transcript:
                raise ToolInputException("--transcript required for review mode")
            Transcript = kVeditData.LoadTranscript(Args.transcript)
            if kS.ErrorMode:
                return 1
            Markdown = kEdlReview.Render(Edl, Transcript, Args.only, Args.context_s, Args.max_line)
            if kS.ErrorMode:
                return 1
            if kReviewApp.WriteOrPrint(Args.out, Markdown):
                sys.stderr.write(f"[vedit/review] wrote {Args.out}\n")
            return 0
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, "kReviewApp.Run")
            return 1


if __name__ == "__main__":
    kRun.Main(kReviewApp)
