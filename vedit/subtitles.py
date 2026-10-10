"""Emit .srt or .vtt subtitles from a transcript JSON.

If --edl is provided, timestamps are re-mapped to the post-cut timeline so
the subtitle file aligns with the edited video produced by apply.py.

**Readability rule** (default behavior): cue texts are polished via Gemini
LLM so they read smoothly. Verbal restarts, filler ("you know"), and grammar
artifacts from speech are removed. The polished cues are NOT word-for-word
faithful to the audio, by design — viewers reading subtitles want clean
sentences, not a literal transcript. Disable with --no-polish if you need
a verbatim transcript-style .srt.

Run with uv (the script header declares google-genai for the default polish):

    # Polished subtitles (default; needs GEMINI_API_KEY)
    uv run subtitles.py \\
        --transcript transcript.json --edl edl.json --out edited.srt

    # Raw verbatim transcript (no LLM call)
    uv run subtitles.py --no-polish \\
        --transcript transcript.json --edl edl.json --out edited.srt

    (without inline-metadata support: uvx --with google-genai python subtitles.py ...)

Flags:
    --transcript PATH  Transcript JSON (output of transcribe.py). Required.
    --edl PATH         EDL JSON. Optional. When given, timestamps are
                       re-mapped to the cut timeline.
    --out PATH         Output file. Format inferred from extension
                       (.srt or .vtt). Required.
    --format FMT       Override format: srt | vtt. Default: from extension.
    --max-line LEN     Soft-wrap subtitle text at this character length.
                       Default: 80.
    --no-polish        Skip the LLM polish pass and emit verbatim transcript.
    --polish-model M   Gemini model for polish. Default: gemini-2.5-flash.
"""
# /// script
# requires-python = ">=3.10"
# dependencies = ["google-genai"]
# ///

import argparse
import re
import sys
from pathlib import Path
from typing import Any

from kFfmpeg import kDependency
from kGenAi import kGenAi
from _vedit_data import kVeditData
from kShared import ToolInputException, kRun, kS, kToolException


class kCutTimeline:
    """Moves transcript cues onto the timeline of the cut video."""

    @staticmethod
    def Remap(TranscriptSegments: list[dict[str, Any]],
              KeepRanges: list[tuple[float, float]]) -> list[dict[str, Any]]:
        """Re-map transcript segments to a timeline where everything outside
        KeepRanges has been removed and kept ranges are concatenated.

        Each input segment gets clipped to each kept range it overlaps. The
        text is clipped too — only the WORDS whose midpoint falls inside the
        kept portion are emitted. This prevents duplicate text when a segment
        straddles a cut boundary (which is common after word-snap).
        """
        if kS.ErrorMode:
            return []
        try:
            Out: list[dict[str, Any]] = []
            Cumulative = 0.0  # post-cut time at which the current keep range starts
            for KeepStart, KeepEnd in KeepRanges:
                for Segment in TranscriptSegments:
                    if Segment["end"] <= KeepStart or Segment["start"] >= KeepEnd:
                        continue
                    ClippedStart = max(Segment["start"], KeepStart)
                    ClippedEnd = min(Segment["end"], KeepEnd)
                    if ClippedEnd <= ClippedStart + 0.01:
                        continue
                    # Pick words by midpoint inside [ClippedStart, ClippedEnd].
                    Words = Segment.get("words") or []
                    if Words:
                        Kept = [Word for Word in Words
                                if ClippedStart <= (Word["start"] + Word["end"]) / 2 <= ClippedEnd]
                        if not Kept:
                            continue
                        Text = "".join(Word["word"] for Word in Kept).strip()
                        # Use the kept words' tight time bounds for better cue alignment
                        WordStart = max(ClippedStart, Kept[0]["start"])
                        WordEnd = min(ClippedEnd, Kept[-1]["end"])
                    else:
                        Text = Segment["text"]
                        WordStart = ClippedStart
                        WordEnd = ClippedEnd
                    if not Text:
                        continue
                    Out.append({"start": Cumulative + (WordStart - KeepStart),
                                "end": Cumulative + (WordEnd - KeepStart), "text": Text})
                Cumulative += KeepEnd - KeepStart
            return Out
        except Exception as e:
            kS.GlobalErrorHandler(e, "kCutTimeline.Remap")
            return []


