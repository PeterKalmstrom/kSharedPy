"""The Gemini passes of propose.py (sibling module, imported by the script): retake finder, filler trim and the
multimodal retake check. TranscriptText, ToSpans and ParseJson are pure and testable without the SDK.
"""
import json
import subprocess
from pathlib import Path
from typing import Any

from _transcript import kTranscript
from kConsole import kConsole
from kGenAi import kGenAi
from kShared import kS, kToolException


RETAKE_PROMPT = """You are reviewing a transcript of a screen-recorded software tutorial. The speaker is recording in one take. Sometimes they redo SPEECH (cut these); other times they redo ACTIONS or describe what's happening (keep these — that's content).

Your job: find ONLY the spans where the speaker is clearly redoing speech.

# What counts as a retake (CUT)

- Explicit verbal restarts: "wait, let me redo that" / "let me start over" / "scratch that"
- Speech-correction acknowledgements: "ugh, I meant X, not Y" / "no wait, that's wrong"
- Stuttered word repetitions: "the the server", "we we want to"
- Abandoned sentences the speaker explicitly walks back AND re-states differently

# What does NOT count (KEEP — these are content, not retakes)

- "let's try it again" / "let me try that again" — speaker is retrying a COMMAND on screen.
- "if I had to X, I would Y, but here Z" — hypothetical/counterfactual; comparison content.
- Self-deprecating asides: "if I can spell that", "bear with me" — speaker chose to say them.
- "uh" / "um" inside a fluent sentence.
- "no, it doesn't recognize that" — speaker reacting TO what's on screen.
- Partial sentences that get completed normally on the next breath.

# Calibration examples

GOOD CUTS (you SHOULD flag these):
- "we're going to deploy this. Wait — scratch that. Let me start over. We're going to deploy…"
  → Cut "Wait — scratch that. Let me start over." Reason: "scratch that, let me start over"
- "the the server is" → Cut the duplicated "the". Reason: "stuttered repetition"
- "ugh, I meant azd, not ac. azd login." → Cut "ugh, I meant azd, not ac." Reason: "ugh, I meant azd, not ac"

BAD CUTS (you must NOT flag these — past versions of you got these wrong):
- "So let's try it again, just copied and pasted that" → KEEP. Retrying the command, not the speech.
- "if I found a script that published my website online, then when that failed, I would have to start over. But here with Claude, I can just feed it the error" → KEEP ALL. Hypothetical comparison; "have to start over" refers to the SCRIPT failure scenario, not redoing speech.
- "log in, if I can spell that, most likely it won't recognize that" → KEEP. Self-aware aside; speaker chose to say it.
- "No, it didn't recognize the command" → KEEP. Reacting to the screen, not retracting speech.
- "So okay, so let's continue to do that" → KEEP. Transition phrase, not a restart.
- "but okay, it doesn't" → KEEP. Reacting; not a retake.
- "Cut that cut the way the whole that whole section" → KEEP. Speaker giving an INSTRUCTION to the AI tool ("Cut that section in that way"). A verb repeating because the speaker is working out HOW to phrase the command is NOT a retake — the command itself is real. Any hesitant instruction containing words like Cut / Add / Remove / Delete / Make followed by content about slides, code, or files is a command, not a retake.
- Any sentence where the speaker tells an AI to perform an action on the visible content → KEEP, even if the wording is hesitant or contains word-level repetition.

# Output format

Return a JSON array. **Empty array is correct and common** — on a typical 15-min tutorial, fewer than 5 real retakes are normal. If you flag more than that, you're probably wrong.

Each entry: start (seconds), end (seconds), reason (a short string quoting the speaker's actual trigger words — verbatim from the transcript).

When in doubt, KEEP. The cost of a false positive (user has to un-cut) exceeds the cost of a false negative (user hand-cuts an obvious retake we missed).

# Transcript

(each line is "[start-end] text")

{transcript}
"""


