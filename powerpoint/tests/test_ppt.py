"""Tests for _ppt.py. Run: python -m unittest discover -s X:\\Source\\Admin\\kSharedPy\\powerpoint\\tests -v

_ppt imports the generated kShared.py beside it, so the tests import both from powerpoint\\. The COM tests run only
where pywin32 and PowerPoint are installed; they build a deck under kSharedPy\\_scratch\\ppt-tests and quit PowerPoint
afterwards unless it was already running.
"""
import importlib.util
import os
import shutil
import sys
import unittest
import uuid

POWERPOINT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRATCH = os.path.join(os.path.dirname(POWERPOINT), "_scratch", "ppt-tests")
sys.path.insert(0, POWERPOINT)

from _ppt import kPpt, kPptLocator, kPptSession  # noqa: E402
from kShared import ToolInputException, kErrorReport, kS  # noqa: E402


def HasPowerPoint():
    if sys.platform != "win32" or importlib.util.find_spec("win32com") is None:
        return False
    import winreg
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "PowerPoint.Application"))
        return True
    except OSError:
        return False


class kPptTestBase(unittest.TestCase):

    def setUp(self):
        kS.Reset()
        kErrorReport.Reset()
        self.Root = os.path.join(SCRATCH, uuid.uuid4().hex[:8])
        os.makedirs(self.Root)

    def tearDown(self):
        shutil.rmtree(self.Root, ignore_errors=True)
        self.assertFalse(kS.ErrorMode, kS.Errors)
        kS.Reset()
        kErrorReport.Reset()


class kPptLocatorTests(kPptTestBase):

    def test_matches_part_of_full_path(self):
        self.assertTrue(kPptLocator.Matches("decks/Q3", r"C:\Work\Decks\Q3\Plan.pptx", "Plan.pptx"))
        self.assertTrue(kPptLocator.Matches(r"Q3\plan.PPTX", r"C:\Work\Decks\Q3\Plan.pptx", "Plan.pptx"))

    def test_matches_file_name_of_a_path(self):
        self.assertTrue(kPptLocator.Matches(r"D:\elsewhere\Plan.pptx", r"C:\Work\Plan.pptx", "Plan.pptx"))

    def test_no_match(self):
        self.assertFalse(kPptLocator.Matches("Other.pptx", r"C:\Work\Plan.pptx", "Plan.pptx"))
        self.assertFalse(kPptLocator.Matches(r"D:\x\Plan2.pptx", r"C:\Work\Plan.pptx", "Plan.pptx"))

    def test_resolve_existing_file_without_matching_open(self):
        Path = os.path.join(self.Root, "deck.pptx")
        with open(Path, "wb") as File:
            File.write(b"not a real deck; Resolve only checks that it exists")
        Mixed = Path.replace("\\", "/") if os.name == "nt" else Path
        self.assertEqual(kPptLocator.Resolve(Mixed, MatchOpen=False), os.path.normpath(Path))

    def test_resolve_missing_file_is_input_error(self):
        with self.assertRaises(ToolInputException) as Raised:
            kPptLocator.Resolve(os.path.join(self.Root, "absent.pptx"), MatchOpen=False)
        self.assertEqual(Raised.exception.ExitCode, 2)
        self.assertIn("File not found", str(Raised.exception))

    def test_resolve_returns_default_while_halted(self):
        kS.ErrorMode = True
        self.assertEqual(kPptLocator.Resolve("anything", MatchOpen=False), "")
        kS.ErrorMode = False


class kPptNormTests(kPptTestBase):

    def test_norm_is_absolute_and_uses_one_separator(self):
        Result = kPpt.Norm("some/dir/../deck.pptx")
        self.assertTrue(os.path.isabs(Result))
        self.assertEqual(Result, os.path.normpath(os.path.abspath("some/deck.pptx")))
        if os.name == "nt":
            self.assertNotIn("/", Result)

    def test_norm_while_halted(self):
        kS.ErrorMode = True
        self.assertEqual(kPpt.Norm("x"), "")
        kS.ErrorMode = False


@unittest.skipUnless(HasPowerPoint(), "needs pywin32 and PowerPoint")
class kPptComTests(kPptTestBase):
    """Builds a two-slide deck with PowerPoint, then reads it back through kPptSession."""

    def setUp(self):
        super().setUp()
        import pythoncom
        import win32com.client
        self.Deck = os.path.join(self.Root, "two-slides.pptx")
        pythoncom.CoInitialize()
        try:
            try:
                win32com.client.GetActiveObject("PowerPoint.Application")
                self.WasRunning = True
            except pythoncom.com_error:  # not running: the tests start it and must quit it
                self.WasRunning = False
            App = win32com.client.Dispatch("PowerPoint.Application")
            Pres = App.Presentations.Add(0)  # WithWindow=False
            Slide = Pres.Slides.Add(1, 1)  # ppLayoutTitle
            Slide.Shapes(1).TextFrame.TextRange.Text = "Hello title"
            Pres.Slides.Add(2, 12)  # ppLayoutBlank
            Pres.SaveAs(self.Deck)
            Pres.Close()
            if not self.WasRunning and App.Presentations.Count == 0:
                App.Quit()
            App = None
        finally:
            pythoncom.CoUninitialize()

    def tearDown(self):
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        try:
            try:
                App = win32com.client.GetActiveObject("PowerPoint.Application")
                if not self.WasRunning and App.Presentations.Count == 0:
                    App.Quit()
                App = None
            except pythoncom.com_error:  # already gone: nothing to close
                pass
        finally:
            pythoncom.CoUninitialize()
        super().tearDown()

    def test_session_reads_deck(self):
        with kPptSession(self.Deck) as Pres:
            self.assertIsNotNone(Pres)
            self.assertEqual(Pres.Slides.Count, 2)
            Titles = [Shape for Shape in kPpt.ShapeTexts(Pres.Slides(1).Shapes) if kPpt.IsTitle(Shape)]
            self.assertEqual(len(Titles), 1)
            self.assertEqual(Titles[0].TextFrame.TextRange.Text, "Hello title")
            self.assertEqual(kPpt.ShapeTexts(Pres.Slides(2).Shapes), [])
            self.assertEqual(kPpt.NotesText(Pres.Slides(2)), "")

    def test_resolve_falls_back_to_file_when_not_open(self):
        self.assertEqual(kPptLocator.Resolve(self.Deck), os.path.normpath(self.Deck))

    def test_resolve_finds_open_deck_by_name(self):
        with kPptSession(self.Deck) as Pres:
            self.assertIsNotNone(Pres)
            self.assertEqual(kPpt.Norm(kPptLocator.Resolve("two-slides.pptx")), os.path.normpath(self.Deck))


if __name__ == "__main__":
    unittest.main()
