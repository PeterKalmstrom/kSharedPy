"""One output policy for every command-line script: results on stdout, progress and warnings on stderr.

MASTER: kSharedPy/kshared/kConsole.py - products and kSharedPy's own folders receive generated copies (kpy).

    kConsole.Quiet = Args.quiet                       # set once, right after parsing the arguments
    kConsole.Result(Markdown, End="")                 # stdout: what a caller may parse (JSON, a path, a report)
    kConsole.Progress(f"[vedit/apply] done → {Out}")  # stderr, suppressed by --quiet
    kConsole.Warn("WARNING: no matching tracks")      # stderr, always - also with --quiet

Rules: data a caller may parse goes to stdout and nothing else does; progress and status go to stderr; --quiet
suppresses progress only; warnings and errors always show (errors themselves reach stderr through kRun.Main).
"""
import sys

from kShared import kS


class kConsole:
    """The three output channels of a script. Quiet is process-wide: a script sets it once from its arguments."""

    Quiet: bool = False

    @staticmethod
    def Result(Text: str = "", End: str = "\n") -> None:
        """Primary, machine-readable output: stdout, never suppressed."""
        if kS.ErrorMode:
            return
        try:
            sys.stdout.write(f"{Text}{End}")
            sys.stdout.flush()
        except Exception as e:
            kS.GlobalErrorHandler(e, "kConsole.Result")
            return

    @staticmethod
    def Progress(Text: str = "", End: str = "\n") -> None:
        """Progress and status for the person watching: stderr, suppressed when Quiet."""
        if kS.ErrorMode:
            return
        try:
            if kConsole.Quiet:
                return
            sys.stderr.write(f"{Text}{End}")
            sys.stderr.flush()
        except Exception as e:
            kS.GlobalErrorHandler(e, "kConsole.Progress")
            return

    @staticmethod
    def Warn(Text: str, End: str = "\n") -> None:
        """A warning or a per-item error the run survives: stderr, shown also when Quiet."""
        if kS.ErrorMode:
            return
        try:
            sys.stderr.write(f"{Text}{End}")
            sys.stderr.flush()
        except Exception as e:
            kS.GlobalErrorHandler(e, "kConsole.Warn")
            return
