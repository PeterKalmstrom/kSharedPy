"""Propose cuts for a screen recording, writing an EDL JSON.

Three detectors, all optional via flags:

  1. Silence (ffmpeg silencedetect) — pure silence >= --silence-min-s.
  2. Static-screen + silence (ffmpeg freezedetect ∩ silencedetect)
     — used to catch shorter pauses where the screen also isn't changing.
  3. Retake/false-start finder (Gemini text-only on the transcript).

Run with uv (the script header declares google-genai and static-ffmpeg):

    uv run propose.py \
        --in "F:\\path\\input.mp4" --transcript transcript.json --out edl.json

    Optional --vad-engine silero needs silero-vad (pulls torch), added per run:
    uv run --with silero-vad propose.py --vad-engine silero ...
    (without inline-metadata support: uvx --with google-genai --with static-ffmpeg python propose.py ...)

Flags:
    --in PATH               Input video. Required.
    --transcript PATH       Transcript JSON (output of transcribe.py). Required for --llm.
    --out PATH              Output EDL JSON. Required.
    --silence-db FLOAT      dB threshold for silence (silence engine only). Default: -40.
                            Lower = more permissive (less stuff flagged as silence).
                            Camtasia recordings often have modest mic gain; -30 was
                            cutting quiet speech as silence on the validation video.
    --silence-min-s FLOAT   Min duration to mark pure silence as a cut. Default: 3.0.
    --silence-max-s FLOAT   Cap on silence-cut duration. Silences longer than
                            this are NOT cut. Disabled by default — validation
                            against a hand edit showed long silences (deploy
                            time, terminal output) are usually cut by editors
                            too. Set >0 only if YOUR recordings keep the screen
                            sitting in long silent reading-time spans on purpose.
    --static-min-s FLOAT    Min freeze+silence overlap to mark as a cut. Default: 5.0.
    --freeze-noise FLOAT    ffmpeg freezedetect noise threshold (mean abs pixel
                            diff). Lower = stricter (only truly-frozen frames
                            count). Default: 0.003. Only used when --static is on.
    --static                Enable freeze detection. OFF by default — on the
                            validation video, static-screen cuts removed too much
                            demo content (where the screen IS the point). Opt-in
                            only if your recording has clear "I forgot to stop
                            recording" dead time.
    --vad-engine ENG        silence | silero | transcript. Default: silence.
                            transcript: derive silence from Whisper transcript
                            (speech = where there are transcribed words). Avoids
                            dB-threshold calibration entirely; requires --transcript.
                            silero: NN-based VAD, requires `uv run --with silero-vad`.
    --no-llm                Disable Gemini retake finder.
    --llm-model NAME        Gemini model. Default: gemini-2.5-flash.
    --validate-retakes      For each LLM-flagged retake, extract the screen
                            frame at the cut midpoint and ask Gemini multimodal
                            to confirm it's a speech retake (not an action
                            retry or screen reaction). Drops cuts the model
                            can't confirm. Adds ~2s per retake; only worth
                            it if the text-only LLM has too many false positives.
    --trim-filler           Additional Gemini pass that flags filler / hedging /
                            redundant phrases the speaker would trim in editing.
                            DIFFERENT from retakes — these are content cuts
                            (e.g. "or something like that", "if I can spell that").
                            Disabled by default. Validation showed Kate's hand
                            edit makes ~6 of these on a 15-min tutorial.
    --trim-audio-events     Detect non-speech vocalizations (uh/um, throat
                            clears, mouth sounds) that are audible but not
                            transcribed as words. Strategy: find inter-word gaps
                            (0.1–2.0 s) that contain audio above the silence
                            threshold. Requires --transcript. Disabled by default.
                            Duration buckets: <0.3 s → mouth_sound, 0.3–0.8 s →
                            filler_sound, >0.8 s → throat_clear.
    --screen-check          Sample one frame per --screen-interval-s and detect
                            visual changes between consecutive frames.  During
                            silence detection, silences that overlap a screen
                            change are PRESERVED (screen was active — do not cut).
                            Long static silences (>= --speed-up-threshold-s with
                            no screen changes) are SPED UP instead of cut.  Adds
                            ~30–60 s to the pipeline.  Recommended for any
                            screen-recording tutorial.
    --screen-interval-s F   Frame sample interval for --screen-check. Default: 2.0.
    --screen-change-thr F   Mean absolute pixel diff (0–255) to call a frame a
                            screen change. Default: 8.  Lower = more sensitive.
    --speed-up-threshold-s F  Static-silence spans >= this are sped up rather than
                            cut.  Default: 4.0 s.
    --speed-up-factor F     Speed multiplier applied to those spans.  Default: 5.0.
    --speed-up-max-s F    Upper bound on silence duration to speed up.  Static
                          silences LONGER than this are kept at 1× (not sped
                          up and not cut) — they are demo payoff moments (AI
                          typing, deploy output, terminal progress) where
                          real-time pacing is the point.  0 disables the cap
                          (default).  Try 10.0 s for tutorials that contain
                          long autonomous-task sequences.
    --keep-until FLOAT    Hard-cut the tail after this source timestamp
                          (seconds).  Use this to drop everything after the
                          speaker's sign-off ("that's it for today", "let me
                          know").  0 disables (default).  Kate's ClaudePowerPoint
                          edit used an effective cutoff of ~674 s on a 1268 s
                          source — the demo continued past the natural outro.
    --keep-padding-s FLOAT  Shrink each cut by this on both sides so we don't
                            clip mid-word audio. Default: 0.25. Used as a
                            fallback when no transcript is available.
    --no-word-snap          Skip the word-boundary snap step (transcript-aware
                            cleanup that aligns cut boundaries to silence gaps
                            between words). Word-snap is on by default whenever
                            a transcript is loaded.
    --word-snap-drift-s F   Max distance to drag a cut boundary to a nearby
                            silence gap. Default: 0.4.
    --quiet                 Less verbose output.
"""
# /// script
# requires-python = ">=3.10"
# dependencies = ["google-genai", "static-ffmpeg"]
# ///

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from _transcript import kTranscript
from kFfmpeg import kDependency, kFfmpeg
from kGenAi import kGenAi
from kJsonFile import kJsonFile
from kShared import ToolInputException, ToolReportableException, kRun, kS, kToolException


