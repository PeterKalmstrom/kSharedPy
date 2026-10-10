"""
Batch video generation via Google Veo 3.1 (Gemini API).

Generates one or many short videos from a JSON spec or inline prompt. Each
video is saved to a file path you supply. Mirrors the API of
`kSharedPy/gemini/batch_image_gen.py` for video instead of stills.

Usage:
    # Single video, inline
    python batch_video_gen.py \\
        --prompt "Cinematic side view of dominoes falling..." \\
        --out C:\\path\\to\\domino.mp4

    # Batch from JSON spec
    python batch_video_gen.py --spec spec.json

    # Spec file format (list of {prompt, out, model?, resolution?,
    # negative_prompt?, image?, mute?}):
    [
      {"prompt": "...", "out": "C:\\\\videos\\\\intro.mp4"},
      {"prompt": "...", "out": "C:\\\\videos\\\\reveal.mp4", "model": "veo-3.1-fast-generate-preview"}
    ]

Run via uvx (the google-genai SDK is installed on demand):
    GEMINI_API_KEY="..." uvx --with google-genai python batch_video_gen.py [args]

Environment:
    GEMINI_API_KEY - required. Get from https://aistudio.google.com/apikey

Models (most expensive last):
    veo-3.1-fast-generate-preview   - fastest, cheapest, lower quality
    veo-3.1-generate-preview         - default; balanced
    veo-3.1-pro-generate-preview     - highest quality, slower, costlier

Output: 8-second 720p MP4 with native audio, typically 1-10 MB per clip.
Generation time: ~60 seconds per clip including polling.

Exit codes and errors (the kShared error pattern):
    0  every job saved its video
    1  at least one job was refused by the API (see below), google-genai is not
       installed, or an unexpected error was reported (the run halts on it)
    2  bad input: missing --prompt/--spec, --prompt without --out, GEMINI_API_KEY
       not set, spec file missing or malformed, a starting-frame --image / "image"
       that does not exist, or --mute / "mute" without ffmpeg (PATH or static-ffmpeg)
    4  an unexpected error whose report waits for the user's yes
       (stderr: "ERROR-REPORT-PENDING: <file>")
    Batch choice: a refusal BY THE API for one job (google.genai APIError - e.g.
    the free-tier 429 RESOURCE_EXHAUSTED, a 400 - or a finished operation with no
    video, e.g. filtered by safety) is an expected per-job outcome: "ERROR: ..."
    goes to stderr (also with --quiet), the other jobs still run, and the exit
    code is 1. Anything else (network/DNS failure, ffmpeg failing, an unwritable
    output path, a bug) is unexpected: it is reported and the remaining jobs are
    skipped. All jobs are validated (spec, image files, ffmpeg) before the first
    paid call.

Prompt tips:
    - Keep prompts to 3-5 sentences. Long rule lists degrade results.
    - Veo trains on natural cinematic descriptions, not specifications.
    - Specify direction of motion explicitly: "moves from left to right".
    - "No text, no logos, no captions" to avoid baked-in text.

Known limits - don't waste reprompts:
    - Sequential rigid-body chain reactions (dominoes, Newton's cradle) - fail.
    - Counting (exactly N objects, ordered sequences) - frequently miscounts.
    - Complex multi-step "this then that then that" beyond ~3 actions - fails.
    For these, use stock footage (Pexels free tier or yt-dlp on a CC clip).

Transient errors - retry once before doing anything else:
    - DNS resolution failures on the first call of a session are not unusual
      (Windows / corporate DNS races against the SDK's first lookup). If the
      script reports a "Name or service not known" / "getaddrinfo failed" /
      DNS-style error, just re-run. A second attempt almost always succeeds.
"""

import argparse
import os
import sys
import time
from pathlib import Path

from kFfmpeg import kFfmpeg
from kGenAi import kGenAi
from kJsonFile import kJsonFile
from kShared import ToolInputException, kRun, kS, kToolException

DEFAULT_MODEL = "veo-3.1-generate-preview"
POLL_INTERVAL_SECONDS = 15


