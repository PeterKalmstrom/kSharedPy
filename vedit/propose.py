"""Propose cuts for a screen recording, writing an EDL JSON.

Three detectors, all optional via flags:

  1. Silence (ffmpeg silencedetect) — pure silence >= --silence-min-s.
  2. Static-screen + silence (ffmpeg freezedetect ∩ silencedetect)
     — used to catch shorter pauses where the screen also isn't changing.
  3. Retake/false-start finder (Gemini text-only on the transcript).

Run via uvx:

    uvx --with google-genai --with static-ffmpeg python propose.py \
        --in "F:\\path\\input.mp4" --transcript transcript.json --out edl.json

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
                            silero: NN-based VAD, requires --with silero-vad.
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

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)


# ---------- ffmpeg detectors -----------------------------------------------

def detect_silences(inp: Path, db: float, min_s: float, quiet: bool) -> list[tuple[float, float]]:
    """Return list of (start, end) for silent intervals >= min_s."""
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner",
        "-i", str(inp),
        "-af", f"silencedetect=noise={db}dB:d={min_s:.3f}",
        "-f", "null", "-",
    ]
    if not quiet:
        print(f"[vedit/propose] ffmpeg silencedetect (noise={db}dB d={min_s}s)…", file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-2000:])
        raise RuntimeError("silencedetect failed")

    ranges: list[tuple[float, float]] = []
    current_start: float | None = None
    for line in proc.stderr.splitlines():
        m = re.search(r"silence_start:\s*([\d.]+)", line)
        if m:
            current_start = float(m.group(1))
            continue
        m = re.search(r"silence_end:\s*([\d.]+)", line)
        if m and current_start is not None:
            ranges.append((current_start, float(m.group(1))))
            current_start = None
    return ranges


def detect_silences_from_transcript(
    transcript: dict, min_s: float, total_s: float, quiet: bool,
) -> list[tuple[float, float]]:
    """Derive silence ranges from the Whisper transcript: a 'silence' is any
    span of total_s that contains no transcribed words for at least min_s.

    This sidesteps dB-threshold calibration entirely — Whisper's VAD already
    decided what's speech. Quiet narration that ffmpeg silencedetect would
    miss-classify as silence is correctly treated as speech here.
    """
    word_intervals: list[tuple[float, float]] = []
    for s in transcript["segments"]:
        for w in s.get("words") or []:
            word_intervals.append((float(w["start"]), float(w["end"])))
    word_intervals.sort()

    if not word_intervals:
        return [(0.0, total_s)] if total_s >= min_s else []

    silences: list[tuple[float, float]] = []
    if word_intervals[0][0] >= min_s:
        silences.append((0.0, word_intervals[0][0]))
    for i in range(len(word_intervals) - 1):
        gap_start = word_intervals[i][1]
        gap_end = word_intervals[i + 1][0]
        if gap_end - gap_start >= min_s:
            silences.append((gap_start, gap_end))
    if total_s - word_intervals[-1][1] >= min_s:
        silences.append((word_intervals[-1][1], total_s))

    if not quiet:
        print(f"[vedit/propose] transcript-VAD: {len(silences)} silence ranges "
              f"≥{min_s}s from {len(word_intervals)} words",
              file=sys.stderr)
    return silences


def detect_silences_silero(
    inp: Path, min_s: float, total_s: float, quiet: bool,
) -> list[tuple[float, float]]:
    """Detect silences using silero-vad (NN-based VAD).

    Speech timestamps are inverted to silence ranges, including any leading
    silence before the first speech and trailing silence after the last.
    Requires `silero-vad` on the uvx --with list (pulls torch).
    """
    try:
        from silero_vad import load_silero_vad, get_speech_timestamps, read_audio
    except ImportError:
        sys.stderr.write(
            "ERROR: silero-vad not installed.\n"
            "Run via: uvx --with silero-vad --with google-genai --with static-ffmpeg "
            "python propose.py --vad-engine silero ...\n"
        )
        sys.exit(2)

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav_path = f.name
    try:
        if not quiet:
            print(f"[vedit/propose] extracting 16kHz mono audio for silero-vad…",
                  file=sys.stderr)
        rc = subprocess.run(
            ["ffmpeg", "-y", "-nostdin", "-loglevel", "error",
             "-i", str(inp), "-ac", "1", "-ar", "16000", "-vn", wav_path],
            capture_output=True, text=True,
        )
        if rc.returncode != 0:
            sys.stderr.write(rc.stderr[-2000:])
            raise RuntimeError("audio extraction for silero failed")

        if not quiet:
            print(f"[vedit/propose] silero-vad…", file=sys.stderr)
        model = load_silero_vad(onnx=True)
        wav = read_audio(wav_path, sampling_rate=16000)
        speech = get_speech_timestamps(
            wav, model, sampling_rate=16000, return_seconds=True,
            min_silence_duration_ms=int(min_s * 1000),
        )

        silences: list[tuple[float, float]] = []
        prev_end = 0.0
        for s in speech:
            ss, se = float(s["start"]), float(s["end"])
            if ss - prev_end >= min_s:
                silences.append((prev_end, ss))
            prev_end = se
        if total_s - prev_end >= min_s:
            silences.append((prev_end, total_s))
        return silences
    finally:
        Path(wav_path).unlink(missing_ok=True)


def detect_freezes(
    inp: Path, noise: float, min_s: float, quiet: bool
) -> list[tuple[float, float]]:
    """Return list of (start, end) for static-screen intervals >= min_s.

    Uses ffmpeg freezedetect: marks a range frozen when consecutive frames
    differ by less than `noise` (mean abs pixel diff) for at least `min_s`.
    """
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner",
        "-i", str(inp),
        "-vf", f"freezedetect=n={noise}:d={min_s:.3f}",
        "-an", "-f", "null", "-",
    ]
    if not quiet:
        print(f"[vedit/propose] ffmpeg freezedetect (n={noise} d={min_s}s)…", file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-2000:])
        raise RuntimeError("freezedetect failed")

    starts: list[float] = []
    ends: list[float] = []
    for line in proc.stderr.splitlines():
        m = re.search(r"freeze_start:\s*([\d.]+)", line)
        if m:
            starts.append(float(m.group(1))); continue
        m = re.search(r"freeze_end:\s*([\d.]+)", line)
        if m:
            ends.append(float(m.group(1)))
    # If a freeze is still active at EOF there's no freeze_end — drop it.
    return list(zip(starts, ends))


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


def find_filler_with_gemini(
    segments: list[dict], model: str, quiet: bool,
) -> list[tuple[float, float, str]]:
    """Second LLM pass: filler/hedging/redundant content trims.

    Same wiring as retake-finder but with a different prompt and category label.
    """
    if "GEMINI_API_KEY" not in os.environ and "GOOGLE_API_KEY" not in os.environ:
        sys.stderr.write(
            "ERROR: GEMINI_API_KEY (or GOOGLE_API_KEY) not set in environment.\n"
        )
        sys.exit(2)
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        sys.stderr.write("ERROR: google-genai not installed.\n")
        sys.exit(2)

    text = "\n".join(
        f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in segments
    )
    prompt = FILLER_PROMPT.format(transcript=text)

    if not quiet:
        print(f"[vedit/propose] gemini filler-trim ({model}, "
              f"{len(text):,} chars of transcript)…", file=sys.stderr)

    client = genai.Client()
    schema = {
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
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.0,
        ),
    )
    raw = response.text or "[]"
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        sys.stderr.write(f"WARNING: filler pass returned non-JSON; treating as empty.\n")
        return []
    out: list[tuple[float, float, str]] = []
    for it in items:
        try:
            s = float(it["start"]); e = float(it["end"])
            if e > s:
                out.append((s, e, f"filler: {it.get('reason', '')[:80]}"))
        except (KeyError, ValueError, TypeError):
            continue
    if not quiet:
        print(f"[vedit/propose] filler-trim: {len(out)} trims proposed",
              file=sys.stderr)
    return out


def find_retakes_with_gemini(
    segments: list[dict], model: str, quiet: bool,
) -> list[tuple[float, float, str]]:
    if "GEMINI_API_KEY" not in os.environ and "GOOGLE_API_KEY" not in os.environ:
        sys.stderr.write(
            "ERROR: GEMINI_API_KEY (or GOOGLE_API_KEY) not set in environment.\n"
            "       Set it in Windows user env vars or pass --no-llm to skip.\n"
        )
        sys.exit(2)

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        sys.stderr.write(
            "ERROR: google-genai not installed.\n"
            "Run via: uvx --with google-genai --with static-ffmpeg python propose.py ...\n"
        )
        sys.exit(2)

    text = "\n".join(
        f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in segments
    )
    prompt = RETAKE_PROMPT.format(transcript=text)

    if not quiet:
        print(f"[vedit/propose] gemini retake-finder ({model}, "
              f"{len(text):,} chars of transcript)…", file=sys.stderr)

    client = genai.Client()
    schema = {
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
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.0,
        ),
    )
    raw = response.text or "[]"
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        sys.stderr.write(f"WARNING: Gemini returned non-JSON; treating as empty.\n  raw: {raw[:200]}\n")
        return []
    out: list[tuple[float, float, str]] = []
    for it in items:
        try:
            s = float(it["start"]); e = float(it["end"])
            if e > s:
                out.append((s, e, f"retake: {it.get('reason', '')[:80]}"))
        except (KeyError, ValueError, TypeError):
            continue
    return out


# ---------- merging --------------------------------------------------------

def intersect(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Interval-list intersection. Both inputs sorted, non-overlapping."""
    out: list[tuple[float, float]] = []
    i = j = 0
    while i < len(a) and j < len(b):
        s = max(a[i][0], b[j][0])
        e = min(a[i][1], b[j][1])
        if s < e:
            out.append((s, e))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def words_inside_span(transcript_segs: list[dict], a: float, b: float) -> str:
    """Extract words whose midpoint falls within [a, b] using word-level
    timestamps; falls back to whole overlapping segments if no words available."""
    out: list[str] = []
    saw_words = False
    for s in transcript_segs:
        if s["end"] <= a or s["start"] >= b:
            continue
        if s.get("words"):
            saw_words = True
            for w in s["words"]:
                mid = (w["start"] + w["end"]) / 2
                if a <= mid <= b:
                    out.append(w["word"])
    if saw_words:
        return "".join(out).strip()
    return " ".join(s["text"] for s in transcript_segs
                    if s["end"] > a and s["start"] < b).strip()


