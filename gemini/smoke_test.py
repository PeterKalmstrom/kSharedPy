"""
Smoke test for batch_image_gen.py.

Generates one tiny known-good image and asserts the file exists with a
plausible size. Use to verify the Gemini API key, network path, and
batch_image_gen.py import all work end-to-end.

Usage:
    GEMINI_API_KEY="..." uv run smoke_test.py
    (the header declares google-genai; without inline-metadata support:
     uvx --with google-genai python smoke_test.py)

Exits 0 on success, 1 on any failure with a "FAIL: ..." diagnostic message
(4 if an unexpected error was reported and its report waits for the user's yes).
The test image is written to a temporary folder under kSharedPy/_scratch/ (inside
the workspace, never the user profile) and removed afterwards.
"""
# /// script
# requires-python = ">=3.10"
# dependencies = ["google-genai"]
# ///

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from kShared import ToolReportableException, kRun, kS, kToolException

THIS_DIR = Path(__file__).resolve().parent
TARGET_SCRIPT = THIS_DIR / "batch_image_gen.py"
SCRATCH_DIR = THIS_DIR.parent / "_scratch"
PROMPT = "A single red apple on a plain white background, photograph, no text"
TIMEOUT_SECONDS = 180
MIN_BYTES = 5_000   # any real PNG should be larger than this
MAX_BYTES = 20_000_000  # if larger than this, something is likely wrong
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"


class kSmokeTest:
    """One end-to-end run of batch_image_gen.py and the checks on its output."""

    @staticmethod
    def Fail(Message: str) -> ToolReportableException:
        """The expected failure of the smoke test: exit 1 with "FAIL: <message>". Always an exception, so `raise` never gets None."""
        if kS.ErrorMode:
            return ToolReportableException(f"FAIL: {Message}", ExitCode=1)
        try:
            return ToolReportableException(f"FAIL: {Message}", ExitCode=1)
        except Exception as e:
            kS.GlobalErrorHandler(e, "kSmokeTest.Fail")
            return ToolReportableException(f"FAIL: {Message}", ExitCode=1)

    @staticmethod
    def Generate(OutPath: Path) -> "subprocess.CompletedProcess[str] | None":
        """Run batch_image_gen.py for one image; raises the FAIL exception when it does not exit 0."""
        if kS.ErrorMode:
            return None
        try:
            try:
                Result = subprocess.run(
                    [sys.executable, str(TARGET_SCRIPT), "--prompt", PROMPT, "--out", str(OutPath), "--aspect", "1:1"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:  # ERROR-SUPPRESSED-JUSTIFIED: a hung generation is a smoke-test FAIL, raised to the user below
                raise kSmokeTest.Fail(f"batch_image_gen.py did not finish within {TIMEOUT_SECONDS} s")
            if Result.returncode != 0:
                raise kSmokeTest.Fail(f"batch_image_gen.py exit {Result.returncode}\n"
                                      f"STDOUT:\n{Result.stdout}\nSTDERR:\n{Result.stderr}")
            if not OutPath.exists():
                raise kSmokeTest.Fail(f"output file not created at {OutPath}\nSTDOUT:\n{Result.stdout}")
            return Result
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, "kSmokeTest.Generate")
            return None

    @staticmethod
    def Check(OutPath: Path) -> tuple[int, str]:
        """Size and magic-byte checks; returns (size, "PNG" or "JPEG")."""
        if kS.ErrorMode:
            return 0, ""
        try:
            Size = OutPath.stat().st_size
            if Size < MIN_BYTES:
                raise kSmokeTest.Fail(f"output file too small: {Size} bytes (expected > {MIN_BYTES})")
            if Size > MAX_BYTES:
                raise kSmokeTest.Fail(f"output file suspiciously large: {Size} bytes (expected < {MAX_BYTES})")
            # Sniff magic bytes - Gemini may return PNG or JPEG depending on the request
            with open(OutPath, "rb") as File:
                Magic = File.read(8)
            if Magic[:8] == PNG_MAGIC:
                return Size, "PNG"
            if Magic[:3] == JPEG_MAGIC:
                return Size, "JPEG"
            raise kSmokeTest.Fail(f"output is neither a PNG nor a JPEG (magic bytes: {Magic.hex()})")
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, "kSmokeTest.Check")
            return 0, ""


class kSmokeTestApp:
    """Command line."""

    def Run(self) -> int:
        if kS.ErrorMode:
            return 1
        try:
            Parser = argparse.ArgumentParser(description="Smoke test for batch_image_gen.py.",
                                             formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
            Parser.parse_args()
            if not os.environ.get("GEMINI_API_KEY"):
                raise kSmokeTest.Fail("GEMINI_API_KEY environment variable not set")
            if not TARGET_SCRIPT.exists():
                raise kSmokeTest.Fail(f"target script not found: {TARGET_SCRIPT}")
            SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="smoke-", dir=SCRATCH_DIR) as Folder:
                OutPath = Path(Folder) / "smoke.png"
                print(f"Generating smoke image to {OutPath}...")
                kSmokeTest.Generate(OutPath)
                Size, Format = kSmokeTest.Check(OutPath)
            if kS.ErrorMode:
                return 1
            print(f"PASS: generated {Size} bytes, valid {Format} header.")
            return 0
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, "kSmokeTestApp.Run")
            return 1


if __name__ == "__main__":
    kRun.Main(kSmokeTestApp)