FILLER_PROMPT = """You are reviewing a transcript of a screen-recorded software tutorial. The speaker is talking conversationally and includes some FILLER/HEDGING/REDUNDANT phrases that an editor would trim to tighten the video — these are content cuts (different from speech retakes, which are handled by a separate pass).

# What to flag (CUT — small content trims)

- Self-deprecating asides: "if I can spell that", "bear with me", "hopefully"
- Hedging: "or something like that", "I think", "maybe", "more or less", "in 10 minutes or so"
- Throwaway sentences that don't add information:
    "Here's another security warning. Let's approve that also."
    "And now meanwhile here, this tool installation has found that"
- Hypothetical asides that don't pay off:
    "If I was writing a script or I found a script online that"
- Verbal real-estate filler that doesn't introduce new content:
    "And now meanwhile", "Let's see", "So okay, so let's continue to do that"
- Sentence-final filler / extra closings: "in 10 minutes or something like that"

# What to KEEP

- Substantive narration of what's happening on screen
- Commands, results, demonstrations
- Topic transitions listeners need to follow
- Punchlines and emphasis (even when conversational)
- Anything you're not confident is filler

# Critical: phrases that look like filler but ARE content — never cut these

- **Result-confirmation phrases**: "That's exactly what we want", "That's good", "That's it",
  "Yes, that's of course what we want" — when the speaker confirms a result visible on screen,
  the phrase has tutorial value even if conversational.  Keep result-confirmations.
- **Transition/launch phrases before a new visible action**: "Let's see if it works this time",
  "Let's go to...", "Now let's...", "There we go", "Let me...", "Let's try...", "All right" —
  these prime the viewer for the upcoming screen event.  Cutting them creates a jarring jump.
  Keep pre-action transitions.
- **Short affirmative phrase immediately before a SPEED or NORMAL segment**: "All right",
  "Okay", "So", "And", "But" — these serve as natural breath-pause cues.  Keep them.
- **Explanation phrases before an action**: "I just want to…", "I'll just…", "Let me just…",
  "I'm going to…" — these orient the viewer before the speaker performs the action.  Keep them.
- **End-of-list closers**: "Stuff like that", "and so on", "or whatever", "things like that" —
  these terminate a spoken list naturally; cutting them sounds abrupt.  Keep them.
- **Self-corrections that extend the thought**: if the speaker re-states with new information
  ("and other and other kinds of"), keep it — the correction adds content, not noise.
- **Demo narration in the second half of the recording**: after roughly the midpoint of a tutorial,
  the speaker is demonstrating a running system.  Treat every utterance as content — the viewer
  needs narration to follow what the AI / tool is doing.  Be extremely conservative with cuts after
  the midpoint.
- **Screen-pointing phrases**: "as you see", "as you can see", "you can see", "notice that",
  "look at", "here you can", "so and as you see also" — keep, they guide the viewer's attention.
- **Capability/feature narration**: "you can analyze", "it can", "this allows", "it will",
  "you can also" + a noun/verb — the speaker is describing what the tool does.  Keep it.
- **Timing expectations**: "maybe a minute", "in a few seconds", "it takes about", "up to a minute"
  — these set viewer expectations and are part of the tutorial value.  Keep them.
- **Instructional phrases about how to set things up**: "all set up", "set up here",
  "is set up" — context a viewer needs.  Keep them.
- **Speaker instructions to the AI tool**: utterances where the speaker tells the on-screen AI
  what to do ("Cut that", "Add a section", "Make this") — these are the core demo content
  even if they look like commands.  Do NOT cut them.
- **Viewer-instruction / workflow sentences**: "You would come back and check the results",
  "You can then...", "At this point you would...", "You would need to..." — these give the
  viewer workflow context.  Keep them.
- **Isolated short filler phrases next to content** ("of course", "all right", "so", "and") —
  if the phrase is ≤2 words and surrounded by kept content on both sides, keep it.  Cutting
  sub-2-word fillers creates jarring jump cuts.

# Calibration examples

GOOD TRIMS (you SHOULD flag these):
- "to Azure or Claude code driven website in 10 minutes or something like that" → trim "or something like that" (or the whole hedge sentence if it's redundant)
- "let's take a look at that website. Here's another security warning. Let's approve that also." → trim "Here's another security warning. Let's approve that also." if the next narration moves on without referencing it
- "in a few steps in less than 10 minutes or so." → trim if it's a redundant restatement

BAD TRIMS (you must NOT flag these):
- "Now I got a window security warning." → KEEP. Substantive — speaker is reporting screen state.
- "And now you see that it's deployed" → KEEP. Demo narration.
- "It didn't actually publish the site that I have." → KEEP. Reporting an outcome.

# Output

Return a JSON array. Empty array is acceptable. Aim for 4-10 small trims on a 15-min tutorial; flagging more than 15 is almost certainly over-eager.

Each entry: start (seconds), end (seconds), reason (a short string quoting the trimmed phrase verbatim from the transcript).

When in doubt, KEEP. False positives waste the user's time.

# Transcript

(each line is "[start-end] text")

{transcript}
"""


SPAN_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "start":  {"type": "NUMBER"},
            "end":    {"type": "NUMBER"},
            "reason": {"type": "STRING"},
        },
        "required": ["start", "end", "reason"],
    },
}

VERDICT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "action": {"type": "STRING", "enum": ["cut", "keep"]},
        "why":    {"type": "STRING"},
    },
    "required": ["action", "why"],
}


