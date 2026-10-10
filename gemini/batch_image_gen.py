"""
Batch image generation via Google Gemini Flash Image.

Generates one or many images from a JSON spec or inline prompt list. Each image
is saved to a file path you supply. Use as a reusable helper from any project
that needs Gemini-generated images (slide backgrounds, illustrations, mockups).

Usage:
    # Single image, inline
    python batch_image_gen.py \\
        --prompt "A photograph of an apple on a wooden table" \\
        --out C:\\path\\to\\apple.png \\
        --aspect 16:9

    # Batch from JSON spec
    python batch_image_gen.py --spec spec.json

    # Spec file format (list of {prompt, out, aspect_ratio?, model?}):
    [
      {"prompt": "...", "out": "C:\\\\images\\\\eq-opener.png", "aspect_ratio": "16:9"},
      {"prompt": "...", "out": "C:\\\\images\\\\eq-faults.png", "aspect_ratio": "16:9"}
    ]

Run with uv (the script header declares google-genai; uv installs it on demand):
    GEMINI_API_KEY="..." uv run batch_image_gen.py [args]
    (an older uv without inline-metadata support: uvx --with google-genai python batch_image_gen.py [args])

Environment:
    GEMINI_API_KEY - required. Get from https://aistudio.google.com/apikey

Models:
    gemini-3.1-flash-image-preview  - fast, default
    gemini-3-pro-image-preview      - higher quality, slower

Exit codes and errors (the kShared error pattern):
    0  every job saved its image
    1  at least one job was refused by the API (see below), google-genai is not
       installed, or an unexpected error was reported (the run halts on it)
    2  bad input: missing --prompt/--spec, --prompt without --out, GEMINI_API_KEY
       not set, spec file missing or not a list of {prompt, out} entries
    4  an unexpected error whose report waits for the user's yes
       (stderr: "ERROR-REPORT-PENDING: <file>")
    Batch choice: a refusal BY THE API for one job (google.genai APIError - quota,
    safety block, bad model id - or a response without an image) is an expected
    per-job outcome: "ERROR: ..." goes to stderr (also with --quiet), the other
    jobs still run, and the exit code is 1. Anything else (network failure,
    an unwritable output path, a bug) is unexpected: it is reported and the
    remaining jobs are skipped. The spec is validated in full before the first
    paid call.

Notes:
    - Always prompt with "no text, no letters, no words, no logos, no captions"
      to avoid AI generators baking text into the image. Overlay text in the
      consuming app instead (PowerPoint, web, etc.) for editability.
    - If the model inserts text after 2 reprompts, give up and use a different
      background or fetch a stock image.
    - Output format: the API may return PNG OR JPEG depending on the model and
      request. Examples above use `.png` extension but you may receive JPEG
      bytes (magic `ffd8ff...`). The script writes whatever the API returns to
      the file path you supplied - extension is not enforced. Sniff magic bytes
      if you need to know the format downstream. The companion `smoke_test.py`
      accepts both PNG and JPEG headers.
"""
# /// script
# requires-python = ">=3.10"
# dependencies = ["google-genai"]
# ///

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from kGenAi import kGenAi
from kJsonFile import kJsonFile
from kShared import ToolInputException, kRun, kS, kToolException

DEFAULT_MODEL = "gemini-3.1-flash-image-preview"


class kImageJob:
    """One image to generate: prompt, output path, aspect ratio, model."""

    def __init__(self, Prompt: str, OutPath: str, AspectRatio: str, Model: str) -> None:
        try:
            self.Prompt = Prompt
            self.OutPath = OutPath
            self.AspectRatio = AspectRatio
            self.Model = Model
        except Exception as e:
            kS.GlobalErrorHandler(e, "kImageJob.__init__")

    @staticmethod
    def FromSpec(SpecPath: str, DefaultAspect: str, DefaultModel: str) -> list["kImageJob"]:
        """The jobs of a JSON spec file (a list of {prompt, out, aspect_ratio?, model?}), validated in full."""
        if kS.ErrorMode:
            return []
        try:
            Spec = kJsonFile.Load(SpecPath, "spec file")
            if kS.ErrorMode:
                return []
            if not isinstance(Spec, list):
                raise ToolInputException(f"spec must be a JSON list of {{prompt, out}} entries: {SpecPath}")
            Jobs: list[kImageJob] = []
            for Index, Entry in enumerate(Spec):
                if not isinstance(Entry, dict) or not Entry.get("prompt") or not Entry.get("out"):
                    raise ToolInputException(f"spec entry {Index} needs \"prompt\" and \"out\": {SpecPath}")
                Jobs.append(kImageJob(Entry["prompt"], Entry["out"],
                                      Entry.get("aspect_ratio", DefaultAspect), Entry.get("model", DefaultModel)))
            return Jobs
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kImageJob.FromSpec(spec={SpecPath})")
            return []


