"""Convert an .srt subtitle file to a clean reading script.

Strips cue numbers and timestamps, leaving just the spoken text. Useful for
re-recording: the speaker reads back from the cleaned-up script and the
producer matches the new audio to the existing edit timeline (or just makes
a fresh recording from it).

Run with uv or plain python (stdlib only):

    uv run srt_to_script.py --in edited.srt --out script.txt

Flags:
    --in PATH           Input .srt file. Required.
    --out PATH          Output .txt file. Default: stdout.
    --paragraphs        Group cues into paragraphs at >=1.5s gaps in the source
                         timeline. Default: one line per cue.
    --width N           Wrap output text at N characters. Default: 80. Set 0
                         to disable wrapping.
    --quiet             No progress line on stderr after writing --out.

Output: without --out the script text is stdout; the status line after writing --out goes to stderr.
"""
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
import argparse
import re
import textwrap
from pathlib import Path
from typing import Any

from kConsole import kConsole
from kShared import ToolInputException, kRun, kS, kToolException

TIMESTAMP_PATTERN = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)


class kSrtParser:
    """Reads the cues of one .srt text: [{start, end, text}, ...]."""

    def __init__(self) -> None:
        try:
            self.Cues: list[dict[str, Any]] = []
            self.Current: dict[str, Any] | None = None
            self.Lines: list[str] = []
        except Exception as e:
            kS.GlobalErrorHandler(e, "kSrtParser.__init__")

    @staticmethod
    def ToSeconds(Hours: str, Minutes: str, Seconds: str, Milliseconds: str) -> float:
        """One SRT timestamp's parts as seconds."""
        if kS.ErrorMode:
            return 0.0
        try:
            return int(Hours) * 3600 + int(Minutes) * 60 + int(Seconds) + int(Milliseconds) / 1000
        except Exception as e:
            kS.GlobalErrorHandler(e, "kSrtParser.ToSeconds")
            return 0.0

    def Flush(self) -> None:
        """Close the current cue: keep it when it has text, then start over."""
        if kS.ErrorMode:
            return
        try:
            if self.Current is not None and self.Lines:
                self.Current["text"] = " ".join(Line.strip() for Line in self.Lines).strip()
                self.Cues.append(self.Current)
            self.Current = None
            self.Lines = []
        except Exception as e:
            kS.GlobalErrorHandler(e, "kSrtParser.Flush")
            return

    def Parse(self, Text: str) -> list[dict[str, Any]]:
        """Return the list of cues in Text."""
        if kS.ErrorMode:
            return []
        try:
            for Raw in Text.splitlines():
                Line = Raw.rstrip()
                if not Line:
                    self.Flush()
                    continue
                Match = TIMESTAMP_PATTERN.search(Line)
                if Match:
                    self.Current = {
                        "start": kSrtParser.ToSeconds(Match.group(1), Match.group(2), Match.group(3), Match.group(4)),
                        "end": kSrtParser.ToSeconds(Match.group(5), Match.group(6), Match.group(7), Match.group(8)),
                    }
                    self.Lines = []
                    continue
                if re.match(r"^\d+$", Line) and self.Current is None:
                    continue  # a cue number
                if self.Current is not None:
                    self.Lines.append(Line)
            self.Flush()
            return self.Cues
        except Exception as e:
            kS.GlobalErrorHandler(e, "kSrtParser.Parse")
            return []


class kScriptFormatter:
    """Turns cues into reading-script text."""

    @staticmethod
    def Format(Cues: list[dict[str, Any]], Paragraphs: bool, Width: int) -> str:
        """One line per cue, or paragraphs split at 1.5s+ gaps; wrapped at Width (0 = no wrap)."""
        if kS.ErrorMode:
            return ""
        try:
            if not Cues:
                return ""
            OutLines = []
            if Paragraphs:
                Paragraph = []
                for Index, Cue in enumerate(Cues):
                    Paragraph.append(Cue["text"])
                    GapToNext = (Cues[Index + 1]["start"] - Cue["end"]) if Index + 1 < len(Cues) else 999
                    if GapToNext >= 1.5:
                        OutLines.append(" ".join(Paragraph))
                        OutLines.append("")
                        Paragraph = []
                if Paragraph:
                    OutLines.append(" ".join(Paragraph))
            else:
                for Cue in Cues:
                    OutLines.append(Cue["text"])
            if Width > 0:
                Wrapped = []
                for Line in OutLines:
                    if Line:
                        Wrapped.extend(textwrap.wrap(
                            Line, width=Width, break_long_words=False, break_on_hyphens=False
                        ) or [""])
                    else:
                        Wrapped.append("")
                OutLines = Wrapped
            return "\n".join(OutLines) + "\n"
        except Exception as e:
            kS.GlobalErrorHandler(e, "kScriptFormatter.Format")
            return ""


class kSrtToScriptApp:
    """Command line."""

    def Run(self) -> int:
        if kS.ErrorMode:
            return 1
        try:
            Parser = argparse.ArgumentParser(description="Strip .srt to a clean reading script.")
            Parser.add_argument("--in", dest="inp", required=True)
            Parser.add_argument("--out", default=None)
            Parser.add_argument("--paragraphs", action="store_true",
                                help="Group cues into paragraphs at 1.5s+ gaps.")
            Parser.add_argument("--width", type=int, default=80, help="Wrap width. 0=off.")
            Parser.add_argument("--quiet", action="store_true")
            Args = Parser.parse_args()
            kConsole.Quiet = Args.quiet

            Input = Path(Args.inp)
            if not Input.exists():
                raise ToolInputException(f"input not found: {Input}")
            Cues = kSrtParser().Parse(Input.read_text(encoding="utf-8"))
            Text = kScriptFormatter.Format(Cues, Args.paragraphs, Args.width)
            if kS.ErrorMode:
                return 1
            if Args.out:
                Out = Path(Args.out)
                Out.parent.mkdir(parents=True, exist_ok=True)
                Out.write_text(Text, encoding="utf-8")
                kConsole.Progress(f"[vedit/srt_to_script] {len(Cues)} cues → {Out}")
            else:
                kConsole.Result(Text, End="")
            return 0
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, "kSrtToScriptApp.Run")
            return 1


if __name__ == "__main__":
    kRun.Main(kSrtToScriptApp)