class kGeminiCuts:
    """The Gemini passes: retake finder, filler trim, and the multimodal retake check."""

    @staticmethod
    def TranscriptText(Segments: list[dict[str, Any]]) -> str:
        """The transcript as "[start-end] text" lines for the prompts."""
        if kS.ErrorMode:
            return ""
        try:
            return "\n".join(f"[{Segment['start']:.1f}-{Segment['end']:.1f}] {Segment['text']}" for Segment in Segments)
        except Exception as e:
            kS.GlobalErrorHandler(e, "kGeminiCuts.TranscriptText")
            return ""

    @staticmethod
    def AskForSpans(Prompt: str, Model: str) -> str:
        """Send a span prompt with the JSON array schema; returns the raw answer text."""
        if kS.ErrorMode:
            return "[]"
        try:
            GenAi, Types, _Errors = kGenAi.Load("propose.py")
            if kS.ErrorMode:
                return "[]"
            Client = GenAi.Client()
            Response = Client.models.generate_content(
                model=Model,
                contents=Prompt,
                config=Types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=SPAN_SCHEMA,
                    temperature=0.0,
                ),
            )
            return Response.text or "[]"
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kGeminiCuts.AskForSpans(model={Model})")
            return "[]"

    @staticmethod
    def ToSpans(Items: list[Any], Label: str) -> list[tuple[float, float, str]]:
        """The well-formed (start, end, "<Label>: reason") spans in the model's items; a malformed item is skipped
        (the schema makes that rare, and one bad item must not cost the other proposals)."""
        if kS.ErrorMode:
            return []
        try:
            Out = []
            for Item in Items:
                try:
                    Start = float(Item["start"])
                    End = float(Item["end"])
                    if End > Start:
                        Out.append((Start, End, f"{Label}: {Item.get('reason', '')[:80]}"))
                except (KeyError, ValueError, TypeError):  # ERROR-SUPPRESSED-JUSTIFIED: a malformed proposal is not a cut; the rest still count
                    continue
            return Out
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kGeminiCuts.ToSpans(label={Label})")
            return []

    @staticmethod
    def ParseJson(Raw: str) -> tuple[Any, bool]:
        """Return (items, ok): the parsed answer, or ([], False) when the model did not answer JSON."""
        if kS.ErrorMode:
            return ([], False)
        try:
            try:
                return (json.loads(Raw), True)
            except json.JSONDecodeError:  # ERROR-SUPPRESSED-JUSTIFIED: a non-JSON answer means no proposals; the caller warns, the run goes on
                return ([], False)
        except Exception as e:
            kS.GlobalErrorHandler(e, "kGeminiCuts.ParseJson")
            return ([], False)

    @staticmethod
    def FindFiller(Segments: list[dict[str, Any]], Model: str) -> list[tuple[float, float, str]]:
        """Second LLM pass: filler/hedging/redundant content trims.

        Same wiring as the retake finder but with a different prompt and category label.
        """
        if kS.ErrorMode:
            return []
        try:
            kGenAi.RequireKey()
            Text = kGeminiCuts.TranscriptText(Segments)
            kConsole.Progress(f"[vedit/propose] gemini filler-trim ({Model}, {len(Text):,} chars of transcript)…")
            Raw = kGeminiCuts.AskForSpans(FILLER_PROMPT.format(transcript=Text), Model)
            Items, Ok = kGeminiCuts.ParseJson(Raw)
            if not Ok:
                kConsole.Warn("WARNING: filler pass returned non-JSON; treating as empty.")
                return []
            Out = kGeminiCuts.ToSpans(Items, "filler")
            kConsole.Progress(f"[vedit/propose] filler-trim: {len(Out)} trims proposed")
            return Out
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kGeminiCuts.FindFiller(model={Model})")
            return []

    @staticmethod
    def FindRetakes(Segments: list[dict[str, Any]], Model: str) -> list[tuple[float, float, str]]:
        """Text-only LLM pass: spans where the speaker redoes speech."""
        if kS.ErrorMode:
            return []
        try:
            kGenAi.RequireKey("\n       Set it in Windows user env vars or pass --no-llm to skip.")
            Text = kGeminiCuts.TranscriptText(Segments)
            kConsole.Progress(f"[vedit/propose] gemini retake-finder ({Model}, {len(Text):,} chars of transcript)…")
            Raw = kGeminiCuts.AskForSpans(RETAKE_PROMPT.format(transcript=Text), Model)
            Items, Ok = kGeminiCuts.ParseJson(Raw)
            if not Ok:
                kConsole.Warn(f"WARNING: Gemini returned non-JSON; treating as empty.\n  raw: {Raw[:200]}")
                return []
            return kGeminiCuts.ToSpans(Items, "retake")
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kGeminiCuts.FindRetakes(model={Model})")
            return []

    @staticmethod
    def ExtractFrameJpeg(Input: Path, Seconds: float) -> tuple[bytes | None, str]:
        """Return (jpeg_bytes, "") for the frame at Seconds (q=5 ≈ 80% quality), or (None, why) when ffmpeg
        cannot grab one (past the end, unreadable frame) - the caller keeps the cut unvalidated."""
        if kS.ErrorMode:
            return (None, "halted")
        try:
            Process = subprocess.run(
                ["ffmpeg", "-y", "-nostdin", "-loglevel", "error",
                 "-ss", f"{Seconds:.3f}", "-i", str(Input),
                 "-frames:v", "1", "-f", "image2", "-vcodec", "mjpeg", "-q:v", "5",
                 "-"],
                capture_output=True, check=False,
            )
            if Process.returncode != 0 or not Process.stdout:
                return (None, f"frame extract failed at {Seconds:.3f}s: "
                              f"rc={Process.returncode} stderr={Process.stderr[-300:]!r}")
            return (Process.stdout, "")
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kGeminiCuts.ExtractFrameJpeg(file={Input}, at={Seconds})")
            return (None, "halted")

    @staticmethod
    def ValidateRetakes(Input: Path, RetakeCuts: list[tuple[float, float, str]],
                        TranscriptSegments: list[dict[str, Any]], Model: str) -> list[tuple[float, float, str]]:
        """Second pass: for each retake cut, ask Gemini multimodal whether the
        screen frame at the cut midpoint is consistent with a SPEECH retake.
        Drops cuts the model classifies as content (action retry, screen
        reaction, hypothetical comparison). A cut that could not be checked stays a cut, with a warning."""
        if kS.ErrorMode:
            return RetakeCuts
        try:
            if not RetakeCuts:
                return RetakeCuts
            GenAi, Types, _Errors = kGenAi.Load("propose.py")
            if kS.ErrorMode:
                return RetakeCuts
            Client = GenAi.Client()
            kConsole.Progress(f"[vedit/propose] validating {len(RetakeCuts)} retakes with frame check…")
            Valid = []
            for Start, End, Reason in RetakeCuts:
                CutText = kTranscript.WordsInside(TranscriptSegments, Start, End)
                Middle = (Start + End) / 2
                Jpeg, Why = kGeminiCuts.ExtractFrameJpeg(Input, Middle)
                if kS.ErrorMode:
                    return RetakeCuts
                if Jpeg is None:
                    kConsole.Warn(f"  ?  {Start:.1f}-{End:.1f}: frame extract failed ({Why}); keeping cut")
                    Valid.append((Start, End, Reason))
                    continue
                Prompt = (
                    f'In a screen-recorded software tutorial at time {Middle:.1f}s, '
                    f'the speaker says:\n'
                    f'  "{CutText}"\n\n'
                    f'The attached image is the screen at that moment.\n\n'
                    f'Decide: is this a SPEECH retake (the speaker is redoing what they '
                    f'just said) or CONTENT (the speaker is reacting to / describing '
                    f'what is on screen, retrying a command, or making a hypothetical '
                    f'comparison)?\n\n'
                    f'- "cut" = speech retake; remove this span\n'
                    f'- "keep" = content; do not remove\n\n'
                    f'When in doubt, "keep". JSON only.'
                )
                try:
                    Response = Client.models.generate_content(
                        model=Model,
                        contents=[
                            Types.Part.from_bytes(data=Jpeg, mime_type="image/jpeg"),
                            Prompt,
                        ],
                        config=Types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=VERDICT_SCHEMA,
                            temperature=0.0,
                        ),
                    )
                    Verdict = json.loads(Response.text or "{}")
                    if Verdict.get("action") == "cut":
                        Valid.append((Start, End, f"{Reason} [frame ok]"))
                        kConsole.Progress(f"  ✓ {Start:6.1f}-{End:6.1f} keep cut: {Verdict.get('why','')[:80]}")
                    else:
                        kConsole.Progress(f"  ✗ {Start:6.1f}-{End:6.1f} drop cut: {Verdict.get('why','')[:80]}")
                except Exception as Failure:  # ERROR-SUPPRESSED-JUSTIFIED: the check is optional - an unchecked retake stays a cut (the text pass's verdict) and the warning says so
                    kConsole.Warn(f"  ?  {Start:.1f}-{End:.1f}: API error ({Failure}); keeping cut")
                    Valid.append((Start, End, Reason))
            kConsole.Progress(f"[vedit/propose] frame check: {len(RetakeCuts)} → {len(Valid)} retakes")
            return Valid
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kGeminiCuts.ValidateRetakes(model={Model})")
            return RetakeCuts