POLISH_PROMPT = """You are cleaning up the subtitle text for a screen-recording tutorial. Each input line is one subtitle cue, marked with [N] where N is the cue number. Output the same cues with the same numbers, but with the TEXT polished for readability.

# Rules

1. Output EVERY cue. Same numbers. Same count. Do not merge cues, do not skip cues, do not insert cues.
2. Polish each cue's text:
   - Remove verbal restarts and stutters ("So I told it, after that I told it" → "So I told it")
   - Remove filler ("you know", "actually", "I mean", "or something like that")
   - Fix grammar artifacts from spoken English
3. Readability is more important than literal accuracy. You may slightly rewrite to make a cue read well on its own.
4. Keep the casual first-person tutorial voice. Do NOT formalize the tone.
5. Keep proper nouns and product names EXACT: Claude, Claude Code, CLAUDE.md, SKILL.md, Visual Studio Code, Azure, Peter Kalmström (note: Kalmström, NOT "Kammstrom" — fix this if it appears).
6. Keep each cue's text length similar to the input. If the speaker's words are already clean, return them unchanged.
7. If a sentence spans multiple cues, polish coherently across them — but keep the cue boundaries.

# Output format (STRICT)

[1] <polished text for cue 1>
[2] <polished text for cue 2>
[3] <polished text for cue 3>
...

No preamble, no explanation, no markdown. Just the [N] lines.

# Input cues

{cues}
"""


class kCuePolisher:
    """Gemini polish of the cue texts. Cue boundaries and timing are preserved."""

    CUE_LINE = re.compile(r"^\[(\d+)\]\s*(.*)$")

    @staticmethod
    def Polish(Cues: list[dict[str, Any]], Model: str) -> list[dict[str, Any]]:
        """Polish the text of each cue via Gemini.

        A missing key, a missing google-genai package or a failed API call leaves the input cues unchanged with
        a warning - the .srt is still produced (the raw transcript is a complete, correct fallback)."""
        if kS.ErrorMode:
            return Cues
        try:
            if not Cues:
                return Cues
            if not kGenAi.HasKey():
                sys.stderr.write(
                    "[vedit/subtitles] WARNING: GEMINI_API_KEY not set; skipping polish "
                    "(raw transcript will be used). Pass --no-polish to suppress this warning.\n"
                )
                return Cues
            if not kDependency.Has("google.genai"):
                sys.stderr.write(
                    "[vedit/subtitles] WARNING: google-genai not installed; skipping polish.\n"
                    "  Run with: uv run subtitles.py ... (the script declares google-genai)\n"
                )
                return Cues
            genai, types, _Errors = kGenAi.Load("subtitles.py")
            Numbered = "\n".join(f"[{Index+1}] {Cue['text']}" for Index, Cue in enumerate(Cues))
            Prompt = POLISH_PROMPT.format(cues=Numbered)
            sys.stderr.write(
                f"[vedit/subtitles] polishing {len(Cues)} cues via {Model} "
                f"({len(Numbered):,} chars)…\n"
            )
            try:
                Client = genai.Client()
                Response = Client.models.generate_content(
                    model=Model,
                    contents=Prompt,
                    config=types.GenerateContentConfig(temperature=0.2),
                )
            except Exception as Failure:  # ERROR-SUPPRESSED-JUSTIFIED: polish is optional - the raw cues are a correct result and the warning says so
                sys.stderr.write(
                    f"[vedit/subtitles] WARNING: Gemini polish failed ({Failure}); using raw text.\n"
                )
                return Cues
            PolishedByIndex: dict[int, str] = {}
            for Line in (Response.text or "").splitlines():
                Match = kCuePolisher.CUE_LINE.match(Line.strip())
                if not Match:
                    continue
                PolishedByIndex[int(Match.group(1))] = Match.group(2).strip()
            Out: list[dict[str, Any]] = []
            Missing = 0
            for Index, Cue in enumerate(Cues):
                PolishedText = PolishedByIndex.get(Index + 1)
                if PolishedText:
                    Out.append({**Cue, "text": PolishedText})
                else:
                    Missing += 1
                    Out.append(Cue)
            if Missing:
                sys.stderr.write(
                    f"[vedit/subtitles] WARNING: {Missing}/{len(Cues)} cues missing from "
                    f"polish response; left raw.\n"
                )
            return Out
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kCuePolisher.Polish(model={Model})")
            return Cues


