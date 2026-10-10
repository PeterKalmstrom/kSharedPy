"""Tests for send_error_report.py. Run: python -m unittest discover -s X:\\Source\\Admin\\kSharedPy\\kshared\\tests -v

The script runs as a real process with stdin=DEVNULL (no terminal, as when Claude runs it). Reports are written under
kSharedPy\\_scratch\\send-report-tests; --yes posts only to kLocalRelay on 127.0.0.1 (KPS_ERROR_WEBHOOK).
"""
import json
import os
import shutil
import subprocess
import sys
import unittest
import uuid

TESTS = os.path.dirname(os.path.abspath(__file__))
KSHARED = os.path.dirname(TESTS)
SENDER = os.path.join(KSHARED, "send_error_report.py")
SCRATCH = os.path.join(os.path.dirname(KSHARED), "_scratch", "send-report-tests")
sys.path.insert(0, TESTS)

from kLocalRelay import kLocalRelay  # noqa: E402

PAYLOAD = {"ProductName": "kSharedPy (test)", "ProductVersion": "", "FunctionName": "kTest.Send",
           "ErrorFileName": "test.py", "ErrorNumber": 1, "ErrorDescription": "sample", "Comments": "c",
           "ClientName": "test", "ClientEmail": "", "OSVersion": "Python"}


class kSendErrorReportTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.Relay = kLocalRelay().Start()

    @classmethod
    def tearDownClass(cls):
        cls.Relay.Stop()

    def setUp(self):
        self.Root = os.path.join(SCRATCH, uuid.uuid4().hex[:8])
        os.makedirs(self.Root)
        self.Report = os.path.join(self.Root, "kpy-error-test.json")
        self.Received = len(self.Relay.Received)

    def tearDown(self):
        shutil.rmtree(self.Root, ignore_errors=True)

    def WriteReport(self, Data=None, Text=None):
        with open(self.Report, "w", encoding="utf-8") as File:
            File.write(Text if Text is not None else json.dumps(Data or {
                "question": "Do you want to send this error message?", "summary": [["what      : sample"]],
                "payloads": [PAYLOAD]}))

    def Sender(self, *Args, Path="/ok"):
        Env = dict(os.environ, KPS_ERROR_WEBHOOK=self.Relay.Url(Path), KPY_REPORT_DIR=self.Root, TMP=self.Root,
                   TEMP=self.Root, NO_PROXY="127.0.0.1,localhost")
        Env.pop("KPS_ERROR_REPORT", None)
        Env.pop("KPY_ERROR_LOG", None)
        return subprocess.run([sys.executable, SENDER, *Args], capture_output=True, text=True, encoding="utf-8",
                              env=Env, stdin=subprocess.DEVNULL)

    def NewRequests(self):
        return self.Relay.Received[self.Received:]

    def test_no_deletes_and_sends_nothing(self):
        self.WriteReport()
        Result = self.Sender(self.Report, "--no")
        self.assertEqual(Result.returncode, 0, Result.stderr)
        self.assertFalse(os.path.exists(self.Report))
        self.assertEqual(self.NewRequests(), [])

    def test_yes_posts_and_deletes(self):
        self.WriteReport()
        Result = self.Sender(self.Report, "--yes")
        self.assertEqual(Result.returncode, 0, Result.stderr)
        self.assertIn("Error report sent (sent, HTTP 202)", Result.stdout)
        self.assertFalse(os.path.exists(self.Report))
        Requests = self.NewRequests()
        self.assertEqual(len(Requests), 1)
        self.assertEqual(Requests[0]["body"], PAYLOAD)

    def test_yes_refused_keeps_file(self):
        self.WriteReport()
        Result = self.Sender(self.Report, "--yes", Path="/bad")
        self.assertEqual(Result.returncode, 1, Result.stderr)
        self.assertIn("Not sent:", Result.stdout)
        self.assertTrue(os.path.exists(self.Report))

    def test_no_answer_without_terminal_is_exit_2(self):
        self.WriteReport()
        Result = self.Sender(self.Report)
        self.assertEqual(Result.returncode, 2, Result.stderr)
        self.assertIn("Do you want to send this error message?", Result.stderr)
        self.assertTrue(os.path.exists(self.Report))
        self.assertEqual(self.NewRequests(), [])

    def test_missing_file_is_exit_2(self):
        Result = self.Sender(os.path.join(self.Root, "absent.json"), "--yes")
        self.assertEqual(Result.returncode, 2, Result.stderr)
        self.assertIn("not found", Result.stderr)

    def test_json_without_payloads_is_exit_2(self):
        self.WriteReport(Data={"question": "q", "payloads": []})
        Result = self.Sender(self.Report, "--yes")
        self.assertEqual(Result.returncode, 2, Result.stderr)
        self.assertIn("not an error report", Result.stderr)

    # A file that is not JSON at all is bad input just like one without payloads (fixed 2026-10-10: it used to be
    # reported as an unexpected error, exit 4 with a new pending report about the report file).
    def test_malformed_file_is_exit_2(self):
        self.WriteReport(Text="{ this is not json")
        Result = self.Sender(self.Report, "--yes")
        self.assertEqual(Result.returncode, 2, Result.stderr)
        self.assertEqual(self.NewRequests(), [])


if __name__ == "__main__":
    unittest.main()
