"""Tests for kShared.py. Run: python -m unittest discover -s X:\\Source\\Admin\\kSharedPy\\kshared\\tests -v

In-process tests reset kS / kErrorReport around every test. Exit codes are checked by running small scripts as real
processes (they live under kSharedPy\\_scratch\\kshared-tests, never the user profile). Nothing is ever sent to the real
relay or any internet address: sending goes to kLocalRelay on 127.0.0.1, and the child processes either switch
reporting off (KPS_ERROR_REPORT=0) or point KPS_ERROR_WEBHOOK at 127.0.0.1.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import unittest
import uuid

TESTS = os.path.dirname(os.path.abspath(__file__))
KSHARED = os.path.dirname(TESTS)
SCRATCH = os.path.join(os.path.dirname(KSHARED), "_scratch", "kshared-tests")
sys.path.insert(0, KSHARED)
sys.path.insert(0, TESTS)
os.environ["NO_PROXY"] = "127.0.0.1,localhost"  # a system proxy must never see the local relay's traffic

from kLocalRelay import kLocalRelay  # noqa: E402
from kShared import kErrorDetail, kErrorReport, kS  # noqa: E402

UNUSED_URL = "http://127.0.0.1:9/never-contacted"

SCRIPT_HEAD = f'''import sys, threading
sys.path.insert(0, {KSHARED!r})
from kShared import ToolInputException, ToolReportableException, kRun, kS, kToolException
'''


class kGuarded:
    """A class written in the pattern, to see guarded methods return their defaults once halted."""

    def Value(self):
        if kS.ErrorMode:
            return None
        try:
            return 42
        except Exception as e:
            kS.GlobalErrorHandler(e, "kGuarded.Value")
            return None

    def Fail(self, Message):
        if kS.ErrorMode:
            return None
        try:
            raise ValueError(Message)
        except Exception as e:
            kS.GlobalErrorHandler(e, "kGuarded.Fail")
            return None


class kKSharedTestBase(unittest.TestCase):
    """Resets the latch and silences the handler's stderr for every in-process test."""

    def setUp(self):
        kS.Reset()
        kErrorReport.Reset()
        kErrorReport.LastStatus = ""
        self.SavedStderr = sys.stderr
        sys.stderr = io.StringIO()
        self.SavedLogPath = kS.LogPath
        kS.LogPath = ""

    def tearDown(self):
        sys.stderr = self.SavedStderr
        kS.LogPath = self.SavedLogPath
        kS.Reset()
        kErrorReport.Reset()


