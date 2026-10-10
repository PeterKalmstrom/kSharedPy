"""Shared helper: ensure ffmpeg/ffprobe are on PATH via the static-ffmpeg package.

Call `ensure_ffmpeg()` near the top of any script that shells out to ffmpeg.
First call may take ~30s to download ~80MB of static binaries; subsequent
calls in the same uv cache are instant.
"""

from __future__ import annotations

import shutil
import subprocess
import sys


def ensure_ffmpeg() -> tuple[str, str]:
    """Return (ffmpeg_path, ffprobe_path), prepending them to PATH if needed."""
    try:
        from static_ffmpeg import add_paths
    except ImportError:
        sys.stderr.write(
            "ERROR: static-ffmpeg not installed. "
            "Run via: uvx --with static-ffmpeg python <script>.py ...\n"
        )
        sys.exit(2)

    add_paths()
    ff = shutil.which("ffmpeg")
    fp = shutil.which("ffprobe")
    if not ff or not fp:
        sys.stderr.write("ERROR: ffmpeg/ffprobe not found after static_ffmpeg.add_paths()\n")
        sys.exit(2)
    return ff, fp


def probe_duration_seconds(path: str) -> float:
    """Return the duration of a media file in seconds via ffprobe."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())
