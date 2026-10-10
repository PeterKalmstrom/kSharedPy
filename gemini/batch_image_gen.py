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

Run via uvx (the google-genai SDK is installed on demand):
    GEMINI_API_KEY="..." uvx --with google-genai python batch_image_gen.py [args]

Environment:
    GEMINI_API_KEY — required. Get from https://aistudio.google.com/apikey

Models:
    gemini-3.1-flash-image-preview  — fast, default
    gemini-3-pro-image-preview      — higher quality, slower

Notes:
    - Always prompt with "no text, no letters, no words, no logos, no captions"
      to avoid AI generators baking text into the image. Overlay text in the
      consuming app instead (PowerPoint, web, etc.) for editability.
    - If the model inserts text after 2 reprompts, give up and use a different
      background or fetch a stock image.
    - Output format: the API may return PNG OR JPEG depending on the model and
      request. Examples above use `.png` extension but you may receive JPEG
      bytes (magic `ffd8ff…`). The script writes whatever the API returns to
      the file path you supplied — extension is not enforced. Sniff magic bytes
      if you need to know the format downstream. The companion `smoke_test.py`
      accepts both PNG and JPEG headers.
"""

import argparse
import json
import os
import sys
from pathlib import Path


def _import_genai():
    try:
        from google import genai
        from google.genai import types
        return genai, types
    except ImportError:
        sys.stderr.write(
            "google-genai not installed. Run with:\n"
            "  uvx --with google-genai python batch_image_gen.py [args]\n"
        )
        sys.exit(1)


def generate_one(client, types, prompt, out_path, aspect_ratio="16:9",
                 model="gemini-3.1-flash-image-preview", quiet=False):
    """Generate a single image and save it. Returns True on success."""
    if not quiet:
        print(f"Generating {out_path}...", flush=True)
    try:
        resp = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=types.ImageConfig(aspect_ratio=aspect_ratio),
            ),
        )
        for part in resp.candidates[0].content.parts:
            if part.inline_data and part.inline_data.data:
                Path(out_path).parent.mkdir(parents=True, exist_ok=True)
                with open(out_path, "wb") as f:
                    f.write(part.inline_data.data)
                size = os.path.getsize(out_path)
                if not quiet:
                    print(f"  saved ({size} bytes)")
                return True
        # Errors always print, even in quiet mode
        sys.stderr.write(f"ERROR: no image returned for {out_path}\n")
        return False
    except Exception as e:
        sys.stderr.write(f"ERROR: {type(e).__name__}: {e}\n")
        return False


def main():
    p = argparse.ArgumentParser(
        description="Batch image generation via Google Gemini Flash Image.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--prompt", help="Inline prompt (single-image mode)")
    p.add_argument("--out", help="Output PNG path (single-image mode)")
    p.add_argument("--aspect", default="16:9",
                   help="Aspect ratio (16:9, 1:1, 9:16, 3:4, 4:3)")
    p.add_argument("--model", default="gemini-3.1-flash-image-preview",
                   help="Model id (default: gemini-3.1-flash-image-preview)")
    p.add_argument("--spec", help="JSON spec file with list of {prompt, out, "
                                  "aspect_ratio?, model?} entries (batch mode)")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress per-job progress output. Errors still go to stderr.")
    args = p.parse_args()

    if not args.prompt and not args.spec:
        p.error("Either --prompt + --out OR --spec required")
    if args.prompt and not args.out:
        p.error("--prompt requires --out")
    if "GEMINI_API_KEY" not in os.environ:
        p.error("GEMINI_API_KEY environment variable required")

    genai, types = _import_genai()
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    # Build job list
    jobs = []
    if args.spec:
        with open(args.spec, encoding="utf-8") as f:
            spec = json.load(f)
        for entry in spec:
            jobs.append({
                "prompt": entry["prompt"],
                "out_path": entry["out"],
                "aspect_ratio": entry.get("aspect_ratio", args.aspect),
                "model": entry.get("model", args.model),
            })
    else:
        jobs.append({
            "prompt": args.prompt,
            "out_path": args.out,
            "aspect_ratio": args.aspect,
            "model": args.model,
        })

    # Run all jobs
    successes = 0
    for job in jobs:
        if generate_one(client, types, quiet=args.quiet, **job):
            successes += 1

    if not args.quiet:
        print(f"\n{successes}/{len(jobs)} images generated.")
    sys.exit(0 if successes == len(jobs) else 1)


if __name__ == "__main__":
    main()
