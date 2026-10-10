"""Loaders for the vedit data files (EDL, transcript) shared by the vedit scripts.

A file that is valid JSON but not the expected shape is bad input (ToolInputException, exit 2), the same as a
missing or malformed file - never an unexpected KeyError.
"""
from os import PathLike
from typing import Any

from kJsonFile import kJsonFile
from kShared import ToolInputException, kS, kToolException


class kVeditData:
    """Load an EDL or a transcript and check it has a "segments" list before any script reads it."""

    @staticmethod
    def LoadEdl(Path: str | PathLike[str]) -> dict[str, Any]:
        """An EDL: {"segments": [{"start", "end", "keep", ...}, ...], ...}."""
        if kS.ErrorMode:
            return {}
        try:
            return kVeditData.WithSegments(kJsonFile.Load(Path, "EDL"), "EDL", Path)
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kVeditData.LoadEdl(path={Path})")
            return {}

    @staticmethod
    def LoadTranscript(Path: str | PathLike[str]) -> dict[str, Any]:
        """A transcript (transcribe.py output): {"segments": [{"start", "end", "text", "words"?}, ...], ...}."""
        if kS.ErrorMode:
            return {}
        try:
            return kVeditData.WithSegments(kJsonFile.Load(Path, "transcript"), "transcript", Path)
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kVeditData.LoadTranscript(path={Path})")
            return {}

    @staticmethod
    def WithSegments(Data: Any, What: str, Path: str | PathLike[str]) -> dict[str, Any]:
        """Data itself when it is an object with a "segments" list, else ToolInputException."""
        if kS.ErrorMode:
            return {}
        try:
            if not isinstance(Data, dict) or not isinstance(Data.get("segments"), list):
                raise ToolInputException(f"{What} has no \"segments\" list: {Path}")
            return Data
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, "kVeditData.WithSegments")
            return {}
