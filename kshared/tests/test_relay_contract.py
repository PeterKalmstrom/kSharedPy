"""kErrorDetail.ToPayload against the ONE error-relay contract. Run:
python -m unittest discover -s X:\\Source\\Admin\\kSharedPy\\kshared\\tests -p test_relay_contract.py -v

The contract is a JSON Schema whose master lives with the relay in a PRIVATE repo
(X:\\Source\\kShared\\azure\\kErrorRelay\\contract\\error-report.schema.json; the relay's own test pins it to
kPayloadValidator.Rules). This repo is public and its CI cannot see that one, so a committed COPY sits at
kshared/contract/error-report.schema.json: the payload is validated against the copy everywhere, and the copy is
compared with the master wherever the master is on disk (skipped, with the reason, where it is not).

Standard library only - jsonschema may not be installed - so a small validator implements exactly the subset the
contract uses, and REFUSES any keyword it does not know rather than ignoring it. Two things differ from standard
JSON Schema, as the contract states: maxLength counts UTF-16 code units (x-lengthUnit "utf16" - the relay is
JavaScript), and x-maxBodyBytes limits the UTF-8 body. The body is built exactly as kErrorReport.Send builds it.
The same subset exists in TypeScript (kReusableCoreTS tests/support/kJsonSchemaSubset.ts) and PowerShell
(kPS tests/Test-kPSErrorDetail.ps1). Nothing is sent anywhere.
"""
import json
import os
import re
import sys
import unittest
from typing import Any

TESTS = os.path.dirname(os.path.abspath(__file__))
KSHARED = os.path.dirname(TESTS)
sys.path.insert(0, KSHARED)

from kShared import kErrorDetail, kErrorReport  # noqa: E402

COPY_PATH = os.path.join(KSHARED, "contract", "error-report.schema.json")
MASTER_PATH = r"X:\Source\kShared\azure\kErrorRelay\contract\error-report.schema.json"


class kSchemaSubset:
    """The contract's subset of JSON Schema 2020-12. Test support: a schema it cannot read raises."""

    ANNOTATIONS = {"$schema", "$id", "title", "description", "x-lengthUnit", "x-maxBodyBytes"}
    ASSERTIONS = {"type", "additionalProperties", "required", "properties", "maxLength", "minimum", "maximum",
                  "pattern"}

    def __init__(self, Schema: dict[str, Any]) -> None:
        self.Schema = Schema
        self.RefuseUnknown(Schema, "#")

    def RefuseUnknown(self, Node: dict[str, Any], Where: str) -> None:
        for Keyword in Node:
            if Keyword not in self.ANNOTATIONS | self.ASSERTIONS:
                raise ValueError(f"kSchemaSubset does not implement '{Keyword}' at {Where}")
        if Node.get("x-lengthUnit", "utf16") != "utf16" or Node.get("additionalProperties", False) is not False:
            raise ValueError(f"kSchemaSubset supports x-lengthUnit utf16 and additionalProperties false only ({Where})")
        for Name, Child in Node.get("properties", {}).items():
            self.RefuseUnknown(Child, f"{Where}/{Name}")

    @staticmethod
    def Utf16Length(Text: str) -> int:
        return len(Text.encode("utf-16-le", "surrogatepass")) // 2

    def ValidateBody(self, Body: bytes) -> list[str]:
        Problems = self.Validate(json.loads(Body.decode("utf-8")))
        Limit = self.Schema.get("x-maxBodyBytes")
        if isinstance(Limit, int) and len(Body) > Limit:
            Problems.append(f"(body) is {len(Body)} bytes, over x-maxBodyBytes {Limit}")
        return Problems

    def Validate(self, Value: Any) -> list[str]:
        Problems: list[str] = []
        if not isinstance(Value, dict):
            return ["(body) must be an object"]
        Properties = self.Schema.get("properties", {})
        Problems += [f"{Name} is required" for Name in self.Schema.get("required", []) if Name not in Value]
        for Name, Field in Value.items():
            if Name not in Properties:
                Problems.append(f"{Name} is not allowed")
            else:
                Problems += self.CheckField(Name, Properties[Name], Field)
        return Problems

    def CheckField(self, Name: str, Rule: dict[str, Any], Field: Any) -> list[str]:
        if Rule["type"] == "integer":
            if isinstance(Field, bool) or not isinstance(Field, int):
                return [f"{Name} must be an integer"]
            if Field < Rule.get("minimum", Field) or Field > Rule.get("maximum", Field):
                return [f"{Name} is out of range"]
            return []
        if Rule["type"] != "string":
            raise ValueError(f"kSchemaSubset does not implement type '{Rule['type']}'")
        if not isinstance(Field, str):
            return [f"{Name} must be a string"]
        Problems = []
        if self.Utf16Length(Field) > Rule.get("maxLength", 10 ** 9):
            Problems.append(f"{Name} is {self.Utf16Length(Field)} UTF-16 units, over {Rule['maxLength']}")
        if "pattern" in Rule and re.search(Rule["pattern"], Field) is None:
            Problems.append(f"{Name} does not match {Rule['pattern']}")
        return Problems