class kGlobalErrorHandlerTests(kKSharedTestBase):

    def test_handler_sets_error_mode_and_records_first_cause(self):
        self.assertFalse(kS.ErrorMode)
        kGuarded().Fail("first")
        kS.GlobalErrorHandler(RuntimeError("second"), "kTest.Second")
        self.assertTrue(kS.ErrorMode)
        self.assertEqual(len(kS.Errors), 2)
        self.assertEqual(kS.FirstError()["location"], "kGuarded.Fail")
        self.assertEqual(kS.FirstError()["message"], "first")
        self.assertFalse(kS.FirstError()["while_halted"])
        self.assertTrue(kS.Errors[1]["while_halted"])
        self.assertEqual(len(kErrorReport.Pending), 2)
        self.assertIn("ERROR in kGuarded.Fail", sys.stderr.getvalue())

    def test_guarded_methods_return_defaults_while_halted(self):
        Obj = kGuarded()
        self.assertEqual(Obj.Value(), 42)
        Obj.Fail("boom")
        self.assertIsNone(Obj.Value())
        Obj.Fail("ignored")  # the guard returns before the body: nothing new is recorded
        self.assertEqual(len(kS.Errors), 1)

    def test_reset_rearms(self):
        kGuarded().Fail("boom")
        kS.Reset()
        self.assertFalse(kS.ErrorMode)
        self.assertEqual(kS.Errors, [])
        self.assertEqual(kErrorReport.Pending, [])
        self.assertIsNone(kS.FirstError())
        self.assertEqual(kGuarded().Value(), 42)

    def test_handler_writes_log_when_log_path_set(self):
        os.makedirs(SCRATCH, exist_ok=True)
        kS.LogPath = os.path.join(SCRATCH, f"log-{uuid.uuid4().hex[:8]}.txt")
        try:
            kGuarded().Fail("logged")
            with open(kS.LogPath, encoding="utf-8") as File:
                self.assertIn("logged", File.read())
        finally:
            os.remove(kS.LogPath)

    def test_many_threads_report_at_once(self):
        Count = 64
        Start = threading.Barrier(Count)
        Threads = [threading.Thread(target=self.ReportFromThread, args=(Start, Index)) for Index in range(Count)]
        for Thread in Threads:
            Thread.start()
        for Thread in Threads:
            Thread.join()
        self.assertEqual(len(kS.Errors), Count)
        self.assertEqual(len(kErrorReport.Pending), Count)
        First = kS.FirstError()
        self.assertFalse(First["while_halted"])
        self.assertEqual([Record["while_halted"] for Record in kS.Errors].count(False), 1)
        self.assertEqual(First["message"], kErrorReport.Pending[0].Message)
        self.assertEqual(First["location"], kErrorReport.Pending[0].Location)
        self.assertEqual(len({Record["message"] for Record in kS.Errors}), Count)

    def ReportFromThread(self, Start, Index):
        Start.wait()
        kS.GlobalErrorHandler(RuntimeError(f"thread {Index}"), f"kTest.Thread{Index}")

    def test_unhandled_thread_exception_is_captured(self):
        Saved = (sys.excepthook, threading.excepthook, sys.unraisablehook, getattr(kS, "_captureInstalled", False))
        try:
            kS._captureInstalled = False
            kS.InstallUnhandledExceptionCapture()
            Thread = threading.Thread(target=self.RaiseInThread, name="worker-7")
            Thread.start()
            Thread.join()
            self.assertTrue(kS.ErrorMode)
            self.assertEqual(kS.FirstError()["location"], "[unhandled] thread worker-7")
            self.assertEqual(kS.FirstError()["message"], "escaped")
        finally:
            sys.excepthook, threading.excepthook, sys.unraisablehook, kS._captureInstalled = Saved

    def RaiseInThread(self):
        raise LookupError("escaped")


class kErrorDetailTests(kKSharedTestBase):

    LIMITS = {"ProductName": 200, "ProductVersion": 50, "FunctionName": 300, "ErrorFileName": 260,
              "ErrorDescription": 2000, "Comments": 9000, "ClientName": 200, "OSVersion": 200}

    def MakeHugeError(self):
        Error = None
        try:
            self.Recurse(200, "x" * 50000)
        except RuntimeError as e:
            Error = e
        return Error

    def Recurse(self, Depth, Message):
        if Depth == 0:
            raise RuntimeError(Message)
        self.Recurse(Depth - 1, Message)

    def test_payload_fields_within_relay_limits(self):
        SavedProduct, SavedComponent, SavedVersion = kErrorReport.Product, kErrorReport.Component, \
            kErrorReport.ProductVersion
        try:
            kErrorReport.Product = "P" * 500
            kErrorReport.Component = "C" * 500
            kErrorReport.ProductVersion = "9" * 500
            Payload = kErrorDetail(self.MakeHugeError(), "kHuge." + "L" * 1000, "doing " * 2000).ToPayload()
        finally:
            kErrorReport.Product, kErrorReport.Component, kErrorReport.ProductVersion = SavedProduct, \
                SavedComponent, SavedVersion
        for Key in list(self.LIMITS) + ["ErrorNumber", "ClientEmail"]:
            self.assertIn(Key, Payload)
        for Key, Limit in self.LIMITS.items():
            self.assertIsInstance(Payload[Key], str, Key)
            self.assertLessEqual(len(Payload[Key]), Limit, Key)
        self.assertIsInstance(Payload["ErrorNumber"], int)
        self.assertTrue(0 <= Payload["ErrorNumber"] <= 10000000)
        self.assertEqual(Payload["ErrorDescription"], "x" * 2000)
        self.assertEqual(Payload["ErrorFileName"], "test_kShared.py")

    def test_payload_without_traceback(self):
        Payload = kErrorDetail(ValueError(""), "kTest.NoTrace").ToPayload()
        self.assertEqual(Payload["ErrorNumber"], 0)
        self.assertEqual(Payload["ErrorFileName"], "")
        self.assertEqual(Payload["ErrorDescription"], "builtins.ValueError")
        json.dumps(Payload)


