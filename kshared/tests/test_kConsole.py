"""Tests for kConsole. Run: python -m unittest discover -s kshared/tests -p "test_kConsole.py" -v"""
import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

KSHARED = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KSHARED)
from kConsole import kConsole  # noqa: E402
from kShared import kS  # noqa: E402


class kConsoleTests(unittest.TestCase):

    def setUp(self):
        kS.Reset()
        kConsole.Quiet = False

    def tearDown(self):
        kConsole.Quiet = False
        kS.Reset()

    def Capture(self, Action):
        """(stdout, stderr) written while Action runs."""
        Out, Err = io.StringIO(), io.StringIO()
        with redirect_stdout(Out), redirect_stderr(Err):
            Action()
        return Out.getvalue(), Err.getvalue()

    def AllThree(self):
        kConsole.Result('{"ok": true}')
        kConsole.Progress("[tool] working…")
        kConsole.Warn("WARNING: something")

    def test_ChannelsWhenNotQuiet(self):
        self.assertEqual(self.Capture(self.AllThree), ('{"ok": true}\n', "[tool] working…\nWARNING: something\n"))

    def test_QuietSuppressesProgressOnly(self):
        kConsole.Quiet = True
        self.assertEqual(self.Capture(self.AllThree), ('{"ok": true}\n', "WARNING: something\n"))

    def ResultWithoutNewlineAndDots(self):
        kConsole.Result("# Review\n", End="")
        kConsole.Progress("  polling", End="")
        kConsole.Progress(".", End="")
        kConsole.Progress()
        kConsole.Warn("raw ffmpeg tail", End="")

    def test_EndControlsTheLineEnd(self):
        self.assertEqual(self.Capture(self.ResultWithoutNewlineAndDots),
                         ("# Review\n", "  polling.\nraw ffmpeg tail"))

    def test_EmptyResultIsABlankLine(self):
        self.assertEqual(self.Capture(kConsole.Result), ("\n", ""))

    def test_HaltedWritesNothing(self):
        kS.ErrorMode = True
        self.assertEqual(self.Capture(self.AllThree), ("", ""))


if __name__ == "__main__":
    unittest.main()
