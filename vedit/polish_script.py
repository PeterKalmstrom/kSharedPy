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

from __future__ import annotations

import argparse
import io
import os
import sys
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)


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


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM-polish a reading script.")
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--voice", default=(
        "Peter Kalmström — casual tutorial first-person, "
        "occasional 'so' / 'now' / 'let's' transitions, "
        "instructional but warm"
    ))
    args = ap.parse_args()

    inp = Path(args.inp)
    out = Path(args.out)
    if not inp.exists():
        sys.stderr.write(f"ERROR: input not found: {inp}\n"); return 2

    if "GEMINI_API_KEY" not in os.environ and "GOOGLE_API_KEY" not in os.environ:
        sys.stderr.write(
            "ERROR: GEMINI_API_KEY (or GOOGLE_API_KEY) not set.\n"
        ); return 2
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        sys.stderr.write(
            "ERROR: google-genai not installed.\n"
            "Run via: uvx --with google-genai python polish_script.py ...\n"
        ); return 2

    text = inp.read_text(encoding="utf-8")
    prompt = PROMPT.format(voice=args.voice, text=text)

    sys.stderr.write(f"[vedit/polish_script] polishing {len(text):,} chars via {args.model}…\n")

    client = genai.Client()
    response = client.models.generate_content(
        model=args.model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.2,
        ),
    )
    polished = (response.text or "").strip()
    if not polished:
        sys.stderr.write("ERROR: empty response from Gemini.\n"); return 2

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(polished + "\n", encoding="utf-8")
    sys.stderr.write(
        f"[vedit/polish_script] {len(text):,} → {len(polished):,} chars → {out}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
