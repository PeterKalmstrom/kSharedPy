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
    --quiet                 No progress lines on stderr (warnings and errors still show).

Output: the EDL file only - stdout stays empty; progress goes to stderr.
Modules beside this script: _silence.py (kSilenceDetector), _gemini_cuts.py (kGeminiCuts, the prompts),
_cut_editor.py (kCutEditor, the interval arithmetic and the EDL).
"""
# /// script
# requires-python = ">=3.10"
# dependencies = ["google-genai", "static-ffmpeg"]
# ///

import argparse
import time
from pathlib import Path
from typing import Any

from _cut_editor import kCutEditor
from _gemini_cuts import kGeminiCuts
from _silence import kSilenceDetector
from _vedit_data import kVeditData
from kConsole import kConsole
from kFfmpeg import kFfmpeg
from kJsonFile import kJsonFile
from kShared import ToolInputException, kRun, kS, kToolException


class kProposeApp:
    """Command line: wires the detectors (kSilenceDetector), the Gemini passes (kGeminiCuts) and the cut
    arithmetic (kCutEditor) into one EDL."""

    @staticmethod
    def ParseArgs() -> argparse.Namespace | None:
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

    def DetectSilences(self, Input: Path, Total: float, TranscriptData: dict[str, Any] | None
                       ) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        """Return (long_silences, short_silences) with the chosen VAD engine; short ones only with --static."""
        if kS.ErrorMode:
            return ([], [])
        try:
            Args = self.Args
            if Args.vad_engine == "silero":
                Long = kSilenceDetector.Silero(Input, Args.silence_min_s, Total)
                Short = kSilenceDetector.Silero(Input, 1.0, Total) if Args.static else []
            elif Args.vad_engine == "transcript":
                if TranscriptData is None:
                    raise ToolInputException("--vad-engine transcript requires --transcript")
                Long = kSilenceDetector.FromTranscript(TranscriptData, Args.silence_min_s, Total)
                Short = kSilenceDetector.FromTranscript(TranscriptData, 1.0, Total) if Args.static else []
            else:
                Long = kSilenceDetector.Ffmpeg(Input, Args.silence_db, Args.silence_min_s)
                Short = kSilenceDetector.Ffmpeg(Input, Args.silence_db, 1.0) if Args.static else []
            return (Long, Short)
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, "kProposeApp.DetectSilences")
            return ([], [])

    def ClassifySilences(self, SilencesLong: list[tuple[float, float]], ScreenChanges: list[float]
                         ) -> tuple[list[tuple[float, float, str]], list[tuple[float, float, str, float]]]:
        """Return (cuts, speed_segments): each long silence is skipped, kept, sped up or cut (kCutEditor), and the
        decisions are reported as progress."""
        if kS.ErrorMode:
            return ([], [])
        try:
            Args = self.Args
            Cuts, SpeedSegments, Counts = kCutEditor.ClassifySilences(
                SilencesLong, ScreenChanges, Args.silence_min_s, Args.silence_max_s, Args.screen_check,
                Args.screen_interval_s, Args.speed_up_threshold_s, Args.speed_up_factor, Args.speed_up_max_s)
            if kS.ErrorMode:
                return ([], [])
            if Counts["SkippedLong"]:
                kConsole.Progress(f"[vedit/propose] skipped {Counts['SkippedLong']} long silences "
                                  f"(>{Args.silence_max_s:.0f}s) — likely demo content, not dead time")
            if Counts["ScreenActiveSkipped"]:
                kConsole.Progress(f"[vedit/propose] screen-check: {Counts['ScreenActiveSkipped']} silence(s) preserved "
                                  f"(screen was active during that span)")
            if Counts["DemoPayoffKept"]:
                kConsole.Progress(f"[vedit/propose] screen-check: {Counts['DemoPayoffKept']} long-silence span(s) kept "
                                  f"at 1x (demo payoff, >{Args.speed_up_max_s:.0f}s)")
            if SpeedSegments:
                SpeedTotal = sum(Segment[1] - Segment[0] for Segment in SpeedSegments)
                kConsole.Progress(f"[vedit/propose] screen-check: {len(SpeedSegments)} static-silence span(s) → "
                                  f"{Args.speed_up_factor:.0f}x speed-up ({SpeedTotal:.1f}s source → "
                                  f"{SpeedTotal / Args.speed_up_factor:.1f}s on timeline)")
            return (Cuts, SpeedSegments)
        except Exception as e:
            kS.GlobalErrorHandler(e, "kProposeApp.ClassifySilences")
            return ([], [])

    def CleanBoundaries(self, Cuts: list[tuple[float, float, str]],
                        TranscriptData: dict[str, Any] | None) -> list[tuple[float, float, str]]:
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
                kConsole.Progress(f"[vedit/propose] word-snap: {Before} cuts → {len(SnapCuts)} "
                                  f"after snapping to silence gaps within ±{Args.word_snap_drift_s}s")
                return SnapCuts + AudioEventCuts
            if Args.keep_padding_s > 0:
                return kCutEditor.PadCuts(Cuts, Args.keep_padding_s)
            return Cuts
        except Exception as e:
            kS.GlobalErrorHandler(e, "kProposeApp.CleanBoundaries")
            return []

    def Summarize(self, Edl: dict[str, Any], Total: float, Out: Path, StartTime: float) -> None:
        """The closing progress line: cut spans, cut share and speed-ups."""
        if kS.ErrorMode:
            return
        try:
            Segments = Edl["segments"]
            CutCount = sum(1 for Segment in Segments if not Segment["keep"])
            CutDuration = sum(Segment["end"] - Segment["start"] for Segment in Segments if not Segment["keep"])
            Sped = [Segment for Segment in Segments if Segment.get("speed", 1.0) != 1.0]
            SpeedSource = sum(Segment["end"] - Segment["start"] for Segment in Sped)
            SpeedTimeline = sum((Segment["end"] - Segment["start"]) / Segment["speed"] for Segment in Sped)
            SpeedNote = (
                f", {len(Sped)} speed-up span(s) ({SpeedSource:.1f}s → {SpeedTimeline:.1f}s at "
                f"{self.Args.speed_up_factor:.0f}x)" if Sped else ""
            )
            kConsole.Progress(f"[vedit/propose] done in {time.monotonic() - StartTime:.1f}s. "
                              f"{CutCount} cut spans, {CutDuration:.1f}s "
                              f"({100 * CutDuration / Total:.1f}% of total){SpeedNote} → {Out}")
        except Exception as e:
            kS.GlobalErrorHandler(e, "kProposeApp.Summarize")
            return

    def Run(self) -> int:
        if kS.ErrorMode:
            return 1
        try:
            Args = kProposeApp.ParseArgs()
            if kS.ErrorMode or Args is None:
                return 1
            self.Args = Args
            kConsole.Quiet = Args.quiet
            Input = Path(Args.inp)
            Out = Path(Args.out)
            if not Input.exists():
                raise ToolInputException(f"input not found: {Input}")
            kFfmpeg.Ensure()
            Total = kFfmpeg.ProbeDurationSeconds(Input)
            if kS.ErrorMode:
                return 1
            kConsole.Progress(f"[vedit/propose] duration: {Total:.1f}s")

            # Load the transcript up front (used for both the LLM retake finder and word-snap)
            TranscriptData: dict[str, Any] | None = None
            if Args.transcript:
                TranscriptData = kVeditData.LoadTranscript(Args.transcript)
                if kS.ErrorMode:
                    return 1
            StartTime = time.monotonic()

            # 1. Pure silence (>= silence_min_s, <= silence_max_s if the cap is set)
            SilencesLong, SilencesShort = self.DetectSilences(Input, Total, TranscriptData)
            # Screen-change detection (run once here; the result is used during silence filtering)
            ScreenChanges: list[float] = []
            if Args.screen_check:
                ScreenChanges = kSilenceDetector.ScreenActivity(Input, Args.screen_interval_s, Args.screen_change_thr)
            Cuts, SpeedSegments = self.ClassifySilences(SilencesLong, ScreenChanges)

            # 2. Freeze ∩ shorter silence — only when --static is on
            if Args.static:
                Freezes = kSilenceDetector.Freezes(Input, Args.freeze_noise, 2.0)
                Cuts.extend(kCutEditor.StaticCuts(SilencesShort, Freezes, Args.static_min_s, Args.silence_max_s))

            # 3. Gemini retake finder (text-only first pass)
            if not Args.no_llm:
                if TranscriptData is None:
                    raise ToolInputException("--transcript required unless --no-llm")
                RetakeCuts = kGeminiCuts.FindRetakes(TranscriptData["segments"], Args.llm_model)
                # Optional: multimodal second pass to validate each retake against the screen
                if RetakeCuts and Args.validate_retakes:
                    RetakeCuts = kGeminiCuts.ValidateRetakes(Input, RetakeCuts, TranscriptData["segments"],
                                                             Args.llm_model)
                Cuts.extend(RetakeCuts)

            # 3b. Optional filler/hedging trim pass (different prompt, content cuts)
            if Args.trim_filler:
                if TranscriptData is None:
                    raise ToolInputException("--transcript required for --trim-filler")
                Cuts.extend(kGeminiCuts.FindFiller(TranscriptData["segments"], Args.llm_model))

            # 3c. Non-speech vocalization detection (audible but not transcribed as words)
            if Args.trim_audio_events:
                if TranscriptData is None:
                    raise ToolInputException("--transcript required for --trim-audio-events")
                Cuts.extend(kCutEditor.DetectAudioEvents(Input, TranscriptData))

            # 4a. keep-until: hard-cut the tail after the speaker's sign-off
            if Args.keep_until > 0 and Args.keep_until < Total:
                Cuts.append((Args.keep_until, Total, "keep_until: tail after sign-off"))
                kConsole.Progress(f"[vedit/propose] keep-until: dropping tail {Args.keep_until:.1f}s–"
                                  f"{Total:.1f}s ({Total - Args.keep_until:.1f}s)")

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