class kErrorReportTests(kKSharedTestBase):

    @classmethod
    def setUpClass(cls):
        cls.Relay = kLocalRelay().Start()

    @classmethod
    def tearDownClass(cls):
        cls.Relay.Stop()

    def test_refusal_text(self):
        self.assertIn("about 3 minute(s), HTTP 429", kErrorReport.RefusalText(429, {"retryAfterSeconds": 125}))
        self.assertIn("about 1 minute(s), HTTP 429", kErrorReport.RefusalText(429, {}))
        self.assertIn("too large", kErrorReport.RefusalText(413, {}))
        self.assertIn("refused the report: bad field, HTTP 400", kErrorReport.RefusalText(400, {"error": "bad field"}))
        self.assertIn("not in the expected form, HTTP 400", kErrorReport.RefusalText(400, {}))
        self.assertIn("switched off, HTTP 503", kErrorReport.RefusalText(503, {}))
        self.assertIn("try again later, HTTP 502", kErrorReport.RefusalText(502, {}))
        self.assertEqual(kErrorReport.RefusalText(418, {"error": "teapot"}), "the reporting address answered HTTP 418: teapot")
        self.assertEqual(kErrorReport.RefusalText(500, {}), "the reporting address answered HTTP 500")

    def Payload(self):
        return kErrorDetail(RuntimeError("sample"), "kTest.Send").ToPayload()

    def test_send_202(self):
        Before = len(self.Relay.Received)
        self.assertTrue(kErrorReport.Send(self.Relay.Url("/ok"), self.Payload()))
        self.assertEqual(kErrorReport.LastStatus, "sent, HTTP 202")
        self.assertEqual(self.Relay.Received[Before]["body"]["FunctionName"], "kTest.Send")

    def test_send_202_duplicate(self):
        self.assertTrue(kErrorReport.Send(self.Relay.Url("/dup"), self.Payload()))
        self.assertIn("already reported", kErrorReport.LastStatus)

    def test_send_400(self):
        self.assertFalse(kErrorReport.Send(self.Relay.Url("/bad"), self.Payload()))
        self.assertIn("ProductName is missing, HTTP 400", kErrorReport.LastStatus)

    def test_send_429(self):
        self.assertFalse(kErrorReport.Send(self.Relay.Url("/busy"), self.Payload()))
        self.assertIn("about 3 minute(s), HTTP 429", kErrorReport.LastStatus)

    def test_send_unreachable(self):
        self.assertFalse(kErrorReport.Send(UNUSED_URL, self.Payload()))
        self.assertIn("could not reach", kErrorReport.LastStatus)

    def test_send_all_uses_effective_url(self):
        Saved = os.environ.get("KPS_ERROR_WEBHOOK")
        os.environ["KPS_ERROR_WEBHOOK"] = self.Relay.Url("/ok")
        try:
            self.assertEqual(kErrorReport.SendAll([self.Payload(), self.Payload()]), 2)
        finally:
            if Saved is None:
                os.environ.pop("KPS_ERROR_WEBHOOK", None)
            else:
                os.environ["KPS_ERROR_WEBHOOK"] = Saved