class kVideoJob:
    """One video to generate. Optional settings (None/False) keep the API defaults."""

    def __init__(self, Prompt, OutPath, Model, Resolution=None, NegativePrompt=None, ImagePath=None, Mute=False):
        try:
            self.Prompt = Prompt
            self.OutPath = OutPath
            self.Model = Model
            self.Resolution = Resolution
            self.NegativePrompt = NegativePrompt
            self.ImagePath = ImagePath
            self.Mute = Mute
        except Exception as e:
            kS.GlobalErrorHandler(e, "kVideoJob.__init__")

    @staticmethod
    def FromSpec(SpecPath, Args):
        """The jobs of a JSON spec file; a missing key falls back to the command-line value."""
        if kS.ErrorMode:
            return []
        try:
            Spec = kJsonFile.Load(SpecPath, "spec file")
            if kS.ErrorMode:
                return []
            if not isinstance(Spec, list):
                raise ToolInputException(f"spec must be a JSON list of {{prompt, out}} entries: {SpecPath}")
            Jobs = []
            for Index, Entry in enumerate(Spec):
                if not isinstance(Entry, dict) or not Entry.get("prompt") or not Entry.get("out"):
                    raise ToolInputException(f"spec entry {Index} needs \"prompt\" and \"out\": {SpecPath}")
                Jobs.append(kVideoJob(Entry["prompt"], Entry["out"], Entry.get("model", Args.model),
                                      Entry.get("resolution", Args.resolution),
                                      Entry.get("negative_prompt", Args.negative_prompt),
                                      Entry.get("image", Args.image), Entry.get("mute", Args.mute)))
            return Jobs
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kVideoJob.FromSpec(spec={SpecPath})")
            return []

    @staticmethod
    def Validate(Jobs):
        """Preconditions of every job, checked before the first paid call: image files exist, ffmpeg for mute."""
        if kS.ErrorMode:
            return
        try:
            for Job in Jobs:
                if Job.ImagePath and not os.path.isfile(Job.ImagePath):
                    raise ToolInputException(f"starting-frame image not found: {Job.ImagePath}")
            if any(Job.Mute for Job in Jobs) and not kFfmpeg.Find():
                raise ToolInputException("--mute needs ffmpeg on PATH or the static-ffmpeg package (not found)")
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, "kVideoJob.Validate")
            return


class kVideoGenerator:
    """Generates videos through one Gemini client: submit, poll, download, save."""

    def __init__(self, ApiKey, Quiet, PollInterval=POLL_INTERVAL_SECONDS):
        try:
            self.Quiet = Quiet
            self.PollInterval = PollInterval
            self.GenAi, self.Types, self.Errors = kGenAi.Load("batch_video_gen.py")
            self.Client = self.GenAi.Client(api_key=ApiKey) if self.GenAi is not None else None
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, "kVideoGenerator.__init__")

    def BuildConfig(self, Job):
        """GenerateVideosConfig for the optional settings, or None when none are set."""
        if kS.ErrorMode:
            return None
        try:
            if not Job.Resolution and not Job.NegativePrompt:
                return None
            return self.Types.GenerateVideosConfig(resolution=Job.Resolution, negative_prompt=Job.NegativePrompt)
        except Exception as e:
            kS.GlobalErrorHandler(e, "kVideoGenerator.BuildConfig")
            return None

    def LoadImage(self, Job):
        """Starting-frame Image from a PNG/JPEG path, or None."""
        if kS.ErrorMode:
            return None
        try:
            if not Job.ImagePath:
                return None
            Mime = "image/png" if str(Job.ImagePath).lower().endswith(".png") else "image/jpeg"
            return self.Types.Image(image_bytes=Path(Job.ImagePath).read_bytes(), mime_type=Mime)
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kVideoGenerator.LoadImage(image={Job.ImagePath})")
            return None

    def Produce(self, Job):
        """Submit, poll and download one video. Returns (video, "") or (None, refusal text) when the API refuses."""
        if kS.ErrorMode:
            return None, ""
        try:
            Image = self.LoadImage(Job)
            Config = self.BuildConfig(Job)
            if kS.ErrorMode:
                return None, ""
            try:
                # `source=` rather than `prompt=`: the prompt/image/video arguments are
                # deprecated in google-genai 2.14 and scheduled for removal after
                # 2026-07-31, which would break every caller of this helper.
                Operation = self.Client.models.generate_videos(
                    model=Job.Model,
                    source=self.Types.GenerateVideosSource(prompt=Job.Prompt, image=Image),
                    config=Config,
                )
                if not self.Quiet:
                    print("  polling", end="", flush=True)
                while not Operation.done:
                    time.sleep(self.PollInterval)
                    Operation = self.Client.operations.get(Operation)
                    if not self.Quiet:
                        print(".", end="", flush=True)
                if not self.Quiet:
                    print()
                if Operation.response is None or not Operation.response.generated_videos:
                    Reasons = getattr(Operation.response, "rai_media_filtered_reasons", None)
                    return None, f"no video returned (error={Operation.error}, filtered_reasons={Reasons})"
                Video = Operation.response.generated_videos[0]
                self.Client.files.download(file=Video.video)
                return Video, ""
            except self.Errors.APIError as Refusal:  # ERROR-SUPPRESSED-JUSTIFIED: an API refusal (429 free tier, 400, quota) is an expected per-job outcome, printed to stderr and counted into exit 1
                return None, f"{type(Refusal).__name__}: {Refusal}"
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kVideoGenerator.Produce(out={Job.OutPath})")
            return None, ""

    def StripAudio(self, OutPath):
        """Remove the audio track in place (video stream copied, not re-encoded)."""
        if kS.ErrorMode:
            return
        try:
            TmpPath = str(OutPath) + ".silent.mp4"
            if not kFfmpeg.Run(["-i", OutPath, "-an", "-c:v", "copy", TmpPath]):
                return
            os.replace(TmpPath, OutPath)
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kVideoGenerator.StripAudio(out={OutPath})")
            return

    def Generate(self, Job):
        """Generate one video and save it. True on success, False when the API refused the job."""
        if kS.ErrorMode:
            return False
        try:
            if not self.Quiet:
                print(f"Generating {Job.OutPath} (model={Job.Model})...", flush=True)
            Video, Refusal = self.Produce(Job)
            if kS.ErrorMode:
                return False
            if Video is None:
                sys.stderr.write(f"\nERROR: {Refusal}\n")  # errors always print, even in quiet mode
                return False
            Path(Job.OutPath).parent.mkdir(parents=True, exist_ok=True)
            Video.video.save(Job.OutPath)
            if Job.Mute:
                self.StripAudio(Job.OutPath)
                if kS.ErrorMode:
                    return False
            if not self.Quiet:
                print(f"  saved ({os.path.getsize(Job.OutPath) / 1024 / 1024:.1f} MB)")
            return True
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kVideoGenerator.Generate(out={Job.OutPath})")
            return False


