"""Tests for kJsonFile. Run: python -m unittest discover -s kshared/tests -p "test_kJsonFile.py" -v

Files go under kSharedPy/_scratch/kshared-tests (never the user profile).
"""
import os
import shutil
import sys
import unittest
import uuid

KSHARED = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRATCH = os.path.join(os.path.dirname(KSHARED), "_scratch", "kshared-tests")
sys.path.insert(0, KSHARED)
from kJsonFile import kJsonFile  # noqa: E402
from kShared import ToolInputException, kS  # noqa: E402


class kJsonFileTests(unittest.TestCase):

    def setUp(self):
        kS.Reset()
        self.Dir = os.path.join(SCRATCH, "json-" + uuid.uuid4().hex[:8])
        os.makedirs(self.Dir)

    def tearDown(self):
        shutil.rmtree(self.Dir, ignore_errors=True)
        kS.Reset()

    def test_SaveThenLoadRoundTripsNonAscii(self):
        Path = os.path.join(self.Dir, "sub", "folder", "data.json")
        Data = {"name": "Kälmström – ✓", "items": [1, 2.5, None, True]}
        self.assertTrue(kJsonFile.Save(Path, Data))
        with open(Path, encoding="utf-8") as File:
            Text = File.read()
        self.assertIn("Kälmström – ✓", Text)          # ensure_ascii=False
        self.assertIn('\n  "items"', Text)             # indent 2
        self.assertEqual(kJsonFile.Load(Path, "data"), Data)

    def test_SaveIndentIsAdjustable(self):
        Path = os.path.join(self.Dir, "d.json")
        kJsonFile.Save(Path, {"a": 1}, Indent=4)
        with open(Path, encoding="utf-8") as File:
            self.assertIn('\n    "a"', File.read())

    def test_MissingFileIsBadInput(self):
        Path = os.path.join(self.Dir, "nothere.json")
        with self.assertRaises(ToolInputException) as Caught:
            kJsonFile.Load(Path, "EDL")
        self.assertEqual(str(Caught.exception), f"EDL not found: {Path}")
        self.assertEqual(Caught.exception.ExitCode, 2)
        self.assertFalse(kS.ErrorMode)

    def test_FolderIsNotAFile(self):
        with self.assertRaises(ToolInputException):
            kJsonFile.Load(self.Dir, "spec file")

    def test_MalformedJsonIsBadInput(self):
        Path = os.path.join(self.Dir, "bad.json")
        with open(Path, "w", encoding="utf-8") as File:
            File.write("{not json")
        with self.assertRaises(ToolInputException) as Caught:
            kJsonFile.Load(Path, "spec file")
        self.assertTrue(str(Caught.exception).startswith(f"spec file is not valid JSON: {Path} ("))
        self.assertEqual(Caught.exception.ExitCode, 2)
        self.assertFalse(kS.ErrorMode)

    def test_NonUtf8IsBadInput(self):
        Path = os.path.join(self.Dir, "latin1.json")
        with open(Path, "wb") as File:
            File.write(b'{"a": "\xe9"}')
        with self.assertRaises(ToolInputException):
            kJsonFile.Load(Path, "transcript")

    def test_ErrorModeReturnsSafeDefaults(self):
        kS.ErrorMode = True
        self.assertIsNone(kJsonFile.Load(os.path.join(self.Dir, "x.json"), "x"))
        self.assertFalse(kJsonFile.Save(os.path.join(self.Dir, "x.json"), {}))
        self.assertFalse(os.path.exists(os.path.join(self.Dir, "x.json")))


if __name__ == "__main__":
    unittest.main()
