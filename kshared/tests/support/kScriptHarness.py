"""Shared helpers for the script regression suites (vedit/tests, gemini/tests, veo/tests).

The suites run each script as a real child process with the interpreter running the tests, on small committed
fixtures, and compare its output with committed expected files.

- The child sees stubs/ first on PYTHONPATH: a fake google.genai (no network, controlled by STUB_MODE and the
  prompt text) and a fake static_ffmpeg (a no-op add_paths, so kFfmpeg.Ensure uses the ffmpeg on PATH).
  StubsAreImported() asserts the child really imports the stubs.
- No real key and no network: GEMINI_API_KEY / GOOGLE_API_KEY are removed (KeyEnv adds a dummy), HTTPS_PROXY and
  HTTP_PROXY point to a closed local port, so an accidental real call fails fast.
- Work folders live under <repo>/_scratch/<suite>-tests (git-ignored), never in the user profile.
- KSHAREDPY_UPDATE_EXPECTED=1 rewrites the expected files instead of comparing (review the diff before committing).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import uuid

SUPPORT = os.path.dirname(os.path.abspath(__file__))
STUBS = os.path.join(SUPPORT, "stubs")
REPO = os.path.dirname(os.path.dirname(os.path.dirname(SUPPORT)))
SCRATCH = os.path.join(REPO, "_scratch")
DUMMY_KEY = "dummy-test-key-not-real"
DEAD_PROXY = "http://127.0.0.1:9"
UPDATE = os.environ.get("KSHAREDPY_UPDATE_EXPECTED") == "1"
TIMEOUT = 180


def NewWorkFolder(Suite):
    """A fresh empty folder under <repo>/_scratch/<Suite>-tests."""
    Folder = os.path.join(SCRATCH, f"{Suite}-tests", uuid.uuid4().hex[:8])
    os.makedirs(Folder)
    return Folder


def RemoveWorkFolder(Folder):
    shutil.rmtree(Folder, ignore_errors=True)


def BaseEnv(**Extra):
    """The child environment: stubs first on PYTHONPATH, no API key, a dead proxy, reporting off."""
    Env = {Name: Value for Name, Value in os.environ.items()
           if Name not in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "STUB_MODE", "KSTUB_LOG", "KSTUB_VIDEO",
                           "PYTHONPATH", "NO_PROXY", "no_proxy")}
    Env.update({
        "PYTHONPATH": STUBS,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "KPS_ERROR_REPORT": "0",
        "HTTPS_PROXY": DEAD_PROXY,
        "HTTP_PROXY": DEAD_PROXY,
        "https_proxy": DEAD_PROXY,
        "http_proxy": DEAD_PROXY,
    })
    for Name, Value in Extra.items():
        if Value is None:
            Env.pop(Name, None)
        else:
            Env[Name] = Value
    return Env


def KeyEnv(**Extra):
    """BaseEnv plus the dummy GEMINI_API_KEY."""
    return BaseEnv(GEMINI_API_KEY=DUMMY_KEY, **Extra)


def Run(Script, Args, Cwd, Env):
    """Run Script with Args in Cwd. Returns (exit code, stdout, stderr), text with LF line ends."""
    Result = subprocess.run([sys.executable, Script] + [str(Arg) for Arg in Args], cwd=Cwd, env=Env,
                            capture_output=True, timeout=TIMEOUT)
    return (Result.returncode, Result.stdout.decode("utf-8").replace("\r\n", "\n"),
            Result.stderr.decode("utf-8").replace("\r\n", "\n"))


def StubsAreImported(Cwd):
    """The files the child imports for google.genai and static_ffmpeg (both must be under STUBS)."""
    Code = ("import google.genai, static_ffmpeg; "
            "print(google.genai.__file__); print(static_ffmpeg.__file__); print(google.genai.IS_KSHAREDPY_STUB)")
    Result = subprocess.run([sys.executable, "-c", Code], cwd=Cwd, env=BaseEnv(), capture_output=True,
                            text=True, timeout=TIMEOUT)
    return Result.stdout.splitlines()


def Normalise(Text, Folders):
    """Replace each folder in Folders ({token: path}) by its token, in native and forward-slash spelling, turn the
    backslashes of a line that held a token into slashes, and blank run times ("done in 0.1s")."""
    Lines = []
    for Line in Text.replace("\r\n", "\n").split("\n"):
        Hit = False
        for Token, Folder in Folders.items():
            for Spelling in {Folder, Folder.replace("\\", "/"), os.path.realpath(Folder),
                             os.path.realpath(Folder).replace("\\", "/")}:
                if Spelling in Line:
                    Line = Line.replace(Spelling, Token)
                    Hit = True
        if Hit:
            Line = Line.replace("\\", "/")
        Lines.append(Line)
    Text = "\n".join(Lines)
    return re.sub(r"done in \d+(\.\d+)?s", "done in <T>s", Text)


def ReadText(Path):
    with open(Path, encoding="utf-8", newline="") as File:
        return File.read().replace("\r\n", "\n")


def AssertMatches(Case, Actual, ExpectedPath):
    """Compare Actual with the committed expected file (rewrite it when KSHAREDPY_UPDATE_EXPECTED=1)."""
    if UPDATE:
        os.makedirs(os.path.dirname(ExpectedPath), exist_ok=True)
        with open(ExpectedPath, "w", encoding="utf-8", newline="\n") as File:
            File.write(Actual)
        return
    Case.assertTrue(os.path.isfile(ExpectedPath), f"expected file missing: {ExpectedPath}")
    Case.maxDiff = None
    Case.assertEqual(Actual, ReadText(ExpectedPath), f"differs from {ExpectedPath}")


def AssertJsonClose(Case, Actual, Expected, Tolerance, Where="$"):
    """Equal JSON structures; numbers may differ by Tolerance (media measurements vary across ffmpeg builds)."""
    if isinstance(Expected, bool) or not isinstance(Expected, (int, float)):
        Case.assertEqual(type(Actual), type(Expected), Where)
    if isinstance(Expected, dict):
        Case.assertEqual(sorted(Actual), sorted(Expected), Where)
        for Key in Expected:
            AssertJsonClose(Case, Actual[Key], Expected[Key], Tolerance, f"{Where}.{Key}")
    elif isinstance(Expected, list):
        Case.assertEqual(len(Actual), len(Expected), Where)
        for Index, (A, E) in enumerate(zip(Actual, Expected)):
            AssertJsonClose(Case, A, E, Tolerance, f"{Where}[{Index}]")
    elif isinstance(Expected, (int, float)) and not isinstance(Expected, bool):
        Case.assertAlmostEqual(float(Actual), float(Expected), delta=Tolerance, msg=Where)
    else:
        Case.assertEqual(Actual, Expected, Where)


def AssertJsonFileClose(Case, ActualPath, ExpectedPath, Tolerance):
    """AssertJsonClose on two files (rewrite the expected file when KSHAREDPY_UPDATE_EXPECTED=1)."""
    Actual = json.loads(ReadText(ActualPath))
    if UPDATE:
        AssertMatches(Case, ReadText(ActualPath), ExpectedPath)
        return
    AssertJsonClose(Case, Actual, json.loads(ReadText(ExpectedPath)), Tolerance)


def StubLog(Path):
    """The calls the fake google.genai logged (KSTUB_LOG), one dict per call."""
    if not os.path.isfile(Path):
        return []
    with open(Path, encoding="utf-8") as File:
        return [json.loads(Line) for Line in File if Line.strip()]