class kSubtitleWriter:
    """Formats cues as SRT or WebVTT text."""

    @staticmethod
    def SrtTime(Seconds: float) -> str:
        """Seconds as an SRT timestamp HH:MM:SS,mmm."""
        if kS.ErrorMode:
            return "00:00:00,000"
        try:
            Hours = int(Seconds // 3600)
            Seconds -= Hours * 3600
            Minutes = int(Seconds // 60)
            Seconds -= Minutes * 60
            Whole = int(Seconds)
            Millis = int(round((Seconds - Whole) * 1000))
            if Millis == 1000:
                Whole += 1
                Millis = 0
            return f"{Hours:02d}:{Minutes:02d}:{Whole:02d},{Millis:03d}"
        except Exception as e:
            kS.GlobalErrorHandler(e, "kSubtitleWriter.SrtTime")
            return "00:00:00,000"

    @staticmethod
    def VttTime(Seconds: float) -> str:
        """Seconds as a WebVTT timestamp HH:MM:SS.mmm."""
        if kS.ErrorMode:
            return "00:00:00.000"
        try:
            return kSubtitleWriter.SrtTime(Seconds).replace(",", ".")
        except Exception as e:
            kS.GlobalErrorHandler(e, "kSubtitleWriter.VttTime")
            return "00:00:00.000"

    @staticmethod
    def Wrap(Text: str, MaxLine: int) -> str:
        """Soft-wrap Text at MaxLine characters on word boundaries."""
        if kS.ErrorMode:
            return Text
        try:
            Lines = []
            Current = ""
            for Word in Text.split():
                if Current and len(Current) + 1 + len(Word) > MaxLine:
                    Lines.append(Current)
                    Current = Word
                else:
                    Current = (Current + " " + Word) if Current else Word
            if Current:
                Lines.append(Current)
            return "\n".join(Lines)
        except Exception as e:
            kS.GlobalErrorHandler(e, "kSubtitleWriter.Wrap")
            return Text

    @staticmethod
    def ToSrt(Segments: list[dict[str, Any]], MaxLine: int) -> str:
        """The cues as SRT text."""
        if kS.ErrorMode:
            return ""
        try:
            Out = []
            for Index, Segment in enumerate(Segments, 1):
                Out.append(str(Index))
                Out.append(f"{kSubtitleWriter.SrtTime(Segment['start'])} --> "
                           f"{kSubtitleWriter.SrtTime(Segment['end'])}")
                Out.append(kSubtitleWriter.Wrap(Segment["text"].strip(), MaxLine))
                Out.append("")
            return "\n".join(Out)
        except Exception as e:
            kS.GlobalErrorHandler(e, "kSubtitleWriter.ToSrt")
            return ""

    @staticmethod
    def ToVtt(Segments: list[dict[str, Any]], MaxLine: int) -> str:
        """The cues as WebVTT text."""
        if kS.ErrorMode:
            return ""
        try:
            Out = ["WEBVTT", ""]
            for Segment in Segments:
                Out.append(f"{kSubtitleWriter.VttTime(Segment['start'])} --> "
                           f"{kSubtitleWriter.VttTime(Segment['end'])}")
                Out.append(kSubtitleWriter.Wrap(Segment["text"].strip(), MaxLine))
                Out.append("")
            return "\n".join(Out)
        except Exception as e:
            kS.GlobalErrorHandler(e, "kSubtitleWriter.ToVtt")
            return ""


class kSubtitlesApp:
    """Command line."""

    def Run(self) -> int:
        if kS.ErrorMode:
            return 1
        try:
            Parser = argparse.ArgumentParser(description="Emit .srt/.vtt from transcript ± EDL.")
            Parser.add_argument("--transcript", required=True)
            Parser.add_argument("--edl", default=None)
            Parser.add_argument("--out", required=True)
            Parser.add_argument("--format", default=None, choices=["srt", "vtt"])
            Parser.add_argument("--max-line", type=int, default=80)
            Parser.add_argument("--no-polish", action="store_true",
                                help="Skip the LLM polish pass (default: polish on).")
            Parser.add_argument("--polish-model", default="gemini-2.5-flash")
            Args = Parser.parse_args()

            TranscriptPath = Path(Args.transcript)
            if not TranscriptPath.exists():
                raise ToolInputException(f"transcript not found: {TranscriptPath}")
            EdlPath = Path(Args.edl) if Args.edl else None
            if EdlPath is not None and not EdlPath.exists():
                raise ToolInputException(f"EDL not found: {EdlPath}")
            OutPath = Path(Args.out)
            Format = Args.format or OutPath.suffix.lstrip(".").lower()
            if Format not in ("srt", "vtt"):  # checked before the paid polish call, not after it
                raise ToolInputException(f"unsupported format '{Format}' "
                                         f"(use --format srt|vtt or .srt/.vtt extension)")
            Transcript = kVeditData.LoadTranscript(TranscriptPath)
            if kS.ErrorMode:
                return 1
            Segments = Transcript["segments"]
            if EdlPath is not None:
                Edl = kVeditData.LoadEdl(EdlPath)
                if kS.ErrorMode:
                    return 1
                KeepRanges = [(float(Segment["start"]), float(Segment["end"]))
                              for Segment in Edl["segments"] if Segment["keep"]]
                Segments = kCutTimeline.Remap(Segments, KeepRanges)
            if not Args.no_polish:
                Segments = kCuePolisher.Polish(Segments, Args.polish_model)
            if Format == "srt":
                Text = kSubtitleWriter.ToSrt(Segments, Args.max_line)
            else:
                Text = kSubtitleWriter.ToVtt(Segments, Args.max_line)
            if kS.ErrorMode:
                return 1
            OutPath.parent.mkdir(parents=True, exist_ok=True)
            OutPath.write_text(Text, encoding="utf-8")
            sys.stderr.write(f"[vedit/subtitles] wrote {len(Segments)} cues "
                             f"({'cut-timeline' if Args.edl else 'source-timeline'}) → {OutPath}\n")
            return 0
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, "kSubtitlesApp.Run")
            return 1


if __name__ == "__main__":
    kRun.Main(kSubtitlesApp)
