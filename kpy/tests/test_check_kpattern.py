"""Tests for check_kpattern.py. Run: python -m unittest discover -s X:\\Source\\Admin\\kSharedPy\\kpy\\tests -v

Each test writes one sample file under kSharedPy\\_scratch\\kpattern-tests and runs the audit on it as a real process,
so the exit code and the printed findings are what a person and CI see.
"""
import os
import shutil
import subprocess
import sys
import textwrap
import unittest
import uuid

KPY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIT = os.path.join(KPY, "check_kpattern.py")
SCRATCH = os.path.join(os.path.dirname(KPY), "_scratch", "kpattern-tests")

COMPLIANT = '''
from kShared import kS, kToolException


class kGood:
    """A compliant class."""

    def __init__(self, Path):
        try:
            self.Path = Path
        except Exception as e:
            kS.GlobalErrorHandler(e, "kGood.__init__")

    def Load(self):
        """Docstring first is allowed."""
        if kS.ErrorMode:
            return None
        try:
            return self.Path
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kGood.Load(file={self.Path})")
            return None


def Helper():
    if kS.ErrorMode:
        return 0
    try:
        return 1
    except Exception as e:
        kS.GlobalErrorHandler(e, "sample.Helper")
        return 0
'''


class kCheckKPatternTests(unittest.TestCase):

    def setUp(self):
        self.Root = os.path.join(SCRATCH, uuid.uuid4().hex[:8])
        os.makedirs(self.Root)
        self.Sample = os.path.join(self.Root, "sample.py")

    def tearDown(self):
        shutil.rmtree(self.Root, ignore_errors=True)

    def Audit(self, Text):
        with open(self.Sample, "w", encoding="utf-8") as File:
            File.write(textwrap.dedent(Text))
        Env = dict(os.environ, KPS_ERROR_REPORT="0", TMP=self.Root, TEMP=self.Root)
        return subprocess.run([sys.executable, AUDIT, self.Sample], capture_output=True, text=True, env=Env,
                              stdin=subprocess.DEVNULL)

    def AssertFinding(self, Text, Expected, Function="Bad"):
        Result = self.Audit(Text)
        self.assertEqual(Result.returncode, 1, Result.stdout + Result.stderr)
        self.assertIn(f": {Function}: {Expected}", Result.stdout)
        self.assertIn("1 file(s) checked", Result.stdout)

    def test_compliant_sample_passes(self):
        Result = self.Audit(COMPLIANT)
        self.assertEqual(Result.returncode, 0, Result.stdout + Result.stderr)
        self.assertIn("1 file(s) checked, 0 problem(s)", Result.stdout)

    def test_guard_missing(self):
        self.AssertFinding('''
            def Bad():
                try:
                    return 1
                except Exception as e:
                    kS.GlobalErrorHandler(e, "sample.Bad")
                    return 0
            ''', "1 guard missing")

    def test_guard_without_return(self):
        self.AssertFinding('''
            def Bad():
                if kS.ErrorMode:
                    print("halted")
                try:
                    return 1
                except Exception as e:
                    kS.GlobalErrorHandler(e, "sample.Bad")
                    return 0
            ''', "1 guard missing")

    def test_body_not_one_try(self):
        self.AssertFinding('''
            def Bad():
                if kS.ErrorMode:
                    return 0
                Value = 1
                try:
                    return Value
                except Exception as e:
                    kS.GlobalErrorHandler(e, "sample.Bad")
                    return 0
            ''', "2 the whole body")

    def test_last_handler_not_exception(self):
        self.AssertFinding('''
            def Bad():
                if kS.ErrorMode:
                    return 0
                try:
                    return 1
                except ValueError as e:
                    kS.GlobalErrorHandler(e, "sample.Bad")
                    return 0
            ''', "3 the last handler must catch Exception")

    def test_earlier_handler_must_only_reraise(self):
        self.AssertFinding('''
            def Bad():
                if kS.ErrorMode:
                    return 0
                try:
                    return 1
                except ValueError:
                    return 2
                except Exception as e:
                    kS.GlobalErrorHandler(e, "sample.Bad")
                    return 0
            ''', "3 handler for ValueError may only re-raise")

    def test_handler_without_report(self):
        self.AssertFinding('''
            def Bad():
                if kS.ErrorMode:
                    return 0
                try:
                    return 1
                except Exception as e:
                    print(e)
                    return 0
            ''', "3 the Exception handler must call kS.GlobalErrorHandler")

    def test_location_not_literal(self):
        self.AssertFinding('''
            def Bad():
                if kS.ErrorMode:
                    return 0
                try:
                    return 1
                except Exception as e:
                    Where = "sample.Bad"
                    kS.GlobalErrorHandler(e, Where)
                    return 0
            ''', '3 location must be a literal "sample.Bad"')

    def test_location_names_another_function(self):
        self.AssertFinding('''
            class kThing:
                def Bad(self):
                    if kS.ErrorMode:
                        return 0
                    try:
                        return 1
                    except Exception as e:
                        kS.GlobalErrorHandler(e, "kThing.Other")
                        return 0
            ''', '3 location "kThing.Other" does not name this function ("kThing.Bad")')

    def test_handler_not_ending_in_return(self):
        self.AssertFinding('''
            def Bad():
                if kS.ErrorMode:
                    return 0
                try:
                    return 1
                except Exception as e:
                    kS.GlobalErrorHandler(e, "sample.Bad")
            ''', "4 the Exception handler must end in return")

    def test_lambda_reported(self):
        Result = self.Audit(COMPLIANT + "\nSorter = lambda Item: Item\n")
        self.assertEqual(Result.returncode, 1)
        self.assertIn(": lambda: use a named method", Result.stdout)

    def test_nested_function_reported(self):
        self.AssertFinding('''
            def Outer():
                if kS.ErrorMode:
                    return 0
                try:
                    def Bad():
                        return 1
                    return Bad()
                except Exception as e:
                    kS.GlobalErrorHandler(e, "sample.Outer")
                    return 0
            ''', "nested function")

    def test_documented_exception_on_def_line(self):
        Result = self.Audit(COMPLIANT + '''
def Free():  # DOCUMENTED EXCEPTION: a pure helper, every caller is guarded
    return 1
''')
        self.assertEqual(Result.returncode, 0, Result.stdout)

    def test_documented_exception_on_line_above(self):
        Result = self.Audit(COMPLIANT + '''
# DOCUMENTED EXCEPTION: a pure helper, every caller is guarded
def Free():
    return 1
''')
        self.assertEqual(Result.returncode, 0, Result.stdout)

    def test_documented_exception_two_lines_above_does_not_count(self):
        Result = self.Audit(COMPLIANT + '''
# DOCUMENTED EXCEPTION: too far away

def Free():
    return 1
''')
        self.assertEqual(Result.returncode, 1)
        self.assertIn(": Free: 1 guard missing", Result.stdout)

    def test_infrastructure_classes_exempt(self):
        Result = self.Audit('''
class kS:
    @staticmethod
    def Anything():
        return 1
''')
        self.assertEqual(Result.returncode, 0, Result.stdout)


if __name__ == "__main__":
    unittest.main()