class kImageGenerator:
    """Generates images through one Gemini client."""

    def __init__(self, ApiKey: str, Quiet: bool) -> None:
        try:
            self.Quiet = Quiet
            self.GenAi, self.Types, self.Errors = kGenAi.Load("batch_image_gen.py")
            self.Client: Any =self.GenAi.Client(api_key=ApiKey) if self.GenAi is not None else None
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, "kImageGenerator.__init__")

    def Request(self, Job: kImageJob) -> tuple[Any, str]:
        """The API response for one job, or the refusal text when the API refuses it (APIError)."""
        if kS.ErrorMode:
            return None, ""
        try:
            Config = self.Types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=self.Types.ImageConfig(aspect_ratio=Job.AspectRatio),
            )
            try:
                return self.Client.models.generate_content(model=Job.Model, contents=Job.Prompt, config=Config), ""
            except self.Errors.APIError as Refusal:  # ERROR-SUPPRESSED-JUSTIFIED: an API refusal (quota, safety, bad model) is an expected per-job outcome, printed to stderr and counted into exit 1
                return None, f"{type(Refusal).__name__}: {Refusal}"
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kImageGenerator.Request(out={Job.OutPath})")
            return None, ""

    def ImageBytes(self, Response: Any) -> bytes | None:
        """The first inline image in the response, or None (safety block or text-only answer)."""
        if kS.ErrorMode:
            return None
        try:
            Candidates = Response.candidates or []
            if not Candidates or Candidates[0].content is None:
                return None
            for Part in Candidates[0].content.parts or []:
                if Part.inline_data and Part.inline_data.data:
                    return Part.inline_data.data
            return None
        except Exception as e:
            kS.GlobalErrorHandler(e, "kImageGenerator.ImageBytes")
            return None

    def Generate(self, Job: kImageJob) -> bool:
        """Generate one image and save it. True on success, False when the API refused the job."""
        if kS.ErrorMode:
            return False
        try:
            if not self.Quiet:
                print(f"Generating {Job.OutPath}...", flush=True)
            Response, Refusal = self.Request(Job)
            if kS.ErrorMode:
                return False
            if Response is None:
                sys.stderr.write(f"ERROR: {Refusal}\n")  # errors always print, even in quiet mode
                return False
            Data = self.ImageBytes(Response)
            if kS.ErrorMode:
                return False
            if Data is None:
                sys.stderr.write(f"ERROR: no image returned for {Job.OutPath}\n")
                return False
            Path(Job.OutPath).parent.mkdir(parents=True, exist_ok=True)
            with open(Job.OutPath, "wb") as File:
                File.write(Data)
            if not self.Quiet:
                print(f"  saved ({os.path.getsize(Job.OutPath)} bytes)")
            return True
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kImageGenerator.Generate(out={Job.OutPath})")
            return False


class kBatchImageGenApp:
    """Command line."""

    def Run(self) -> int:
        if kS.ErrorMode:
            return 1
        try:
            Parser = argparse.ArgumentParser(
                description="Batch image generation via Google Gemini Flash Image.",
                formatter_class=argparse.RawDescriptionHelpFormatter,
                epilog=__doc__,
            )
            Parser.add_argument("--prompt", help="Inline prompt (single-image mode)")
            Parser.add_argument("--out", help="Output PNG path (single-image mode)")
            Parser.add_argument("--aspect", default="16:9", help="Aspect ratio (16:9, 1:1, 9:16, 3:4, 4:3)")
            Parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Model id (default: {DEFAULT_MODEL})")
            Parser.add_argument("--spec", help="JSON spec file with list of {prompt, out, "
                                               "aspect_ratio?, model?} entries (batch mode)")
            Parser.add_argument("--quiet", action="store_true",
                                help="Suppress per-job progress output. Errors still go to stderr.")
            Args: argparse.Namespace = Parser.parse_args()

            if not Args.prompt and not Args.spec:
                raise ToolInputException("Either --prompt + --out OR --spec required")
            if Args.prompt and not Args.out:
                raise ToolInputException("--prompt requires --out")
            ApiKey = kGenAi.RequireKey()

            if Args.spec:
                Jobs = kImageJob.FromSpec(Args.spec, Args.aspect, Args.model)
            else:
                Jobs = [kImageJob(Args.prompt, Args.out, Args.aspect, Args.model)]
            if kS.ErrorMode:
                return 1

            Generator = kImageGenerator(ApiKey, Args.quiet)
            Successes = 0
            for Job in Jobs:
                if Generator.Generate(Job):
                    Successes += 1
            if kS.ErrorMode:
                return 1

            if not Args.quiet:
                print(f"\n{Successes}/{len(Jobs)} images generated.")
            return 0 if Successes == len(Jobs) else 1
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, "kBatchImageGenApp.Run")
            return 1


if __name__ == "__main__":
    kRun.Main(kBatchImageGenApp)