class kEffectiveUrlTests(unittest.TestCase):
    """kErrorWebhook.url is looked for beside kShared.py, so a copy of kShared.py in a scratch folder is used."""

    def setUp(self):
        self.Root = os.path.join(SCRATCH, uuid.uuid4().hex[:8])
        os.makedirs(self.Root)
        shutil.copy(os.path.join(KSHARED, "kShared.py"), self.Root)

    def tearDown(self):
        shutil.rmtree(self.Root, ignore_errors=True)

    def Effective(self, Webhook=None):
        Env = dict(os.environ)
        Env.pop("KPS_ERROR_WEBHOOK", None)
        if Webhook is not None:
            Env["KPS_ERROR_WEBHOOK"] = Webhook
        Result = subprocess.run([sys.executable, "-c", "import kShared; print(kShared.kErrorReport.EffectiveUrl())"],
                                cwd=self.Root, capture_output=True, text=True, env=Env, stdin=subprocess.DEVNULL)
        self.assertEqual(Result.returncode, 0, Result.stderr)
        return Result.stdout.strip()

    def WriteUrlFile(self, Text):
        with open(os.path.join(self.Root, "kErrorWebhook.url"), "w", encoding="utf-8") as File:
            File.write(Text)

    def test_estate_url_by_default(self):
        self.assertEqual(self.Effective(), "https://kerrorrelay-pstt65n2.azurewebsites.net/api/report")

    def test_url_file_beats_estate(self):
        self.WriteUrlFile("http://127.0.0.1:1/from-file  \nsecond line\n")
        self.assertEqual(self.Effective(), "http://127.0.0.1:1/from-file")

    def test_env_beats_url_file(self):
        self.WriteUrlFile("http://127.0.0.1:1/from-file\n")
        self.assertEqual(self.Effective(" http://127.0.0.1:2/from-env "), "http://127.0.0.1:2/from-env")


