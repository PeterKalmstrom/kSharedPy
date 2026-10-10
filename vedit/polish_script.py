"""LLM-polish a reading script: smooth out spoken-language artifacts while
preserving meaning. Designed to make re-recording from an edited transcript
practical — natural speech contains restarts, filler, and grammar artifacts
that don't read aloud well.

Run via uvx (needs Gemini key):

    uvx --with google-genai python polish_script.py \\
        --in script.txt --out script.polished.txt

Flags:
    --in PATH        Input text file. Required.
    --out PATH       Output text file. Required.
    --model NAME     Gemini model. Default: gemini-2.5-flash.
    --voice NAME     Voice/style hint sent to the model. Default: "Peter Kalmström —
                     casual tutorial first-person, occasional 'so' / 'now' / 'let's'
                     transitions, instructional but warm".

The model is told to PRESERVE every fact, step, and proper noun. It removes
verbal restarts, filler ("you know", "actually"), redundant phrasing, and
fixes mid-thought grammar slips — but should not add content, change meaning,
or formalize the voice.
"""

import argparse
import sys
from pathlib import Path

from kGenAi import kGenAi
from kShared import ToolInputException, ToolReportableException, kRun, kS, kToolException


PROMPT = """You are cleaning up a transcript of a software tutorial recorded in one take. The speaker spoke conversationally and made the usual verbal mistakes — restarts, filler, mid-thought changes. Your job is to produce a clean reading script the speaker can re-record from.

# Rules

1. **Preserve every fact, step, and concept.** Do not add new content. Do not invent steps. Do not change which file/tool/command is mentioned. If you're not certain about a fact, keep the speaker's words.
2. **Preserve the voice.** This is {voice}. Keep it conversational, first-person, informal. Do NOT formalize. Do NOT translate "I'm going to" into "I shall". Do NOT add corporate phrasing.
3. **Remove verbal restarts.** "So I told it, after that I told it, save the memory" → "So I told it to save the memory".
4. **Remove filler.** "you know", "I mean", "actually", "or something like that", "or whatever" — these go unless they're load-bearing.
5. **Fix mid-thought grammar.** Run-on sentences from speech should become readable sentences. Don't change which idea precedes which; just make the transitions clean.
6. **Keep proper nouns and product names EXACT.** "Claude", "Claude Code", "CLAUDE.md", "SKILL.md", "Visual Studio Code", "Azure", and "Peter Kalmström" (note: Kalmström, NOT "Kammstrom" or "Kamstrom" — fix this if it appears).
7. **Preserve paragraph breaks** where the input has them. Each input paragraph is one output paragraph.
8. **Match length approximately.** The output should be similar in length to the input — this is polishing, not summarization.

# Output

Return ONLY the cleaned script text. No preamble, no explanation, no markdown formatting, no quotes around the result.

# Transcript to clean

{text}
"""


class kScriptPolisher:
    """One Gemini polish of one reading script."""

    @staticmethod
    def Polish(Text, Model, Voice):
        """The polished text; an empty answer from Gemini stops the tool (exit 2)."""
        if kS.ErrorMode:
            return ""
        try:
            genai, types, _Errors = kGenAi.Load("polish_script.py")
            Prompt = PROMPT.format(voice=Voice, text=Text)
            sys.stderr.write(f"[vedit/polish_script] polishing {len(Text):,} chars via {Model}…\n")
            Client = genai.Client()
            Response = Client.models.generate_content(
                model=Model,
                contents=Prompt,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                ),
            )
            Polished = (Response.text or "").strip()
            if not Polished:
                raise ToolReportableException("empty response from Gemini.", 2)
            return Polished
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kScriptPolisher.Polish(model={Model})")
            return ""


class kPolishScriptApp:
    """Command line."""

    def Run(self):
        if kS.ErrorMode:
            return 1
        try:
            Parser = argparse.ArgumentParser(description="LLM-polish a reading script.")
            Parser.add_argument("--in", dest="inp", required=True)
            Parser.add_argument("--out", required=True)
            Parser.add_argument("--model", default="gemini-2.5-flash")
            Parser.add_argument("--voice", default=(
                "Peter Kalmström — casual tutorial first-person, "
                "occasional 'so' / 'now' / 'let's' transitions, "
                "instructional but warm"
            ))
            Args = Parser.parse_args()

            Input = Path(Args.inp)
            Out = Path(Args.out)
            if not Input.exists():
                raise ToolInputException(f"input not found: {Input}")
            kGenAi.RequireKey()  # exit 2; a missing google-genai is kGenAi.Load's (exit 1)
            Text = Input.read_text(encoding="utf-8")
            Polished = kScriptPolisher.Polish(Text, Args.model, Args.voice)
            if kS.ErrorMode:
                return 1
            Out.parent.mkdir(parents=True, exist_ok=True)
            Out.write_text(Polished + "\n", encoding="utf-8")
            sys.stderr.write(
                f"[vedit/polish_script] {len(Text):,} → {len(Polished):,} chars → {Out}\n"
            )
            return 0
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, "kPolishScriptApp.Run")
            return 1


if __name__ == "__main__":
    kRun.Main(kPolishScriptApp)
