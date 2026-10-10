"""JSON files: load with the expected states (missing, malformed) as bad input, save as readable UTF-8.

MASTER: kSharedPy/kshared/kJsonFile.py - products and kSharedPy's own folders receive generated copies (kpy).

    Edl = kJsonFile.Load(Args.edl, "EDL")          # exit 2: "EDL not found: ..." / "EDL is not valid JSON: ..."
    kJsonFile.Save(Out, Edl)                       # indent 2, ensure_ascii=False, creates the folder
"""
import json
from pathlib import Path as kPath

from kShared import ToolInputException, kS, kToolException


class kJsonFile:
    """Reads and writes JSON files; a missing or malformed file is the user's input mistake (exit 2)."""

    @staticmethod
    def Load(Path, What="JSON file"):
        """The parsed value of the file. Raises ToolInputException "<What> not found: <path>" or
        "<What> is not valid JSON: <path> (<detail>)"."""
        if kS.ErrorMode:
            return None
        try:
            File = kPath(Path)
            if not File.is_file():
                raise ToolInputException(f"{What} not found: {File}")
            try:
                return json.loads(File.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as Bad:  # ERROR-SUPPRESSED-JUSTIFIED: converted to the expected ToolInputException, which reaches the user
                raise ToolInputException(f"{What} is not valid JSON: {File} ({Bad})")
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kJsonFile.Load(file={Path})")
            return None

    @staticmethod
    def Save(Path, Data, Indent=2):
        """Write Data as UTF-8 JSON (non-ASCII kept as is), creating the folder. True when written."""
        if kS.ErrorMode:
            return False
        try:
            File = kPath(Path)
            File.parent.mkdir(parents=True, exist_ok=True)
            File.write_text(json.dumps(Data, indent=Indent, ensure_ascii=False), encoding="utf-8")
            return True
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kJsonFile.Save(file={Path})")
            return False