class kRunMainTests(unittest.TestCase):
    """kRun.Main's exit codes, from real processes."""

    def setUp(self):
        self.Root = os.path.join(SCRATCH, uuid.uuid4().hex[:8])
        self.Reports = os.path.join(self.Root, "reports")
        os.makedirs(self.Root)

    def tearDown(self):
        shutil.rmtree(self.Root, ignore_errors=True)

    def Run(self, Body, Report=False, **Popen):
        Path = os.path.join(self.Root, "app.py")
        with open(Path, "w", encoding="utf-8") as File:
            File.write(SCRIPT_HEAD + textwrap.dedent(Body) + "\n\nkRun.Main(kApp)\n")
        Env = dict(os.environ, KPY_REPORT_DIR=self.Reports, TMP=self.Root, TEMP=self.Root,
                   NO_PROXY="127.0.0.1,localhost")
        Env.pop("KPY_ERROR_LOG", None)
        if Report:
            Env.pop("KPS_ERROR_REPORT", None)
            Env["KPS_ERROR_WEBHOOK"] = UNUSED_URL  # never contacted: there is no terminal, so it is only saved
        else:
            Env["KPS_ERROR_REPORT"] = "0"
        if Popen:
            return subprocess.Popen([sys.executable, Path], env=Env, stdin=subprocess.DEVNULL, **Popen)
        return subprocess.run([sys.executable, Path], capture_output=True, text=True, encoding="utf-8", env=Env,
                              stdin=subprocess.DEVNULL)

    def test_exit_0(self):
        Result = self.Run('''
            class kApp:
                def Run(self):
                    print("fine \u00e9")
                    return 0
            ''')
        self.assertEqual(Result.returncode, 0, Result.stderr)
        self.assertIn("fine \u00e9", Result.stdout)

    def test_exit_1_unexpected(self):
        Result = self.Run('''
            class kApp:
                def Run(self):
                    raise RuntimeError("surprise")
            ''')
        self.assertEqual(Result.returncode, 1)
        self.assertIn("ERROR in kRun.Main: RuntimeError: surprise", Result.stderr)

    def test_exit_1_when_guarded_method_reported_even_if_run_returns_0(self):
        Result = self.Run('''
            class kApp:
                def Run(self):
                    try:
                        raise ValueError("inside")
                    except Exception as e:
                        kS.GlobalErrorHandler(e, "kApp.Run")
                    return 0
            ''')
        self.assertEqual(Result.returncode, 1)
        self.assertIn("ERROR in kApp.Run", Result.stderr)

    def test_exit_2_tool_input(self):
        Result = self.Run('''
            class kApp:
                def Run(self):
                    raise ToolInputException("bad argument --x")
            ''')
        self.assertEqual(Result.returncode, 2)
        self.assertIn("bad argument --x", Result.stderr)
        self.assertNotIn("ERROR in", Result.stderr)

    def test_custom_exit_codes(self):
        Result = self.Run('''
            class kApp:
                def Run(self):
                    return 7
            ''')
        self.assertEqual(Result.returncode, 7)
        Result = self.Run('''
            class kApp:
                def Run(self):
                    raise ToolReportableException("does not fit", ExitCode=3)
            ''')
        self.assertEqual(Result.returncode, 3)
        self.assertIn("does not fit", Result.stderr)

    def test_exit_4_report_saved_when_no_terminal(self):
        Result = self.Run('''
            class kApp:
                def Run(self):
                    raise RuntimeError("needs a report")
            ''', Report=True)
        self.assertEqual(Result.returncode, 4, Result.stderr)
        Lines = [Line for Line in Result.stderr.splitlines() if Line.startswith("ERROR-REPORT-PENDING: ")]
        self.assertEqual(len(Lines), 1, Result.stderr)
        Saved = Lines[0][len("ERROR-REPORT-PENDING: "):].strip()
        self.assertEqual(os.path.normcase(os.path.dirname(Saved)), os.path.normcase(self.Reports))
        self.assertTrue(os.path.basename(Saved).startswith(kErrorReport.ReportFilePrefix))
        with open(Saved, encoding="utf-8") as File:
            Data = json.load(File)
        self.assertEqual(Data["question"], "Do you want to send this error message?")
        self.assertEqual(len(Data["summary"]), 1)
        self.assertEqual(len(Data["payloads"]), 1)
        self.assertEqual(Data["payloads"][0]["ErrorDescription"], "needs a report")
        self.assertEqual(Data["payloads"][0]["FunctionName"], "kRun.Main")

    def test_report_switched_off_saves_nothing(self):
        Result = self.Run('''
            class kApp:
                def Run(self):
                    raise RuntimeError("no report")
            ''', Report=False)
        self.assertEqual(Result.returncode, 1)
        self.assertNotIn("ERROR-REPORT-PENDING", Result.stderr)
        self.assertFalse(os.path.exists(self.Reports))

    def test_unhandled_thread_exception_is_captured(self):
        Result = self.Run('''
            class kApp:
                def Run(self):
                    Thread = threading.Thread(target=self.Work, name="worker-1")
                    Thread.start()
                    Thread.join()
                    return 0

                def Work(self):
                    raise LookupError("lost in a thread")
            ''')
        self.assertEqual(Result.returncode, 1)
        self.assertIn("ERROR in [unhandled] thread worker-1: LookupError: lost in a thread", Result.stderr)

    def test_closed_stdout_ends_quietly(self):
        Process = self.Run('''
            class kApp:
                def Run(self):
                    for Index in range(200000):
                        print(f"line {Index} " + "x" * 60)
                    return 0
            ''', stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        Process.stdout.readline()
        Process.stdout.close()  # the reader leaves, as `| head -1` does
        Stderr = Process.stderr.read().decode("utf-8", "replace")
        Process.stderr.close()
        Process.wait(timeout=60)
        self.assertEqual(Process.returncode, 0, Stderr)
        self.assertNotIn("ERROR", Stderr)
        self.assertNotIn("Traceback", Stderr)


if __name__ == "__main__":
    unittest.main()
