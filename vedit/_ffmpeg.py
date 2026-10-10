"""Shared helpers for the vedit scripts: ffmpeg/ffprobe on PATH via the static-ffmpeg package, media probes, and
the check for an optional Python package.

Call `kFfmpeg.Ensure()` near the top of any script that shells out to ffmpeg. The first call may take ~30s to
download ~80MB of static binaries; later calls in the same uv cache are instant.
"""
import importlib.util
import shutil
import subprocess

from kShared import ToolInputException, kS, kToolException


class kDependency:
    """Whether an optional package is installed - a precondition check, so a missing package is never caught."""

    @staticmethod
    def Has(ModuleName):
        """True when ModuleName (dotted, e.g. "google.genai") can be imported. Checks each parent first, because
        find_spec of a submodule raises when its parent package is missing."""
        if kS.ErrorMode:
            return False
        try:
            Parts = ModuleName.split(".")
            for Index in range(len(Parts)):
                if importlib.util.find_spec(".".join(Parts[:Index + 1])) is None:
                    return False
            return True
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kDependency.Has(module={ModuleName})")
            return False


class kFfmpeg:
    """ffmpeg/ffprobe for the vedit scripts."""

    @staticmethod
    def Ensure():
        """Return (ffmpeg_path, ffprobe_path), prepending the static-ffmpeg binaries to PATH if needed."""
        if kS.ErrorMode:
            return ("", "")
        try:
            if not kDependency.Has("static_ffmpeg"):
                raise ToolInputException("static-ffmpeg not installed. "
                                         "Run via: uvx --with static-ffmpeg python <script>.py ...")
            from static_ffmpeg import add_paths  # optional package: present only under `uvx --with static-ffmpeg`
            add_paths()
            FfmpegPath = shutil.which("ffmpeg")
            FfprobePath = shutil.which("ffprobe")
            if not FfmpegPath or not FfprobePath:
                raise ToolInputException("ffmpeg/ffprobe not found after static_ffmpeg.add_paths()")
            return (FfmpegPath, FfprobePath)
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, "kFfmpeg.Ensure")
            return ("", "")

    @staticmethod
    def ProbeDurationSeconds(Path):
        """The duration of a media file in seconds via ffprobe."""
        if kS.ErrorMode:
            return 0.0
        try:
            Result = subprocess.run(
                ["ffprobe", "-v", "error",
                 "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1",
                 str(Path)],
                capture_output=True, text=True, check=True,
            )
            return float(Result.stdout.strip())
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kFfmpeg.ProbeDurationSeconds(file={Path})")
            return 0.0

    @staticmethod
    def ProbeAudioStreamCount(Path):
        """How many audio streams a media file has, via ffprobe."""
        if kS.ErrorMode:
            return 0
        try:
            Result = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a",
                 "-show_entries", "stream=index", "-of", "csv=p=0", str(Path)],
                capture_output=True, text=True, check=True,
            )
            return len([Line for Line in Result.stdout.splitlines() if Line.strip()])
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kFfmpeg.ProbeAudioStreamCount(file={Path})")
            return 0