def LoadContract(Path: str) -> dict[str, Any]:
    with open(Path, encoding="utf-8") as File:
        return json.load(File)


def Detail(Message: str, Stack: str = "", Location: str = "Test.Contract") -> kErrorDetail:
    One = kErrorDetail(RuntimeError(Message), Location)
    if Stack:
        One.StackTrace = Stack
        One.ScriptName = "C:\\" + Stack[:100] + ".py"
        One.LineNumber = 99999999
    return One


class TestRelayContract(unittest.TestCase):
    def setUp(self) -> None:
        self.Contract = kSchemaSubset(LoadContract(COPY_PATH))
        self.Saved = (kErrorReport.Product, kErrorReport.Component, kErrorReport.ProductVersion)

    def tearDown(self) -> None:
        kErrorReport.Product, kErrorReport.Component, kErrorReport.ProductVersion = self.Saved

    def AssertMeets(self, One: kErrorDetail, What: str) -> None:
        Payload = One.ToPayload()
        Body = json.dumps(Payload).encode("utf-8")  # exactly as kErrorReport.Send builds it
        self.assertEqual(self.Contract.ValidateBody(Body), [], What)
        self.assertEqual(len(Payload), 10, f"{What}: all ten fields travel")

    def test_copy_matches_master(self) -> None:
        if not os.path.isfile(MASTER_PATH):
            self.skipTest(f"the relay's master contract is not on disk (private repo): {MASTER_PATH}")
        self.assertEqual(LoadContract(COPY_PATH), LoadContract(MASTER_PATH),
                         "kshared/contract/error-report.schema.json has drifted from the relay's master - copy it")

    def test_checker_can_refuse(self) -> None:
        self.assertEqual(len(self.Contract.Validate({"ProductName": " ", "ErrorDescription": "d"})), 1)
        self.assertTrue(self.Contract.Validate({"ProductName": "p", "ErrorDescription": "d", "ErrorNumber": True}))
        Big = json.dumps({"ProductName": "p", "ErrorDescription": "d", "Comments": "a" * 17000}).encode("utf-8")
        self.assertGreaterEqual(len(self.Contract.ValidateBody(Big)), 2)
        self.assertEqual(kSchemaSubset.Utf16Length("\U0001F600"), 2, "outside the BMP is two units, as the relay counts")

    def test_ordinary_and_empty(self) -> None:
        self.AssertMeets(Detail("a deliberate test error"), "ordinary")
        self.AssertMeets(Detail(""), "an error with no message")
        self.AssertMeets(Detail("   \n"), "an error whose message is blank")

    def test_huge_message_and_stack(self) -> None:
        Cases = {"ASCII": "a" * 50000, "three-byte characters": "\u20ac" * 50000,
                 "outside the BMP": "\U0001F600" * 30000, "characters JSON escapes": "<\"\\\n" * 15000,
                 "control characters": "\u0001" * 20000}
        for Case, Text in Cases.items():
            with self.subTest(Case=Case):
                kErrorReport.Product, kErrorReport.Component, kErrorReport.ProductVersion = Text, Text, Text
                self.AssertMeets(Detail(Text, Text, Location=Text), f"huge {Case}")


if __name__ == "__main__":
    unittest.main()
