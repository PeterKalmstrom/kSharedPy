"""Tests for kFfmpeg and kDependency. Run: python -m unittest discover -s kshared/tests -p "test_kFfmpeg.py" -v

The probe and Run tests need ffmpeg/ffprobe on PATH and are skipped without them. Files go under
kSharedPy/_scratch/kshared-tests (never the user profile).
"""
import io
import os
import shutil
import subprocess
import sys
import unittest
import uuid
from unittest import mock

KSHARED = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRATCH = os.path.join(os.path.dirname(KSHARED), "_scratch", "kshared-tests")
sys.path.insert(0, KSHARED)
import kFfmpeg as kFfmpegModule  # noqa: E402
from kFfmpeg import kDependency, kFfmpeg  # noqa: E402
from kShared import ToolInputException, kS  # noqa: E402

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


class kDependencyTests(unittest.TestCase):

    def setUp(self):
        kS.Reset()

    def test_InstalledModule(self):
        self.assertTrue(kDependency.Has("json"))
        self.assertTrue(kDependency.Has("email.mime"))

    def test_MissingModuleAndMissingParent(self):
        self.assertFalse(kDependency.Has("no_such_module_kpy"))
        self.assertFalse(kDependency.Has("no_such_parent_kpy.child"))   # find_spec would raise on the child
        self.assertFalse(kS.ErrorMode)

    def test_ErrorModeIsFalse(self):
        kS.ErrorMode = True
        self.assertFalse(kDependency.Has("json"))
        kS.Reset()


class kFfmpegResolveTests(unittest.TestCase):

    def setUp(self):
        kS.Reset()

    def tearDown(self):
        kS.Reset()

    def test_FindPrefersPath(self):
        with mock.patch.object(kFfmpegModule.shutil, "which", return_value="/bin/ffmpeg"), \
                mock.patch.object(kFfmpeg, "AddStaticPaths") as AddStatic:
            self.assertEqual(kFfmpeg.Find(), "/bin/ffmpeg")
            AddStatic.assert_not_called()

    def test_FindFallsBackToStaticFfmpeg(self):
        with mock.patch.object(kFfmpegModule.shutil, "which", side_effect=[None, "/static/ffmpeg"]), \
                mock.patch.object(kFfmpeg, "AddStaticPaths", return_value=True):
            self.assertEqual(kFfmpeg.Find(), "/static/ffmpeg")

    def test_FindNoneAnywhere(self):
        with mock.patch.object(kFfmpegModule.shutil, "which", return_value=None), \
                mock.patch.object(kFfmpeg, "AddStaticPaths", return_value=False):
            self.assertEqual(kFfmpeg.Find(), "")
        self.assertFalse(kS.ErrorMode)

    def test_AddStaticPathsWithoutPackage(self):
        with mock.patch.object(kDependency, "Has", return_value=False):
            self.assertFalse(kFfmpeg.AddStaticPaths())

    def test_EnsureWithoutStaticFfmpegIsBadInput(self):
        with mock.patch.object(kDependency, "Has", return_value=False):
            with self.assertRaises(ToolInputException) as Caught:
                kFfmpeg.Ensure()
        self.assertIn("static-ffmpeg not installed", str(Caught.exception))
        self.assertEqual(Caught.exception.ExitCode, 2)

    def test_EnsureWhenBinariesStillMissing(self):
        with mock.patch.object(kDependency, "Has", return_value=True), \
                mock.patch.object(kFfmpeg, "AddStaticPaths", return_value=True), \
                mock.patch.object(kFfmpegModule.shutil, "which", return_value=None):
            with self.assertRaises(ToolInputException):
                kFfmpeg.Ensure()

    def test_RunFailureIsReported(self):
        Failure = subprocess.CalledProcessError(1, ["ffmpeg"])
        with mock.patch.object(kFfmpegModule.subprocess, "run", side_effect=Failure), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertFalse(kFfmpeg.Run(["-i", "x"]))
        self.assertTrue(kS.ErrorMode)
        self.assertEqual(kS.Errors[0]["location"], "kFfmpeg.Run")

    def test_RunPassesQuietArgs(self):
        with mock.patch.object(kFfmpegModule.subprocess, "run") as Run:
            self.assertTrue(kFfmpeg.Run(["-i", 5]))
        self.assertEqual(Run.call_args.args[0], ["ffmpeg", "-loglevel", "error", "-y", "-i", "5"])

    def test_ErrorModeDefaults(self):
        kS.ErrorMode = True
        self.assertEqual(kFfmpeg.Find(), "")
        self.assertEqual(kFfmpeg.Ensure(), ("", ""))
        self.assertFalse(kFfmpeg.Run([]))
        self.assertEqual(kFfmpeg.ProbeDurationSeconds("x"), 0.0)
        self.assertEqual(kFfmpeg.ProbeAudioStreamCount("x"), 0)


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg/ffprobe not on PATH")
class kFfmpegMediaTests(unittest.TestCase):

    def setUp(self):
        kS.Reset()
        self.Dir = os.path.join(SCRATCH, "ffmpeg-" + uuid.uuid4().hex[:8])
        os.makedirs(self.Dir)
        self.Clip = os.path.join(self.Dir, "clip.mp4")
        self.assertTrue(kFfmpeg.Run(["-f", "lavfi", "-i", "color=c=black:s=64x64:d=2",
                                     "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                                     "-shortest", "-c:v", "libx264", "-c:a", "aac", self.Clip]))

    def tearDown(self):
        shutil.rmtree(self.Dir, ignore_errors=True)
        kS.Reset()

    def test_Probes(self):
        self.assertAlmostEqual(kFfmpeg.ProbeDurationSeconds(self.Clip), 2.0, delta=0.2)
        self.assertEqual(kFfmpeg.ProbeAudioStreamCount(self.Clip), 1)

    def test_RunStripsAudio(self):
        Silent = os.path.join(self.Dir, "silent.mp4")
        self.assertTrue(kFfmpeg.Run(["-i", self.Clip, "-an", "-c:v", "copy", Silent]))
        self.assertEqual(kFfmpeg.ProbeAudioStreamCount(Silent), 0)
        self.assertFalse(kS.ErrorMode)


if __name__ == "__main__":
    unittest.main()
