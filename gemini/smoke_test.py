"""
Smoke test for batch_image_gen.py.

Generates one tiny known-good image and asserts the file exists with a
plausible size. Use to verify the Gemini API key, network path, and
batch_image_gen.py import all work end-to-end.

Usage:
    GEMINI_API_KEY="..." uvx --with google-genai python smoke_test.py

Exits 0 on success, 1 on any failure with a diagnostic message.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

THIS_DIR = Path(__file__).parent
TARGET_SCRIPT = THIS_DIR / "batch_image_gen.py"
MIN_BYTES = 5_000   # any real PNG should be larger than this
MAX_BYTES = 20_000_000  # if larger than this, something is likely wrong


def fail(msg, code=1):
    sys.stderr.write(f"FAIL: {msg}\n")
    sys.exit(code)


def main():
    if "GEMINI_API_KEY" not in os.environ:
        fail("GEMINI_API_KEY environment variable not set")

    if not TARGET_SCRIPT.exists():
        fail(f"target script not found: {TARGET_SCRIPT}")

    with tempfile.TemporaryDirectory() as td:
        out_path = Path(td) / "smoke.png"
        print(f"Generating smoke image to {out_path}...")

        result = subprocess.run(
            [sys.executable, str(TARGET_SCRIPT),
             "--prompt", "A single red apple on a plain white background, photograph, no text",
             "--out", str(out_path),
             "--aspect", "1:1"],
            capture_output=True, text=True, timeout=180,
        )

        if result.returncode != 0:
            fail(f"batch_image_gen.py exit {result.returncode}\n"
                 f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")

        if not out_path.exists():
            fail(f"output file not created at {out_path}\n"
                 f"STDOUT:\n{result.stdout}")

        size = out_path.stat().st_size
        if size < MIN_BYTES:
            fail(f"output file too small: {size} bytes (expected > {MIN_BYTES})")
        if size > MAX_BYTES:
            fail(f"output file suspiciously large: {size} bytes "
                 f"(expected < {MAX_BYTES})")

        # Sniff magic bytes — Gemini may return PNG or JPEG depending on the request
        with open(out_path, "rb") as f:
            magic = f.read(8)
        is_png = magic[:8] == b"\x89PNG\r\n\x1a\n"
        is_jpeg = magic[:3] == b"\xff\xd8\xff"
        if not (is_png or is_jpeg):
            fail(f"output is neither a PNG nor a JPEG (magic bytes: {magic.hex()})")

        fmt = "PNG" if is_png else "JPEG"
        print(f"PASS: generated {size} bytes, valid {fmt} header.")


if __name__ == "__main__":
    main()