class kBatchVideoGenApp:
    """Command line."""

    def Run(self):
        if kS.ErrorMode:
            return 1
        try:
            Parser = argparse.ArgumentParser(
                description="Batch video generation via Google Veo 3.1.",
                formatter_class=argparse.RawDescriptionHelpFormatter,
                epilog=__doc__,
            )
            Parser.add_argument("--prompt", help="Inline prompt (single-video mode)")
            Parser.add_argument("--out", help="Output MP4 path (single-video mode)")
            Parser.add_argument("--model", default=DEFAULT_MODEL, help="Veo model id")
            Parser.add_argument("--spec", help="JSON spec file with list of {prompt, out, "
                                               "model?, resolution?, negative_prompt?, "
                                               "image?, mute?} entries (batch mode)")
            Parser.add_argument("--resolution", default=None, choices=["720p", "1080p"],
                                help="Output resolution (default: the API default, 720p)")
            Parser.add_argument("--negative-prompt", default=None,
                                help="Things to keep out of the video (e.g. 'text, logos')")
            Parser.add_argument("--image", default=None, help="Starting-frame image path (image-to-video)")
            Parser.add_argument("--mute", action="store_true",
                                help="Strip the audio track after download (needs ffmpeg)")
            Parser.add_argument("--quiet", action="store_true",
                                help="Suppress per-job progress output. Errors still go to stderr.")
            Args = Parser.parse_args()

            if not Args.prompt and not Args.spec:
                raise ToolInputException("Either --prompt + --out OR --spec required")
            if Args.prompt and not Args.out:
                raise ToolInputException("--prompt requires --out")
            ApiKey = kGenAi.RequireKey()

            if Args.spec:
                Jobs = kVideoJob.FromSpec(Args.spec, Args)
            else:
                Jobs = [kVideoJob(Args.prompt, Args.out, Args.model, Args.resolution,
                                  Args.negative_prompt, Args.image, Args.mute)]
            kVideoJob.Validate(Jobs)
            if kS.ErrorMode:
                return 1

            Generator = kVideoGenerator(ApiKey, Args.quiet)
            Successes = 0
            for Job in Jobs:
                if Generator.Generate(Job):
                    Successes += 1
            if kS.ErrorMode:
                return 1

            if not Args.quiet:
                print(f"\n{Successes}/{len(Jobs)} videos generated.")
            return 0 if Successes == len(Jobs) else 1
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, "kBatchVideoGenApp.Run")
            return 1


if __name__ == "__main__":
    kRun.Main(kBatchVideoGenApp)