# ---------- ffmpeg detectors -----------------------------------------------

class kSilenceDetector:
    """Silence, freeze and screen-activity detection over one recording."""

    @staticmethod
    def RunAnalysis(Command, Name):
        """Run an ffmpeg analysis pass and return its stderr. ffmpeg refusing the input is an expected state: its
        output is shown and the tool stops (exit 1)."""
        if kS.ErrorMode:
            return ""
        try:
            Process = subprocess.run(Command, capture_output=True, text=True)
            if Process.returncode != 0:
                sys.stderr.write(Process.stderr[-2000:])
                raise ToolReportableException(f"{Name} failed")
            return Process.stderr
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kSilenceDetector.RunAnalysis(step={Name})")
            return ""

    @staticmethod
    def Ffmpeg(Input, Db, MinSeconds, Quiet):
        """Return list of (start, end) for silent intervals >= MinSeconds (ffmpeg silencedetect)."""
        if kS.ErrorMode:
            return []
        try:
            Command = [
                "ffmpeg", "-nostdin", "-hide_banner",
                "-i", str(Input),
                "-af", f"silencedetect=noise={Db}dB:d={MinSeconds:.3f}",
                "-f", "null", "-",
            ]
            if not Quiet:
                print(f"[vedit/propose] ffmpeg silencedetect (noise={Db}dB d={MinSeconds}s)…", file=sys.stderr)
            Output = kSilenceDetector.RunAnalysis(Command, "silencedetect")
            Ranges = []
            CurrentStart = None
            for Line in Output.splitlines():
                Match = re.search(r"silence_start:\s*([\d.]+)", Line)
                if Match:
                    CurrentStart = float(Match.group(1))
                    continue
                Match = re.search(r"silence_end:\s*([\d.]+)", Line)
                if Match and CurrentStart is not None:
                    Ranges.append((CurrentStart, float(Match.group(1))))
                    CurrentStart = None
            return Ranges
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kSilenceDetector.Ffmpeg(file={Input})")
            return []

    @staticmethod
    def FromTranscript(Transcript, MinSeconds, Total, Quiet):
        """Derive silence ranges from the Whisper transcript: a 'silence' is any
        span of Total that contains no transcribed words for at least MinSeconds.

        This sidesteps dB-threshold calibration entirely — Whisper's VAD already
        decided what's speech. Quiet narration that ffmpeg silencedetect would
        miss-classify as silence is correctly treated as speech here.
        """
        if kS.ErrorMode:
            return []
        try:
            WordIntervals = []
            for Segment in Transcript["segments"]:
                for Word in Segment.get("words") or []:
                    WordIntervals.append((float(Word["start"]), float(Word["end"])))
            WordIntervals.sort()
            if not WordIntervals:
                return [(0.0, Total)] if Total >= MinSeconds else []
            Silences = []
            if WordIntervals[0][0] >= MinSeconds:
                Silences.append((0.0, WordIntervals[0][0]))
            for Index in range(len(WordIntervals) - 1):
                GapStart = WordIntervals[Index][1]
                GapEnd = WordIntervals[Index + 1][0]
                if GapEnd - GapStart >= MinSeconds:
                    Silences.append((GapStart, GapEnd))
            if Total - WordIntervals[-1][1] >= MinSeconds:
                Silences.append((WordIntervals[-1][1], Total))
            if not Quiet:
                print(f"[vedit/propose] transcript-VAD: {len(Silences)} silence ranges "
                      f"≥{MinSeconds}s from {len(WordIntervals)} words",
                      file=sys.stderr)
            return Silences
        except Exception as e:
            kS.GlobalErrorHandler(e, "kSilenceDetector.FromTranscript")
            return []

    @staticmethod
    def Silero(Input, MinSeconds, Total, Quiet):
        """Detect silences using silero-vad (NN-based VAD).

        Speech timestamps are inverted to silence ranges, including any leading
        silence before the first speech and trailing silence after the last.
        Requires the optional `silero-vad` package (pulls torch): `uv run --with silero-vad propose.py`.
        """
        if kS.ErrorMode:
            return []
        try:
            if not kDependency.Has("silero_vad"):
                raise ToolInputException(
                    "silero-vad not installed.\n"
                    "Run with: uv run --with silero-vad propose.py --vad-engine silero ...")
            from silero_vad import get_speech_timestamps, load_silero_vad, read_audio  # optional, checked above
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as File:
                WavPath = File.name
            try:
                if not Quiet:
                    print("[vedit/propose] extracting 16kHz mono audio for silero-vad…", file=sys.stderr)
                kSilenceDetector.RunAnalysis(
                    ["ffmpeg", "-y", "-nostdin", "-loglevel", "error",
                     "-i", str(Input), "-ac", "1", "-ar", "16000", "-vn", WavPath],
                    "audio extraction for silero")
                if kS.ErrorMode:
                    return []
                if not Quiet:
                    print("[vedit/propose] silero-vad…", file=sys.stderr)
                Model = load_silero_vad(onnx=True)
                Wav = read_audio(WavPath, sampling_rate=16000)
                Speech = get_speech_timestamps(
                    Wav, Model, sampling_rate=16000, return_seconds=True,
                    min_silence_duration_ms=int(MinSeconds * 1000),
                )
                Silences = []
                PreviousEnd = 0.0
                for Span in Speech:
                    SpeechStart, SpeechEnd = float(Span["start"]), float(Span["end"])
                    if SpeechStart - PreviousEnd >= MinSeconds:
                        Silences.append((PreviousEnd, SpeechStart))
                    PreviousEnd = SpeechEnd
                if Total - PreviousEnd >= MinSeconds:
                    Silences.append((PreviousEnd, Total))
                return Silences
            finally:
                Path(WavPath).unlink(missing_ok=True)
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kSilenceDetector.Silero(file={Input})")
            return []

    @staticmethod
    def Freezes(Input, Noise, MinSeconds, Quiet):
        """Return list of (start, end) for static-screen intervals >= MinSeconds.

        Uses ffmpeg freezedetect: marks a range frozen when consecutive frames
        differ by less than `Noise` (mean abs pixel diff) for at least `MinSeconds`.
        """
        if kS.ErrorMode:
            return []
        try:
            Command = [
                "ffmpeg", "-nostdin", "-hide_banner",
                "-i", str(Input),
                "-vf", f"freezedetect=n={Noise}:d={MinSeconds:.3f}",
                "-an", "-f", "null", "-",
            ]
            if not Quiet:
                print(f"[vedit/propose] ffmpeg freezedetect (n={Noise} d={MinSeconds}s)…", file=sys.stderr)
            Output = kSilenceDetector.RunAnalysis(Command, "freezedetect")
            Starts = []
            Ends = []
            for Line in Output.splitlines():
                Match = re.search(r"freeze_start:\s*([\d.]+)", Line)
                if Match:
                    Starts.append(float(Match.group(1)))
                    continue
                Match = re.search(r"freeze_end:\s*([\d.]+)", Line)
                if Match:
                    Ends.append(float(Match.group(1)))
            # If a freeze is still active at EOF there's no freeze_end — drop it.
            return list(zip(Starts, Ends))
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kSilenceDetector.Freezes(file={Input})")
            return []

    @staticmethod
    def ScreenActivity(Input, IntervalSeconds=2.0, ChangeThreshold=8.0, Quiet=False):
        """Extract one grayscale frame per IntervalSeconds (128×72), compare consecutive
        frames via mean absolute pixel difference.  Returns timestamps of frames where
        the change exceeds ChangeThreshold (0–255 scale).

        A threshold of ~8 catches UI updates and text changes while ignoring codec
        noise on static screen content.  No extra dependencies — uses ffmpeg rawvideo
        pipe and plain Python byte arithmetic.
        """
        if kS.ErrorMode:
            return []
        try:
            Width, Height = 128, 72
            FrameBytes = Width * Height
            Step = max(1, FrameBytes // 1024)  # subsample to ~1024 pixels for speed
            Command = [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-i", str(Input),
                "-vf", f"fps={1.0 / IntervalSeconds:.4f},scale={Width}:{Height}:flags=fast_bilinear,format=gray",
                "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
            ]
            Process = subprocess.Popen(Command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            Changes = []
            Previous = None
            FrameIndex = 0
            try:
                while True:
                    Chunk = Process.stdout.read(FrameBytes)
                    if len(Chunk) < FrameBytes:
                        break
                    if Previous is not None:
                        Difference = (
                            sum(abs(Chunk[Pixel] - Previous[Pixel]) for Pixel in range(0, FrameBytes, Step))
                            * Step / FrameBytes
                        )
                        if Difference > ChangeThreshold:
                            Changes.append(FrameIndex * IntervalSeconds)
                    Previous = Chunk
                    FrameIndex += 1
            finally:
                Process.stdout.close()
                Process.wait()
            if not Quiet:
                print(
                    f"[vedit/propose] screen-check: {FrameIndex} frames × {IntervalSeconds}s, "
                    f"{len(Changes)} changes detected",
                    file=sys.stderr,
                )
            return Changes
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kSilenceDetector.ScreenActivity(file={Input})")
            return []

    @staticmethod
    def HasScreenChange(Start, End, Changes, IntervalSeconds):
        """True iff any screen-change timestamp falls within [Start, End].

        A change recorded at timestamp t means the screen differed between frames at
        t−IntervalSeconds and t, so we extend the window by ±IntervalSeconds on each side.
        """
        if kS.ErrorMode:
            return False
        try:
            for Change in Changes:
                if Start - IntervalSeconds < Change < End + IntervalSeconds:
                    return True
            return False
        except Exception as e:
            kS.GlobalErrorHandler(e, "kSilenceDetector.HasScreenChange")
            return False


# ---------- LLM retake finder ----------------------------------------------

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
    def TranscriptText(Segments):
        """The transcript as "[start-end] text" lines for the prompts."""
        if kS.ErrorMode:
            return ""
        try:
            return "\n".join(f"[{Segment['start']:.1f}-{Segment['end']:.1f}] {Segment['text']}" for Segment in Segments)
        except Exception as e:
            kS.GlobalErrorHandler(e, "kGeminiCuts.TranscriptText")
            return ""

    @staticmethod
    def AskForSpans(Prompt, Model):
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
    def ToSpans(Items, Label):
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
    def ParseJson(Raw):
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
    def FindFiller(Segments, Model, Quiet):
        """Second LLM pass: filler/hedging/redundant content trims.

        Same wiring as the retake finder but with a different prompt and category label.
        """
        if kS.ErrorMode:
            return []
        try:
            kGenAi.RequireKey()
            Text = kGeminiCuts.TranscriptText(Segments)
            if not Quiet:
                print(f"[vedit/propose] gemini filler-trim ({Model}, "
                      f"{len(Text):,} chars of transcript)…", file=sys.stderr)
            Raw = kGeminiCuts.AskForSpans(FILLER_PROMPT.format(transcript=Text), Model)
            Items, Ok = kGeminiCuts.ParseJson(Raw)
            if not Ok:
                sys.stderr.write("WARNING: filler pass returned non-JSON; treating as empty.\n")
                return []
            Out = kGeminiCuts.ToSpans(Items, "filler")
            if not Quiet:
                print(f"[vedit/propose] filler-trim: {len(Out)} trims proposed",
                      file=sys.stderr)
            return Out
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kGeminiCuts.FindFiller(model={Model})")
            return []

    @staticmethod
    def FindRetakes(Segments, Model, Quiet):
        """Text-only LLM pass: spans where the speaker redoes speech."""
        if kS.ErrorMode:
            return []
        try:
            kGenAi.RequireKey("\n       Set it in Windows user env vars or pass --no-llm to skip.")
            Text = kGeminiCuts.TranscriptText(Segments)
            if not Quiet:
                print(f"[vedit/propose] gemini retake-finder ({Model}, "
                      f"{len(Text):,} chars of transcript)…", file=sys.stderr)
            Raw = kGeminiCuts.AskForSpans(RETAKE_PROMPT.format(transcript=Text), Model)
            Items, Ok = kGeminiCuts.ParseJson(Raw)
            if not Ok:
                sys.stderr.write(f"WARNING: Gemini returned non-JSON; treating as empty.\n  raw: {Raw[:200]}\n")
                return []
            return kGeminiCuts.ToSpans(Items, "retake")
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kGeminiCuts.FindRetakes(model={Model})")
            return []

    @staticmethod
    def ExtractFrameJpeg(Input, Seconds):
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
    def ValidateRetakes(Input, RetakeCuts, TranscriptSegments, Model, Quiet):
        """Second pass: for each retake cut, ask Gemini multimodal whether the
        screen frame at the cut midpoint is consistent with a SPEECH retake.
        Drops cuts the model classifies as content (action retry, screen
        reaction, hypothetical comparison)."""
        if kS.ErrorMode:
            return RetakeCuts
        try:
            if not RetakeCuts:
                return RetakeCuts
            GenAi, Types, _Errors = kGenAi.Load("propose.py")
            if kS.ErrorMode:
                return RetakeCuts
            Client = GenAi.Client()
            if not Quiet:
                print(f"[vedit/propose] validating {len(RetakeCuts)} retakes with frame check…",
                      file=sys.stderr)
            Valid = []
            for Start, End, Reason in RetakeCuts:
                CutText = kTranscript.WordsInside(TranscriptSegments, Start, End)
                Middle = (Start + End) / 2
                Jpeg, Why = kGeminiCuts.ExtractFrameJpeg(Input, Middle)
                if kS.ErrorMode:
                    return RetakeCuts
                if Jpeg is None:
                    if not Quiet:
                        print(f"  ?  {Start:.1f}-{End:.1f}: frame extract failed ({Why}); keeping cut",
                              file=sys.stderr)
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
                        if not Quiet:
                            print(f"  ✓ {Start:6.1f}-{End:6.1f} keep cut: {Verdict.get('why','')[:80]}",
                                  file=sys.stderr)
                    elif not Quiet:
                        print(f"  ✗ {Start:6.1f}-{End:6.1f} drop cut: {Verdict.get('why','')[:80]}",
                              file=sys.stderr)
                except Exception as Failure:  # ERROR-SUPPRESSED-JUSTIFIED: the check is optional - an unchecked retake stays a cut (the text pass's verdict) and the line says so
                    if not Quiet:
                        print(f"  ?  {Start:.1f}-{End:.1f}: API error ({Failure}); keeping cut",
                              file=sys.stderr)
                    Valid.append((Start, End, Reason))
            if not Quiet:
                print(f"[vedit/propose] frame check: {len(RetakeCuts)} → {len(Valid)} retakes",
                      file=sys.stderr)
            return Valid
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kGeminiCuts.ValidateRetakes(model={Model})")
            return RetakeCuts


# ---------- merging --------------------------------------------------------

class kCutEditor:
    """Interval arithmetic on cuts, word snapping, audio events, and the EDL itself."""

    @staticmethod
    def Intersect(First, Second):
        """Interval-list intersection. Both inputs sorted, non-overlapping."""
        if kS.ErrorMode:
            return []
        try:
            Out = []
            I = J = 0
            while I < len(First) and J < len(Second):
                Start = max(First[I][0], Second[J][0])
                End = min(First[I][1], Second[J][1])
                if Start < End:
                    Out.append((Start, End))
                if First[I][1] < Second[J][1]:
                    I += 1
                else:
                    J += 1
            return Out
        except Exception as e:
            kS.GlobalErrorHandler(e, "kCutEditor.Intersect")
            return []

    @staticmethod
    def StartKey(Item):
        """Sort key: a word dict's "start"."""
        if kS.ErrorMode:
            return 0.0
        try:
            return Item["start"]
        except Exception as e:
            kS.GlobalErrorHandler(e, "kCutEditor.StartKey")
            return 0.0

    @staticmethod
    def FirstKey(Item):
        """Sort key: a cut tuple's start."""
        if kS.ErrorMode:
            return 0.0
        try:
            return Item[0]
        except Exception as e:
            kS.GlobalErrorHandler(e, "kCutEditor.FirstKey")
            return 0.0

    @staticmethod
    def FlattenWords(Transcript):
        """All words from all transcript segments, in time order."""
        if kS.ErrorMode:
            return []
        try:
            Words = []
            for Segment in Transcript["segments"]:
                for Word in Segment.get("words") or []:
                    Words.append({"start": float(Word["start"]), "end": float(Word["end"])})
            Words.sort(key=kCutEditor.StartKey)
            return Words
        except Exception as e:
            kS.GlobalErrorHandler(e, "kCutEditor.FlattenWords")
            return []

    @staticmethod
    def SnapToSilenceGap(Seconds, Words, MaxDrift):
        """Find the midpoint of the silence gap closest to Seconds (within MaxDrift).
        Returns Seconds unchanged if no gap is close enough.

        A 'silence gap' is the space between word i's end and word i+1's start,
        where end < start. Cut boundaries snapped to gap midpoints don't clip
        audible word edges.
        """
        if kS.ErrorMode:
            return Seconds
        try:
            if not Words:
                return Seconds
            BestMiddle = None
            BestDistance = MaxDrift + 1
            for Index in range(len(Words) - 1):
                GapStart = Words[Index]["end"]
                GapEnd = Words[Index + 1]["start"]
                if GapEnd <= GapStart + 0.005:
                    continue
                GapMiddle = (GapStart + GapEnd) / 2
                Distance = abs(GapMiddle - Seconds)
                if Distance < BestDistance:
                    BestDistance = Distance
                    BestMiddle = GapMiddle
            if BestMiddle is not None and BestDistance <= MaxDrift:
                return BestMiddle
            return Seconds
        except Exception as e:
            kS.GlobalErrorHandler(e, "kCutEditor.SnapToSilenceGap")
            return Seconds

    @staticmethod
    def WordSnapCuts(Cuts, Words, MaxDrift):
        """Every cut with both boundaries snapped to the nearest silence gap; cuts that collapse are dropped."""
        if kS.ErrorMode:
            return []
        try:
            Snapped = []
            for Start, End, Reason in Cuts:
                NewStart = kCutEditor.SnapToSilenceGap(Start, Words, MaxDrift)
                NewEnd = kCutEditor.SnapToSilenceGap(End, Words, MaxDrift)
                if NewEnd > NewStart:
                    Snapped.append((NewStart, NewEnd, Reason))
            return Snapped
        except Exception as e:
            kS.GlobalErrorHandler(e, "kCutEditor.WordSnapCuts")
            return []

    @staticmethod
    def IsFullySilent(Silences, Start, End):
        """True iff every point in [Start, End] falls in some silence interval (Silences sorted)."""
        if kS.ErrorMode:
            return True
        try:
            Cursor = Start
            for SilenceStart, SilenceEnd in Silences:
                if SilenceEnd <= Start:
                    continue
                if SilenceStart > Cursor + 0.015:
                    return False
                Cursor = max(Cursor, SilenceEnd)
                if Cursor >= End - 0.015:
                    return True
            return Cursor >= End - 0.015
        except Exception as e:
            kS.GlobalErrorHandler(e, "kCutEditor.IsFullySilent")
            return True

    @staticmethod
    def DetectAudioEvents(Input, Transcript, Quiet, MinDuration=0.1, MaxDuration=2.0, SilenceDb=-40.0):
        """Find non-speech vocalizations: audible sounds in inter-word gaps.

        Strategy: any gap between consecutive transcript words that (a) falls in
        [MinDuration, MaxDuration] and (b) is not fully covered by silence detections
        must contain audible non-speech content — a throat clear, uh/um, or mouth
        sound. Runs one ffmpeg silencedetect call (min 0.05 s, same threshold as
        the main pass) to get the silence map; gap audio presence is then pure
        interval arithmetic, no per-gap ffmpeg calls.
        """
        if kS.ErrorMode:
            return []
        try:
            AllWords = []
            for Segment in Transcript["segments"]:
                for Word in Segment.get("words") or []:
                    AllWords.append((float(Word["start"]), float(Word["end"])))
            AllWords.sort()
            if len(AllWords) < 2:
                return []
            # Candidate inter-word gaps in the target duration window
            Gaps = []
            for Index in range(len(AllWords) - 1):
                GapStart = AllWords[Index][1]
                GapEnd = AllWords[Index + 1][0]
                if MinDuration <= GapEnd - GapStart <= MaxDuration:
                    Gaps.append((GapStart, GapEnd))
            if not Gaps:
                return []
            # One ffmpeg silencedetect pass with a very short min-duration to build
            # a complete silence map. We deliberately use 0.05 so even brief
            # silent patches are captured (we're looking for ANY audible content).
            Silences = sorted(kSilenceDetector.Ffmpeg(Input, SilenceDb, 0.05, True))
            Events = []
            for GapStart, GapEnd in Gaps:
                if not kCutEditor.IsFullySilent(Silences, GapStart, GapEnd):
                    Duration = GapEnd - GapStart
                    if Duration < 0.3:
                        Hint = "mouth_sound"
                    elif Duration < 0.8:
                        Hint = "filler_sound"
                    else:
                        Hint = "throat_clear"
                    Events.append((GapStart, GapEnd, f"audio_event: {Hint}"))
            if not Quiet:
                print(
                    f"[vedit/propose] audio-events: {len(Events)} vocalizations "
                    f"({sum(1 for Event in Events if 'throat' in Event[2])} throat_clear, "
                    f"{sum(1 for Event in Events if 'filler' in Event[2])} filler_sound, "
                    f"{sum(1 for Event in Events if 'mouth' in Event[2])} mouth_sound)",
                    file=sys.stderr,
                )
            return Events
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kCutEditor.DetectAudioEvents(file={Input})")
            return []

    @staticmethod
    def MergeCuts(Cuts):
        """Sort the cuts and merge overlapping or touching ones, joining their reasons."""
        if kS.ErrorMode:
            return []
        try:
            if not Cuts:
                return []
            Cuts = sorted(Cuts, key=kCutEditor.FirstKey)
            Merged = [Cuts[0]]
            for Start, End, Reason in Cuts[1:]:
                LastStart, LastEnd, LastReason = Merged[-1]
                if Start <= LastEnd + 0.001:
                    NewReason = LastReason if Reason == LastReason or Reason in LastReason else f"{LastReason}+{Reason}"
                    Merged[-1] = (LastStart, max(LastEnd, End), NewReason)
                else:
                    Merged.append((Start, End, Reason))
            return Merged
        except Exception as e:
            kS.GlobalErrorHandler(e, "kCutEditor.MergeCuts")
            return []

    @staticmethod
    def IntervalCut(Cuts, Start, End):
        """(True, reason) when the middle of [Start, End] lies in a cut, else (False, "")."""
        if kS.ErrorMode:
            return (False, "")
        try:
            Middle = (Start + End) / 2
            for CutStart, CutEnd, CutReason in Cuts:
                if CutStart <= Middle < CutEnd:
                    return (True, CutReason)
            return (False, "")
        except Exception as e:
            kS.GlobalErrorHandler(e, "kCutEditor.IntervalCut")
            return (False, "")

    @staticmethod
    def IntervalSpeed(SpeedMap, Start, End):
        """The speed of the speed-up span holding the middle of [Start, End], else 1.0."""
        if kS.ErrorMode:
            return 1.0
        try:
            Middle = (Start + End) / 2
            for SpeedStart, SpeedEnd, Speed in SpeedMap:
                if SpeedStart <= Middle < SpeedEnd:
                    return Speed
            return 1.0
        except Exception as e:
            kS.GlobalErrorHandler(e, "kCutEditor.IntervalSpeed")
            return 1.0

    @staticmethod
    def BuildEdl(Cuts, Total, Source, SpeedSegments=None):
        """Build EDL from cuts (keep=False) and optional SpeedSegments (keep=True, speed>1).

        All boundary points from both cuts and SpeedSegments are collected first, then
        the timeline is split into atomic intervals so that a speed-up zone can never
        bleed into adjacent speech.  Consecutive same-type intervals are merged back.
        """
        if kS.ErrorMode:
            return {}
        try:
            Cuts = [(max(0.0, Start), min(Total, End), Reason) for Start, End, Reason in Cuts if End > Start]
            SpeedMap = []
            if SpeedSegments:
                for SpeedStart, SpeedEnd, _Reason, Speed in SpeedSegments:
                    if SpeedEnd > SpeedStart:
                        SpeedMap.append((max(0.0, SpeedStart), min(Total, SpeedEnd), Speed))
            # Gather every boundary so each interval is unambiguously one type
            Points = {0.0, Total}
            for Start, End, _Reason in Cuts:
                Points.add(Start)
                Points.add(End)
            for SpeedStart, SpeedEnd, _Speed in SpeedMap:
                Points.add(SpeedStart)
                Points.add(SpeedEnd)
            SortedPoints = sorted(Points)
            Raw = []
            for Index in range(len(SortedPoints) - 1):
                Start, End = SortedPoints[Index], SortedPoints[Index + 1]
                if End - Start < 0.001:
                    continue
                IsCut, Reason = kCutEditor.IntervalCut(Cuts, Start, End)
                if IsCut:
                    Raw.append({"start": Start, "end": End, "keep": False, "reason": Reason})
                else:
                    Speed = kCutEditor.IntervalSpeed(SpeedMap, Start, End)
                    Segment = {"start": Start, "end": End, "keep": True, "reason": ""}
                    if Speed != 1.0:
                        Segment["speed"] = Speed
                    Raw.append(Segment)
            # Merge consecutive intervals with identical keep/speed/reason
            Segments = []
            for Segment in Raw:
                if (
                    Segments
                    and Segments[-1]["keep"] == Segment["keep"]
                    and Segments[-1].get("speed", 1.0) == Segment.get("speed", 1.0)
                    and Segments[-1]["reason"] == Segment["reason"]
                ):
                    Segments[-1]["end"] = Segment["end"]
                else:
                    Segments.append(dict(Segment))
            for Segment in Segments:
                Segment["start"] = round(Segment["start"], 3)
                Segment["end"] = round(Segment["end"], 3)
            return {
                "source": Source,
                "duration_s": round(Total, 3),
                "version": 1,
                "segments": Segments,
            }
        except Exception as e:
            kS.GlobalErrorHandler(e, "kCutEditor.BuildEdl")
            return {}


# ---------- main -----------------------------------------------------------

class kProposeApp:
    """Command line."""

    @staticmethod
    def ParseArgs():
        """The command-line flags."""
        if kS.ErrorMode:
            return None
        try:
            Parser = argparse.ArgumentParser(description="Propose cuts as an EDL.")
            Parser.add_argument("--in", dest="inp", required=True)
            Parser.add_argument("--transcript", default=None,
                                help="Required unless --no-llm")
            Parser.add_argument("--out", required=True)
            Parser.add_argument("--silence-db", type=float, default=-40.0)
            Parser.add_argument("--silence-min-s", type=float, default=3.0)
            Parser.add_argument("--silence-max-s", type=float, default=0.0,
                                help="Cap silence-cut duration. 0 (default) disables.")
            Parser.add_argument("--static-min-s", type=float, default=5.0)
            Parser.add_argument("--freeze-noise", type=float, default=0.003)
            Parser.add_argument("--static", action="store_true",
                                help="Enable freeze+silence cuts. OFF by default (see --help).")
            Parser.add_argument("--vad-engine", default="silence",
                                choices=["silence", "silero", "transcript"])
            Parser.add_argument("--no-llm", action="store_true")
            Parser.add_argument("--llm-model", default="gemini-2.5-flash")
            Parser.add_argument("--validate-retakes", action="store_true",
                                help="Multimodal frame-check on each retake. Drops false positives.")
            Parser.add_argument("--trim-filler", action="store_true",
                                help="Additional Gemini pass for filler/hedging/redundant trims.")
            Parser.add_argument("--trim-audio-events", action="store_true",
                                help="Detect non-speech vocalizations in inter-word gaps "
                                     "(throat clears, uh/um, mouth sounds).")
            Parser.add_argument("--screen-check", action="store_true",
                                help="Preserve silences with screen activity; speed up long static silences.")
            Parser.add_argument("--screen-interval-s", type=float, default=2.0,
                                help="Frame sample interval for screen-check (seconds). Default: 2.0.")
            Parser.add_argument("--screen-change-thr", type=float, default=8.0,
                                help="Mean pixel diff (0-255) to call a frame a screen change. Default: 8.")
            Parser.add_argument("--speed-up-threshold-s", type=float, default=4.0,
                                help="Static-silence spans >= this are sped up instead of cut. Default: 4.0.")
            Parser.add_argument("--speed-up-factor", type=float, default=5.0,
                                help="Speed multiplier for static-silence spans. Default: 5.0.")
            Parser.add_argument("--speed-up-max-s", type=float, default=0.0,
                                help="Keep static silences longer than this at 1x (demo payoff). 0=disabled.")
            Parser.add_argument("--keep-until", type=float, default=0.0,
                                help="Hard-cut everything after this source timestamp (seconds). 0=disabled.")
            Parser.add_argument("--keep-padding-s", type=float, default=0.25)
            Parser.add_argument("--no-word-snap", action="store_true")
            Parser.add_argument("--word-snap-drift-s", type=float, default=0.4)
            Parser.add_argument("--quiet", action="store_true")
            return Parser.parse_args()
        except Exception as e:
            kS.GlobalErrorHandler(e, "kProposeApp.ParseArgs")
            return None

    def DetectSilences(self, Input, Total, TranscriptData):
        """Return (long_silences, short_silences) with the chosen VAD engine; short ones only with --static."""
        if kS.ErrorMode:
            return ([], [])
        try:
            Args = self.Args
            if Args.vad_engine == "silero":
                Long = kSilenceDetector.Silero(Input, Args.silence_min_s, Total, Args.quiet)
                Short = kSilenceDetector.Silero(Input, 1.0, Total, Args.quiet) if Args.static else []
            elif Args.vad_engine == "transcript":
                if TranscriptData is None:
                    raise ToolInputException("--vad-engine transcript requires --transcript")
                Long = kSilenceDetector.FromTranscript(TranscriptData, Args.silence_min_s, Total, Args.quiet)
                Short = kSilenceDetector.FromTranscript(TranscriptData, 1.0, Total, Args.quiet) \
                    if Args.static else []
            else:
                Long = kSilenceDetector.Ffmpeg(Input, Args.silence_db, Args.silence_min_s, Args.quiet)
                Short = kSilenceDetector.Ffmpeg(Input, Args.silence_db, 1.0, Args.quiet) if Args.static else []
            return (Long, Short)
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, "kProposeApp.DetectSilences")
            return ([], [])

    def ClassifySilences(self, SilencesLong, ScreenChanges):
        """Return (cuts, speed_segments): each long silence is skipped, kept, sped up or cut."""
        if kS.ErrorMode:
            return ([], [])
        try:
            Args = self.Args
            Cuts = []
            SpeedSegments = []
            SkippedLong = ScreenActiveSkipped = DemoPayoffKept = 0
            for Start, End in SilencesLong:
                if Args.silence_max_s > 0 and (End - Start) > Args.silence_max_s:
                    SkippedLong += 1
                    continue
                if Args.screen_check and kSilenceDetector.HasScreenChange(
                    Start, End, ScreenChanges, Args.screen_interval_s,
                ):
                    ScreenActiveSkipped += 1
                    continue
                if Args.screen_check and (End - Start) >= Args.speed_up_threshold_s:
                    if Args.speed_up_max_s > 0 and (End - Start) > Args.speed_up_max_s:
                        DemoPayoffKept += 1  # long static silence — the demo is the point; keep at 1x
                    else:
                        SpeedSegments.append((
                            Start, End,
                            f"screen_inactive>{Args.speed_up_threshold_s:.0f}s",
                            Args.speed_up_factor,
                        ))
                else:
                    Cuts.append((Start, End, f"silence>{Args.silence_min_s:.0f}s"))
            if SkippedLong and not Args.quiet:
                print(f"[vedit/propose] skipped {SkippedLong} long silences "
                      f"(>{Args.silence_max_s:.0f}s) — likely demo content, not dead time",
                      file=sys.stderr)
            if ScreenActiveSkipped and not Args.quiet:
                print(f"[vedit/propose] screen-check: {ScreenActiveSkipped} silence(s) preserved "
                      f"(screen was active during that span)", file=sys.stderr)
            if DemoPayoffKept and not Args.quiet:
                print(f"[vedit/propose] screen-check: {DemoPayoffKept} long-silence span(s) kept "
                      f"at 1x (demo payoff, >{Args.speed_up_max_s:.0f}s)", file=sys.stderr)
            if SpeedSegments and not Args.quiet:
                SpeedTotal = sum(Segment[1] - Segment[0] for Segment in SpeedSegments)
                print(f"[vedit/propose] screen-check: {len(SpeedSegments)} static-silence span(s) → "
                      f"{Args.speed_up_factor:.0f}x speed-up ({SpeedTotal:.1f}s source → "
                      f"{SpeedTotal / Args.speed_up_factor:.1f}s on timeline)", file=sys.stderr)
            return (Cuts, SpeedSegments)
        except Exception as e:
            kS.GlobalErrorHandler(e, "kProposeApp.ClassifySilences")
            return ([], [])

    def CleanBoundaries(self, Cuts, TranscriptData):
        """Word-snap the cut boundaries when a transcript is loaded, else shrink each cut by the padding.
        Audio-event cuts are already at inter-word boundaries by construction — snapping them can pull a cut end
        into the adjacent word, so they are excluded from the snap pass and re-added afterwards."""
        if kS.ErrorMode:
            return []
        try:
            Args = self.Args
            if TranscriptData is not None and not Args.no_word_snap:
                Words = kCutEditor.FlattenWords(TranscriptData)
                AudioEventCuts = [Cut for Cut in Cuts if Cut[2].startswith("audio_event:")]
                SnapCuts = [Cut for Cut in Cuts if not Cut[2].startswith("audio_event:")]
                Before = len(SnapCuts)
                SnapCuts = kCutEditor.WordSnapCuts(SnapCuts, Words, Args.word_snap_drift_s)
                if not Args.quiet:
                    print(f"[vedit/propose] word-snap: {Before} cuts → {len(SnapCuts)} "
                          f"after snapping to silence gaps within "
                          f"±{Args.word_snap_drift_s}s",
                          file=sys.stderr)
                return SnapCuts + AudioEventCuts
            if Args.keep_padding_s > 0:
                Pad = Args.keep_padding_s
                return [(Start + Pad, End - Pad, Reason) for Start, End, Reason in Cuts if End - Start > 2 * Pad]
            return Cuts
        except Exception as e:
            kS.GlobalErrorHandler(e, "kProposeApp.CleanBoundaries")
            return []

    def Summarize(self, Edl, Total, Out, StartTime):
        """The closing stderr line: cut spans, cut share and speed-ups."""
        if kS.ErrorMode:
            return
        try:
            Segments = Edl["segments"]
            CutCount = sum(1 for Segment in Segments if not Segment["keep"])
            CutDuration = sum(Segment["end"] - Segment["start"] for Segment in Segments if not Segment["keep"])
            Sped = [Segment for Segment in Segments if Segment.get("speed", 1.0) != 1.0]
            SpeedSource = sum(Segment["end"] - Segment["start"] for Segment in Sped)
            SpeedTimeline = sum((Segment["end"] - Segment["start"]) / Segment["speed"] for Segment in Sped)
            if not self.Args.quiet:
                SpeedNote = (
                    f", {len(Sped)} speed-up span(s) ({SpeedSource:.1f}s → {SpeedTimeline:.1f}s at "
                    f"{self.Args.speed_up_factor:.0f}x)" if Sped else ""
                )
                print(f"[vedit/propose] done in {time.monotonic() - StartTime:.1f}s. "
                      f"{CutCount} cut spans, {CutDuration:.1f}s "
                      f"({100 * CutDuration / Total:.1f}% of total){SpeedNote} → {Out}",
                      file=sys.stderr)
        except Exception as e:
            kS.GlobalErrorHandler(e, "kProposeApp.Summarize")
            return

    def Run(self):
        if kS.ErrorMode:
            return 1
        try:
            self.Args = kProposeApp.ParseArgs()
            Args = self.Args
            Input = Path(Args.inp)
            Out = Path(Args.out)
            if not Input.exists():
                raise ToolInputException(f"input not found: {Input}")
            kFfmpeg.Ensure()
            Total = kFfmpeg.ProbeDurationSeconds(Input)
            if kS.ErrorMode:
                return 1
            if not Args.quiet:
                print(f"[vedit/propose] duration: {Total:.1f}s", file=sys.stderr)

            # Load the transcript up front (used for both the LLM retake finder and word-snap)
            TranscriptData = None
            if Args.transcript:
                TranscriptData = kJsonFile.Load(Args.transcript, "transcript")
                if kS.ErrorMode:
                    return 1
            StartTime = time.monotonic()

            # 1. Pure silence (>= silence_min_s, <= silence_max_s if the cap is set)
            SilencesLong, SilencesShort = self.DetectSilences(Input, Total, TranscriptData)
            # Screen-change detection (run once here; the result is used during silence filtering)
            ScreenChanges = []
            if Args.screen_check:
                ScreenChanges = kSilenceDetector.ScreenActivity(
                    Input, Args.screen_interval_s, Args.screen_change_thr, Args.quiet,
                )
            Cuts, SpeedSegments = self.ClassifySilences(SilencesLong, ScreenChanges)

            # 2. Freeze ∩ shorter silence — only when --static is on
            if Args.static:
                Freezes = kSilenceDetector.Freezes(Input, Args.freeze_noise, 2.0, Args.quiet)
                for Start, End in kCutEditor.Intersect(sorted(SilencesShort), sorted(Freezes)):
                    if End - Start >= Args.static_min_s and (
                        Args.silence_max_s <= 0 or (End - Start) <= Args.silence_max_s
                    ):
                        Cuts.append((Start, End, "static+silent"))

            # 3. Gemini retake finder (text-only first pass)
            if not Args.no_llm:
                if TranscriptData is None:
                    raise ToolInputException("--transcript required unless --no-llm")
                RetakeCuts = kGeminiCuts.FindRetakes(TranscriptData["segments"], Args.llm_model, Args.quiet)
                # Optional: multimodal second pass to validate each retake against the screen
                if RetakeCuts and Args.validate_retakes:
                    RetakeCuts = kGeminiCuts.ValidateRetakes(
                        Input, RetakeCuts, TranscriptData["segments"], Args.llm_model, Args.quiet,
                    )
                Cuts.extend(RetakeCuts)

            # 3b. Optional filler/hedging trim pass (different prompt, content cuts)
            if Args.trim_filler:
                if TranscriptData is None:
                    raise ToolInputException("--transcript required for --trim-filler")
                Cuts.extend(kGeminiCuts.FindFiller(TranscriptData["segments"], Args.llm_model, Args.quiet))

            # 3c. Non-speech vocalization detection (audible but not transcribed as words)
            if Args.trim_audio_events:
                if TranscriptData is None:
                    raise ToolInputException("--transcript required for --trim-audio-events")
                Cuts.extend(kCutEditor.DetectAudioEvents(Input, TranscriptData, Args.quiet))

            # 4a. keep-until: hard-cut the tail after the speaker's sign-off
            if Args.keep_until > 0 and Args.keep_until < Total:
                Cuts.append((Args.keep_until, Total, "keep_until: tail after sign-off"))
                if not Args.quiet:
                    print(
                        f"[vedit/propose] keep-until: dropping tail {Args.keep_until:.1f}s–"
                        f"{Total:.1f}s ({Total - Args.keep_until:.1f}s)",
                        file=sys.stderr,
                    )

            # 4b. Cleanup boundaries: word-snap when a transcript is available, else padding.
            Cuts = kCutEditor.MergeCuts(self.CleanBoundaries(Cuts, TranscriptData))
            Edl = kCutEditor.BuildEdl(Cuts, Total, str(Input), SpeedSegments=SpeedSegments or None)
            if kS.ErrorMode:
                return 1
            if not kJsonFile.Save(Out, Edl):
                return 1
            self.Summarize(Edl, Total, Out, StartTime)
            return 0
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, "kProposeApp.Run")
            return 1


if __name__ == "__main__":
    kRun.Main(kProposeApp)
