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

    # Spec file format (list of {prompt, out, model?, aspect_ratio?}):
    [
      {"prompt": "...", "out": "C:\\\\videos\\\\intro.mp4"},
      {"prompt": "...", "out": "C:\\\\videos\\\\reveal.mp4", "model": "veo-3.1-fast-generate-preview"}
    ]

Run via uvx (the google-genai SDK is installed on demand):
    GEMINI_API_KEY="..." uvx --with google-genai python batch_video_gen.py [args]

Environment:
    GEMINI_API_KEY — required. Get from https://aistudio.google.com/apikey

Models (most expensive last):
    veo-3.1-fast-generate-preview   — fastest, cheapest, lower quality
    veo-3.1-generate-preview         — default; balanced
    veo-3.1-pro-generate-preview     — highest quality, slower, costlier

Output: 8-second 720p MP4 with native audio, typically 1–10 MB per clip.
Generation time: ~60 seconds per clip including polling.

Prompt tips:
    - Keep prompts to 3–5 sentences. Long rule lists degrade results.
    - Veo trains on natural cinematic descriptions, not specifications.
    - Specify direction of motion explicitly: "moves from left to right".
    - "No text, no logos, no captions" to avoid baked-in text.

Known limits — don't waste reprompts:
    - Sequential rigid-body chain reactions (dominoes, Newton's cradle) — fail.
    - Counting (exactly N objects, ordered sequences) — frequently miscounts.
    - Complex multi-step "this then that then that" beyond ~3 actions — fails.
    For these, use stock footage (Pexels free tier or yt-dlp on a CC clip).

Transient errors — retry once before doing anything else:
    - DNS resolution failures on the first call of a session are not unusual
      (Windows / corporate DNS races against the SDK's first lookup). If the
      script raises a "Name or service not known" / "getaddrinfo failed" /
      DNS-style error, just re-run. A second attempt almost always succeeds.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path


def _import_genai():
    try:
        from google import genai
        return genai
    except ImportError:
        sys.stderr.write(
            "google-genai not installed. Run with:\n"
            "  uvx --with google-genai python batch_video_gen.py [args]\n"
        )
        sys.exit(1)


def _build_config(types, resolution, negative_prompt):
    """GenerateVideosConfig for the optional settings, or None when none are set."""
    if not resolution and not negative_prompt:
        return None
    return types.GenerateVideosConfig(resolution=resolution,
                                      negative_prompt=negative_prompt)


def _load_image(types, image_path):
    """Starting-frame Image from a PNG/JPEG path, or None."""
    if not image_path:
        return None
    mime = "image/png" if str(image_path).lower().endswith(".png") else "image/jpeg"
    return types.Image(image_bytes=Path(image_path).read_bytes(), mime_type=mime)


def _strip_audio(out_path):
    """Remove the audio track in place (video stream copied, not re-encoded)."""
    import subprocess
    tmp_path = str(out_path) + ".silent.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(out_path),
                    "-an", "-c:v", "copy", tmp_path], check=True)
    os.replace(tmp_path, out_path)


def generate_one(client, prompt, out_path,
                 model="veo-3.1-generate-preview", poll_interval=15, quiet=False,
                 resolution=None, negative_prompt=None, image_path=None, mute=False):
    """Generate a single video and save it. Returns True on success.

    Optional (defaults keep the old behaviour): resolution ("720p"/"1080p"),
    negative_prompt, image_path (starting frame), mute (strip audio via ffmpeg).
    """
    if not quiet:
        print(f"Generating {out_path} (model={model})...", flush=True)
    try:
        # `source=` rather than `prompt=`: the prompt/image/video arguments are
        # deprecated in google-genai 2.14 and scheduled for removal after
        # 2026-07-31, which would break every caller of this helper.
        from google.genai import types
        op = client.models.generate_videos(
            model=model,
            source=types.GenerateVideosSource(prompt=prompt,
                                              image=_load_image(types, image_path)),
            config=_build_config(types, resolution, negative_prompt),
        )
        if not quiet:
            print("  polling", end="", flush=True)
        while not op.done:
            time.sleep(poll_interval)
            op = client.operations.get(op)
            if not quiet:
                print(".", end="", flush=True)
        if not quiet:
            print()
        if op.response is None or not op.response.generated_videos:
            reasons = getattr(op.response, "rai_media_filtered_reasons", None)
            raise RuntimeError(f"no video returned (error={op.error}, "
                               f"filtered_reasons={reasons})")
        video = op.response.generated_videos[0]
        client.files.download(file=video.video)
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        video.video.save(out_path)
        if mute:
            _strip_audio(out_path)
        if not quiet:
            size_mb = os.path.getsize(out_path) / 1024 / 1024
            print(f"  saved ({size_mb:.1f} MB)")
        return True
    except Exception as e:
        sys.stderr.write(f"\nERROR: {type(e).__name__}: {e}\n")
        return False


def main():
    p = argparse.ArgumentParser(
        description="Batch video generation via Google Veo 3.1.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--prompt", help="Inline prompt (single-video mode)")
    p.add_argument("--out", help="Output MP4 path (single-video mode)")
    p.add_argument("--model", default="veo-3.1-generate-preview",
                   help="Veo model id")
    p.add_argument("--spec", help="JSON spec file with list of {prompt, out, "
                                  "model?, resolution?, negative_prompt?, "
                                  "image?, mute?} entries (batch mode)")
    p.add_argument("--resolution", default=None, choices=["720p", "1080p"],
                   help="Output resolution (default: the API default, 720p)")
    p.add_argument("--negative-prompt", default=None,
                   help="Things to keep out of the video (e.g. 'text, logos')")
    p.add_argument("--image", default=None,
                   help="Starting-frame image path (image-to-video)")
    p.add_argument("--mute", action="store_true",
                   help="Strip the audio track after download (needs ffmpeg)")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress per-job progress output. Errors still go to stderr.")
    args = p.parse_args()

    if not args.prompt and not args.spec:
        p.error("Either --prompt + --out OR --spec required")
    if args.prompt and not args.out:
        p.error("--prompt requires --out")
    if "GEMINI_API_KEY" not in os.environ:
        p.error("GEMINI_API_KEY environment variable required")

    genai = _import_genai()
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    jobs = []
    if args.spec:
        with open(args.spec, encoding="utf-8") as f:
            spec = json.load(f)
        for entry in spec:
            jobs.append({
                "prompt": entry["prompt"],
                "out_path": entry["out"],
                "model": entry.get("model", args.model),
                "resolution": entry.get("resolution", args.resolution),
                "negative_prompt": entry.get("negative_prompt", args.negative_prompt),
                "image_path": entry.get("image", args.image),
                "mute": entry.get("mute", args.mute),
            })
    else:
        jobs.append({
            "prompt": args.prompt,
            "out_path": args.out,
            "model": args.model,
            "resolution": args.resolution,
            "negative_prompt": args.negative_prompt,
            "image_path": args.image,
            "mute": args.mute,
        })

    successes = 0
    for job in jobs:
        if generate_one(client, quiet=args.quiet, **job):
            successes += 1

    if not args.quiet:
        print(f"\n{successes}/{len(jobs)} videos generated.")
    sys.exit(0 if successes == len(jobs) else 1)


if __name__ == "__main__":
    main()