def extract_frame_jpeg(inp: Path, t_seconds: float) -> bytes:
    """Grab a single frame at t_seconds, encoded as JPEG (q=5 ≈ 80% quality)."""
    proc = subprocess.run(
        ["ffmpeg", "-y", "-nostdin", "-loglevel", "error",
         "-ss", f"{t_seconds:.3f}", "-i", str(inp),
         "-frames:v", "1", "-f", "image2", "-vcodec", "mjpeg", "-q:v", "5",
         "-"],
        capture_output=True, check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(
            f"frame extract failed at {t_seconds:.3f}s: "
            f"rc={proc.returncode} stderr={proc.stderr[-300:]!r}"
        )
    return proc.stdout


def validate_retakes_with_frames(
    inp: Path,
    retake_cuts: list[tuple[float, float, str]],
    transcript_segs: list[dict],
    model: str,
    quiet: bool,
) -> list[tuple[float, float, str]]:
    """Second pass: for each retake cut, ask Gemini multimodal whether the
    screen frame at the cut midpoint is consistent with a SPEECH retake.
    Drops cuts the model classifies as content (action retry, screen
    reaction, hypothetical comparison)."""
    if not retake_cuts:
        return retake_cuts

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        sys.stderr.write("ERROR: google-genai not installed (need it for --validate-retakes).\n")
        sys.exit(2)

    client = genai.Client()
    schema = {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "enum": ["cut", "keep"]},
            "why":    {"type": "STRING"},
        },
        "required": ["action", "why"],
    }

    if not quiet:
        print(f"[vedit/propose] validating {len(retake_cuts)} retakes with frame check…",
              file=sys.stderr)

    valid: list[tuple[float, float, str]] = []
    for s, e, reason in retake_cuts:
        cut_text = words_inside_span(transcript_segs, s, e)
        mid = (s + e) / 2
        try:
            jpeg = extract_frame_jpeg(inp, mid)
        except Exception as ex:
            if not quiet:
                print(f"  ?  {s:.1f}-{e:.1f}: frame extract failed ({ex}); keeping cut",
                      file=sys.stderr)
            valid.append((s, e, reason))
            continue

        prompt = (
            f'In a screen-recorded software tutorial at time {mid:.1f}s, '
            f'the speaker says:\n'
            f'  "{cut_text}"\n\n'
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
            resp = client.models.generate_content(
                model=model,
                contents=[
                    types.Part.from_bytes(data=jpeg, mime_type="image/jpeg"),
                    prompt,
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema,
                    temperature=0.0,
                ),
            )
            d = json.loads(resp.text or "{}")
            if d.get("action") == "cut":
                valid.append((s, e, f"{reason} [frame ok]"))
                if not quiet:
                    print(f"  ✓ {s:6.1f}-{e:6.1f} keep cut: {d.get('why','')[:80]}",
                          file=sys.stderr)
            else:
                if not quiet:
                    print(f"  ✗ {s:6.1f}-{e:6.1f} drop cut: {d.get('why','')[:80]}",
                          file=sys.stderr)
        except Exception as ex:
            if not quiet:
                print(f"  ?  {s:.1f}-{e:.1f}: API error ({ex}); keeping cut",
                      file=sys.stderr)
            valid.append((s, e, reason))

    if not quiet:
        print(f"[vedit/propose] frame check: {len(retake_cuts)} → {len(valid)} retakes",
              file=sys.stderr)
    return valid


def flatten_words(transcript: dict) -> list[dict]:
    """All words from all transcript segments, in time order."""
    words: list[dict] = []
    for s in transcript["segments"]:
        for w in s.get("words") or []:
            words.append({"start": float(w["start"]), "end": float(w["end"])})
    words.sort(key=lambda w: w["start"])
    return words


def snap_to_silence_gap(t: float, words: list[dict], max_drift: float) -> float:
    """Find the midpoint of the silence gap closest to t (within max_drift).
    Returns t unchanged if no gap is close enough.

    A 'silence gap' is the space between word i's end and word i+1's start,
    where end < start. Cut boundaries snapped to gap midpoints don't clip
    audible word edges.
    """
    if not words:
        return t
    best_mid: float | None = None
    best_dist = max_drift + 1
    for i in range(len(words) - 1):
        gap_start = words[i]["end"]
        gap_end = words[i + 1]["start"]
        if gap_end <= gap_start + 0.005:
            continue
        gap_mid = (gap_start + gap_end) / 2
        dist = abs(gap_mid - t)
        if dist < best_dist:
            best_dist = dist
            best_mid = gap_mid
    if best_mid is not None and best_dist <= max_drift:
        return best_mid
    return t


def word_snap_cuts(
    cuts: list[tuple[float, float, str]],
    words: list[dict],
    max_drift: float,
) -> list[tuple[float, float, str]]:
    snapped: list[tuple[float, float, str]] = []
    for s, e, r in cuts:
        ns = snap_to_silence_gap(s, words, max_drift)
        ne = snap_to_silence_gap(e, words, max_drift)
        if ne > ns:
            snapped.append((ns, ne, r))
    return snapped


def detect_audio_events(
    inp: Path,
    transcript: dict,
    quiet: bool,
    min_dur: float = 0.1,
    max_dur: float = 2.0,
    silence_db: float = -40.0,
) -> list[tuple[float, float, str]]:
    """Find non-speech vocalizations: audible sounds in inter-word gaps.

    Strategy: any gap between consecutive transcript words that (a) falls in
    [min_dur, max_dur] and (b) is not fully covered by silence detections
    must contain audible non-speech content — a throat clear, uh/um, or mouth
    sound. Runs one ffmpeg silencedetect call (min 0.05 s, same threshold as
    the main pass) to get the silence map; gap audio presence is then pure
    interval arithmetic, no per-gap ffmpeg calls.
    """
    all_words: list[tuple[float, float]] = []
    for s in transcript["segments"]:
        for w in s.get("words") or []:
            all_words.append((float(w["start"]), float(w["end"])))
    all_words.sort()

    if len(all_words) < 2:
        return []

    # Build candidate inter-word gaps in the target duration window
    gaps: list[tuple[float, float]] = []
    for i in range(len(all_words) - 1):
        gap_s = all_words[i][1]
        gap_e = all_words[i + 1][0]
        if min_dur <= gap_e - gap_s <= max_dur:
            gaps.append((gap_s, gap_e))

    if not gaps:
        return []

    # One ffmpeg silencedetect pass with a very short min-duration to build
    # a complete silence map. We deliberately use min_s=0.05 so even brief
    # silent patches are captured (we're looking for ANY audible content).
    silences = sorted(detect_silences(inp, silence_db, 0.05, quiet=True))

    def is_fully_silent(a: float, b: float) -> bool:
        """True iff every point in [a, b] falls in some silence interval."""
        cursor = a
        for ss, se in silences:
            if se <= a:
                continue
            if ss > cursor + 0.015:
                return False
            cursor = max(cursor, se)
            if cursor >= b - 0.015:
                return True
        return cursor >= b - 0.015

    events: list[tuple[float, float, str]] = []
    for gap_s, gap_e in gaps:
        if not is_fully_silent(gap_s, gap_e):
            dur = gap_e - gap_s
            if dur < 0.3:
                hint = "mouth_sound"
            elif dur < 0.8:
                hint = "filler_sound"
            else:
                hint = "throat_clear"
            events.append((gap_s, gap_e, f"audio_event: {hint}"))

    if not quiet:
        print(
            f"[vedit/propose] audio-events: {len(events)} vocalizations "
            f"({sum(1 for _, _, r in events if 'throat' in r)} throat_clear, "
            f"{sum(1 for _, _, r in events if 'filler' in r)} filler_sound, "
            f"{sum(1 for _, _, r in events if 'mouth' in r)} mouth_sound)",
            file=sys.stderr,
        )
    return events


def detect_screen_activity(
    inp: Path,
    interval_s: float = 2.0,
    change_threshold: float = 8.0,
    quiet: bool = False,
) -> list[float]:
    """Extract one grayscale frame per interval_s (128×72), compare consecutive
    frames via mean absolute pixel difference.  Returns timestamps of frames where
    the change exceeds change_threshold (0–255 scale).

    A threshold of ~8 catches UI updates and text changes while ignoring codec
    noise on static screen content.  No extra dependencies — uses ffmpeg rawvideo
    pipe and plain Python byte arithmetic.
    """
    W, H = 128, 72
    frame_bytes = W * H
    step = max(1, frame_bytes // 1024)  # subsample to ~1024 pixels for speed

    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(inp),
        "-vf", f"fps={1.0 / interval_s:.4f},scale={W}:{H}:flags=fast_bilinear,format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    changes: list[float] = []
    prev: bytes | None = None
    frame_idx = 0
    try:
        while True:
            chunk = proc.stdout.read(frame_bytes)
            if len(chunk) < frame_bytes:
                break
            if prev is not None:
                diff = (
                    sum(abs(chunk[i] - prev[i]) for i in range(0, frame_bytes, step))
                    * step / frame_bytes
                )
                if diff > change_threshold:
                    changes.append(frame_idx * interval_s)
            prev = chunk
            frame_idx += 1
    finally:
        proc.stdout.close()
        proc.wait()

    if not quiet:
        print(
            f"[vedit/propose] screen-check: {frame_idx} frames × {interval_s}s, "
            f"{len(changes)} changes detected",
            file=sys.stderr,
        )
    return changes


def silence_has_screen_change(
    s: float, e: float, changes: list[float], interval_s: float,
) -> bool:
    """True iff any screen-change timestamp falls within [s, e].

    A change recorded at timestamp t means the screen differed between frames at
    t−interval_s and t, so we extend the window by ±interval_s on each side.
    """
    for t in changes:
        if s - interval_s < t < e + interval_s:
            return True
    return False


def merge_cuts(cuts: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    if not cuts:
        return []
    cuts = sorted(cuts, key=lambda x: x[0])
    merged: list[tuple[float, float, str]] = [cuts[0]]
    for s, e, r in cuts[1:]:
        ls, le, lr = merged[-1]
        if s <= le + 0.001:
            new_reason = lr if r == lr or r in lr else f"{lr}+{r}"
            merged[-1] = (ls, max(le, e), new_reason)
        else:
            merged.append((s, e, r))
    return merged


def build_edl(
    cuts: list[tuple[float, float, str]],
    total: float,
    source: str,
    speed_segs: list[tuple[float, float, str, float]] | None = None,
) -> dict:
    """Build EDL from cuts (keep=False) and optional speed_segs (keep=True, speed>1).

    All boundary points from both cuts and speed_segs are collected first, then
    the timeline is split into atomic intervals so that a speed-up zone can never
    bleed into adjacent speech.  Consecutive same-type intervals are merged back.
    """
    cuts = [(max(0.0, s), min(total, e), r) for s, e, r in cuts if e > s]

    speed_map: list[tuple[float, float, float]] = []
    if speed_segs:
        for ss, se, _sr, spd in speed_segs:
            if se > ss:
                speed_map.append((max(0.0, ss), min(total, se), spd))

    # Gather every boundary so each interval is unambiguously one type
    pts: set[float] = {0.0, total}
    for s, e, _ in cuts:
        pts.add(s); pts.add(e)
    for ss, se, _ in speed_map:
        pts.add(ss); pts.add(se)
    sorted_pts = sorted(pts)

    def interval_cut(a: float, b: float) -> tuple[bool, str]:
        mid = (a + b) / 2
        for cs, ce, cr in cuts:
            if cs <= mid < ce:
                return True, cr
        return False, ""

    def interval_speed(a: float, b: float) -> float:
        mid = (a + b) / 2
        for ss, se, spd in speed_map:
            if ss <= mid < se:
                return spd
        return 1.0

    # Build atomic segments
    raw: list[dict] = []
    for i in range(len(sorted_pts) - 1):
        a, b = sorted_pts[i], sorted_pts[i + 1]
        if b - a < 0.001:
            continue
        is_cut, reason = interval_cut(a, b)
        if is_cut:
            raw.append({"start": a, "end": b, "keep": False, "reason": reason})
        else:
            spd = interval_speed(a, b)
            seg: dict = {"start": a, "end": b, "keep": True, "reason": ""}
            if spd != 1.0:
                seg["speed"] = spd
            raw.append(seg)

    # Merge consecutive intervals with identical keep/speed/reason
    segs: list[dict] = []
    for seg in raw:
        if (
            segs
            and segs[-1]["keep"] == seg["keep"]
            and segs[-1].get("speed", 1.0) == seg.get("speed", 1.0)
            and segs[-1]["reason"] == seg["reason"]
        ):
            segs[-1]["end"] = seg["end"]
        else:
            segs.append(dict(seg))

    for seg in segs:
        seg["start"] = round(seg["start"], 3)
        seg["end"] = round(seg["end"], 3)

    return {
        "source": source,
        "duration_s": round(total, 3),
        "version": 1,
        "segments": segs,
    }


# ---------- main -----------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Propose cuts as an EDL.")
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--transcript", default=None,
                    help="Required unless --no-llm")
    ap.add_argument("--out", required=True)
    ap.add_argument("--silence-db", type=float, default=-40.0)
    ap.add_argument("--silence-min-s", type=float, default=3.0)
    ap.add_argument("--silence-max-s", type=float, default=0.0,
                    help="Cap silence-cut duration. 0 (default) disables.")
    ap.add_argument("--static-min-s", type=float, default=5.0)
    ap.add_argument("--freeze-noise", type=float, default=0.003)
    ap.add_argument("--static", action="store_true",
                    help="Enable freeze+silence cuts. OFF by default (see --help).")
    ap.add_argument("--vad-engine", default="silence",
                    choices=["silence", "silero", "transcript"])
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--llm-model", default="gemini-2.5-flash")
    ap.add_argument("--validate-retakes", action="store_true",
                    help="Multimodal frame-check on each retake. Drops false positives.")
    ap.add_argument("--trim-filler", action="store_true",
                    help="Additional Gemini pass for filler/hedging/redundant trims.")
    ap.add_argument("--trim-audio-events", action="store_true",
                    help="Detect non-speech vocalizations in inter-word gaps (throat clears, uh/um, mouth sounds).")
    ap.add_argument("--screen-check", action="store_true",
                    help="Preserve silences with screen activity; speed up long static silences.")
    ap.add_argument("--screen-interval-s", type=float, default=2.0,
                    help="Frame sample interval for screen-check (seconds). Default: 2.0.")
    ap.add_argument("--screen-change-thr", type=float, default=8.0,
                    help="Mean pixel diff (0-255) to call a frame a screen change. Default: 8.")
    ap.add_argument("--speed-up-threshold-s", type=float, default=4.0,
                    help="Static-silence spans >= this are sped up instead of cut. Default: 4.0.")
    ap.add_argument("--speed-up-factor", type=float, default=5.0,
                    help="Speed multiplier for static-silence spans. Default: 5.0.")
    ap.add_argument("--speed-up-max-s", type=float, default=0.0,
                    help="Keep static silences longer than this at 1x (demo payoff). 0=disabled.")
    ap.add_argument("--keep-until", type=float, default=0.0,
                    help="Hard-cut everything after this source timestamp (seconds). 0=disabled.")
    ap.add_argument("--keep-padding-s", type=float, default=0.25)
    ap.add_argument("--no-word-snap", action="store_true")
    ap.add_argument("--word-snap-drift-s", type=float, default=0.4)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    inp = Path(args.inp)
    out = Path(args.out)
    if not inp.exists():
        sys.stderr.write(f"ERROR: input not found: {inp}\n"); return 2

    try:
        from _ffmpeg import ensure_ffmpeg, probe_duration_seconds
    except ImportError:
        sys.path.insert(0, str(Path(__file__).parent))
        from _ffmpeg import ensure_ffmpeg, probe_duration_seconds
    ensure_ffmpeg()

    total = probe_duration_seconds(str(inp))
    if not args.quiet:
        print(f"[vedit/propose] duration: {total:.1f}s", file=sys.stderr)

    # Load transcript up front (used for both LLM retake-finder and word-snap)
    transcript_data: dict | None = None
    if args.transcript:
        tpath = Path(args.transcript)
        if not tpath.exists():
            sys.stderr.write(f"ERROR: transcript not found: {tpath}\n"); return 2
        transcript_data = json.loads(tpath.read_text(encoding="utf-8"))

    t0 = time.monotonic()

    # 1. Pure silence (>= silence_min_s, <= silence_max_s if cap is set)
    if args.vad_engine == "silero":
        silences_long = detect_silences_silero(inp, args.silence_min_s, total, args.quiet)
        silences_short = detect_silences_silero(inp, 1.0, total, args.quiet) \
            if args.static else []
    elif args.vad_engine == "transcript":
        if transcript_data is None:
            sys.stderr.write("ERROR: --vad-engine transcript requires --transcript\n"); return 2
        silences_long = detect_silences_from_transcript(
            transcript_data, args.silence_min_s, total, args.quiet,
        )
        silences_short = detect_silences_from_transcript(
            transcript_data, 1.0, total, args.quiet,
        ) if args.static else []
    else:
        silences_long = detect_silences(inp, args.silence_db, args.silence_min_s, args.quiet)
        silences_short = detect_silences(inp, args.silence_db, 1.0, args.quiet) \
            if args.static else []

    # Screen-change detection (run once here; result used during silence filtering)
    screen_changes: list[float] = []
    if args.screen_check:
        screen_changes = detect_screen_activity(
            inp, args.screen_interval_s, args.screen_change_thr, args.quiet,
        )

    cuts: list[tuple[float, float, str]] = []
    speed_segs: list[tuple[float, float, str, float]] = []
    skipped_long = screen_active_skipped = demo_payoff_kept = 0

    for s, e in silences_long:
        if args.silence_max_s > 0 and (e - s) > args.silence_max_s:
            skipped_long += 1
            continue
        if args.screen_check and silence_has_screen_change(
            s, e, screen_changes, args.screen_interval_s,
        ):
            screen_active_skipped += 1
            continue
        if args.screen_check and (e - s) >= args.speed_up_threshold_s:
            if args.speed_up_max_s > 0 and (e - s) > args.speed_up_max_s:
                # Long static silence — demo is the point; keep at 1x
                demo_payoff_kept += 1
            else:
                speed_segs.append((
                    s, e,
                    f"screen_inactive>{args.speed_up_threshold_s:.0f}s",
                    args.speed_up_factor,
                ))
        else:
            cuts.append((s, e, f"silence>{args.silence_min_s:.0f}s"))

    if skipped_long and not args.quiet:
        print(f"[vedit/propose] skipped {skipped_long} long silences "
              f"(>{args.silence_max_s:.0f}s) — likely demo content, not dead time",
              file=sys.stderr)
    if screen_active_skipped and not args.quiet:
        print(f"[vedit/propose] screen-check: {screen_active_skipped} silence(s) preserved "
              f"(screen was active during that span)", file=sys.stderr)
    if demo_payoff_kept and not args.quiet:
        print(f"[vedit/propose] screen-check: {demo_payoff_kept} long-silence span(s) kept "
              f"at 1x (demo payoff, >{args.speed_up_max_s:.0f}s)", file=sys.stderr)
    if speed_segs and not args.quiet:
        spd_total = sum(e - s for s, e, _, _ in speed_segs)
        print(f"[vedit/propose] screen-check: {len(speed_segs)} static-silence span(s) → "
              f"{args.speed_up_factor:.0f}x speed-up ({spd_total:.1f}s source → "
              f"{spd_total / args.speed_up_factor:.1f}s on timeline)", file=sys.stderr)

    # 2. Freeze ∩ shorter silence — only when --static is on
    if args.static:
        freezes = detect_freezes(inp, args.freeze_noise, 2.0, args.quiet)
        overlap = intersect(
            sorted(silences_short), sorted(freezes),
        )
        for s, e in overlap:
            if e - s >= args.static_min_s and (
                args.silence_max_s <= 0 or (e - s) <= args.silence_max_s
            ):
                cuts.append((s, e, "static+silent"))

    # 3. Gemini retake finder (text-only first pass)
    if not args.no_llm:
        if transcript_data is None:
            sys.stderr.write("ERROR: --transcript required unless --no-llm\n"); return 2
        retake_cuts = find_retakes_with_gemini(
            transcript_data["segments"], args.llm_model, args.quiet,
        )
        # Optional: multimodal second pass to validate each retake against the screen
        if retake_cuts and args.validate_retakes:
            retake_cuts = validate_retakes_with_frames(
                inp, retake_cuts, transcript_data["segments"],
                args.llm_model, args.quiet,
            )
        cuts.extend(retake_cuts)

    # 3b. Optional filler/hedging trim pass (different prompt, content cuts)
    if args.trim_filler:
        if transcript_data is None:
            sys.stderr.write("ERROR: --transcript required for --trim-filler\n"); return 2
        filler_cuts = find_filler_with_gemini(
            transcript_data["segments"], args.llm_model, args.quiet,
        )
        cuts.extend(filler_cuts)

    # 3c. Non-speech vocalization detection (audible but not transcribed as words)
    if args.trim_audio_events:
        if transcript_data is None:
            sys.stderr.write("ERROR: --transcript required for --trim-audio-events\n"); return 2
        event_cuts = detect_audio_events(inp, transcript_data, args.quiet)
        cuts.extend(event_cuts)

    # 4a. keep-until: hard-cut the tail after the speaker's sign-off
    if args.keep_until > 0 and args.keep_until < total:
        cuts.append((args.keep_until, total, "keep_until: tail after sign-off"))
        if not args.quiet:
            print(
                f"[vedit/propose] keep-until: dropping tail {args.keep_until:.1f}s–"
                f"{total:.1f}s ({total - args.keep_until:.1f}s)",
                file=sys.stderr,
            )

    # 4b. Cleanup boundaries: word-snap when transcript is available, else padding.
    # Audio-event cuts are already at inter-word boundaries by construction —
    # applying word-snap to them can pull cut ends into the adjacent word, so
    # they are excluded from the snap pass and re-added afterwards.
    if transcript_data is not None and not args.no_word_snap:
        words = flatten_words(transcript_data)
        audio_event_cuts = [c for c in cuts if c[2].startswith("audio_event:")]
        snap_cuts = [c for c in cuts if not c[2].startswith("audio_event:")]
        before = len(snap_cuts)
        snap_cuts = word_snap_cuts(snap_cuts, words, args.word_snap_drift_s)
        cuts = snap_cuts + audio_event_cuts
        if not args.quiet:
            print(f"[vedit/propose] word-snap: {before} cuts → {len(snap_cuts)} "
                  f"after snapping to silence gaps within "
                  f"±{args.word_snap_drift_s}s",
                  file=sys.stderr)
    elif args.keep_padding_s > 0:
        pad = args.keep_padding_s
        cuts = [(s + pad, e - pad, r) for s, e, r in cuts if e - s > 2 * pad]

    cuts = merge_cuts(cuts)
    edl = build_edl(cuts, total, str(inp), speed_segs=speed_segs or None)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(edl, ensure_ascii=False, indent=2), encoding="utf-8")

    n_cut = sum(1 for s in edl["segments"] if not s["keep"])
    cut_dur = sum(s["end"] - s["start"] for s in edl["segments"] if not s["keep"])
    n_spd = sum(1 for s in edl["segments"] if s.get("speed", 1.0) != 1.0)
    spd_src = sum(s["end"] - s["start"] for s in edl["segments"] if s.get("speed", 1.0) != 1.0)
    spd_tl = sum(
        (s["end"] - s["start"]) / s["speed"]
        for s in edl["segments"] if s.get("speed", 1.0) != 1.0
    )
    if not args.quiet:
        spd_note = (
            f", {n_spd} speed-up span(s) ({spd_src:.1f}s → {spd_tl:.1f}s at "
            f"{args.speed_up_factor:.0f}x)" if n_spd else ""
        )
        print(f"[vedit/propose] done in {time.monotonic() - t0:.1f}s. "
              f"{n_cut} cut spans, {cut_dur:.1f}s "
              f"({100 * cut_dur / total:.1f}% of total){spd_note} → {out}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
