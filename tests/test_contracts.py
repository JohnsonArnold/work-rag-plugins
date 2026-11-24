"""Independent F02 tests for JSON DTOs, typed locators and error envelopes."""
from datetime import datetime, timedelta
import json
import math
import re
import unittest

from work_rag.contracts import (
    CONTRACT_VERSION, CoreError, ErrorCode, EvidenceRef, ImportResult, Locator,
    Result, Source, SourceVersion, canonical_json, jsonable, new_id, now_iso, require_id,
)


class ContractTests(unittest.TestCase):
    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(CoreError) as caught:
            function(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)

    def evidence(self, **changes):
        values = dict(source_id=new_id("src"), version_id=new_id("ver"),
                      sha256="a" * 64, locator=Locator("pdf", page=1))
        values.update(changes)
        return EvidenceRef(**values)

    def test_stable_ids_are_namespaced_unique_and_validated(self):
        for kind in ("src", "ver", "task", "artifact", "evidence", "log", "review"):
            with self.subTest(kind=kind):
                identifiers = [new_id(kind) for unused in range(20)]
                self.assertEqual(20, len(set(identifiers)))
                for identifier in identifiers:
                    self.assertRegex(identifier, rf"^{kind}_[0-9a-f]{{32}}$")
                    self.assertEqual(identifier, require_id(identifier, kind))
        with self.assertRaises(ValueError):
            new_id("unknown")

    def test_invalid_ids_return_invalid_argument(self):
        for value in (None, 1, "", "src_" + "A" * 32, "src_" + "a" * 31,
                      "ver_" + "a" * 32, "src_" + "a" * 32 + "\n"):
            with self.subTest(value=value):
                self.assert_code(ErrorCode.INVALID_ARGUMENT, require_id, value, "src")

    def test_timestamps_include_beijing_offset(self):
        value = now_iso()
        parsed = datetime.fromisoformat(value)
        self.assertEqual(timedelta(hours=8), parsed.utcoffset())
        self.assertRegex(value, r"\.\d{6}\+08:00$")

    def test_xlsx_locator_minimum_maximum_and_json_round_trip(self):
        for bounds in ("A1:A1", "A1:XFD1048576", "XFD1048576:XFD1048576"):
            locator = Locator("xlsx", sheet="电费", cell_range=bounds)
            self.assertEqual(locator, Locator(**json.loads(canonical_json(locator))))

    def test_xlsx_locator_rejects_out_of_bounds_or_reversed_ranges(self):
        for bounds in ("A0:A1", "A1:XFE1", "A1:A1048577", "B1:A1", "A2:A1",
                       "a1:A1", "A1", "A01:A1", "AAAA1:AAAA1", "A1:A1\n", "A1:B2:C3"):
            with self.subTest(bounds=bounds):
                self.assert_code(ErrorCode.INVALID_RANGE, Locator, "xlsx", sheet="Sheet", cell_range=bounds)

    def test_xlsx_locator_rejects_wrong_json_types_and_cross_kind_fields(self):
        for changes in ({"sheet": ""}, {"sheet": " "}, {"sheet": 1},
                        {"cell_range": 1}, {"cell_range": ["A1:A1"]},
                        {"page": 1}, {"rect": [0, 0, 1, 1]}, {"start": 0}):
            values = dict(kind="xlsx", sheet="Sheet", cell_range="A1:A1")
            values.update(changes)
            with self.subTest(changes=changes):
                self.assert_code(ErrorCode.INVALID_RANGE, Locator, **values)

    def test_pdf_locator_accepts_one_based_page_and_top_left_rect(self):
        locator = Locator("pdf", page=1, rect=(0, 0, 12.5, 20))
        data = json.loads(canonical_json(locator))
        self.assertEqual([0, 0, 12.5, 20], data["rect"])
        restored = Locator(**data)
        self.assertEqual(data, jsonable(restored))
        self.assertEqual(2, Locator("pdf", page=2).page)

    def test_pdf_locator_rejects_bad_page_types_and_cross_kind_fields(self):
        for changes in ({"page": 0}, {"page": -1}, {"page": True}, {"page": 1.0},
                        {"page": "1"}, {"page": None}, {"sheet": "Sheet"},
                        {"cell_range": "A1:A1"}, {"artifact_id": new_id("artifact")}, {"end": 0}):
            values = dict(kind="pdf", page=1)
            values.update(changes)
            with self.subTest(changes=changes):
                self.assert_code(ErrorCode.INVALID_RANGE, Locator, **values)

    def test_pdf_locator_rejects_bad_rect_bounds_nonfinite_and_json_types(self):
        for rect in ((0, 0, 0, 1), (0, 0, 1, 0), (-1, 0, 1, 1), (0, -1, 1, 1),
                     (2, 0, 1, 1), (0, 2, 1, 1), (0, 0, math.inf, 1),
                     (0, 0, math.nan, 1), (False, 0, 1, 1), (0, 0, "1", 1),
                     (0, 0, 1), 1, "0011", {"x0": 0}):
            with self.subTest(rect=rect):
                self.assert_code(ErrorCode.INVALID_RANGE, Locator, "pdf", page=1, rect=rect)

    def test_text_locator_unicode_half_open_offsets_and_empty_slice(self):
        artifact_id = new_id("artifact")
        locator = Locator("text", artifact_id=artifact_id, start=1, end=3)
        self.assertEqual("费🙂", "电费🙂记录"[locator.start:locator.end])
        self.assertEqual(locator, Locator(**json.loads(canonical_json(locator))))
        self.assertEqual(0, Locator("text", artifact_id=artifact_id, start=0, end=0).end)

    def test_text_locator_rejects_invalid_offsets_and_cross_kind_fields(self):
        for changes in ({"start": -1}, {"end": -1}, {"start": 2, "end": 1},
                        {"start": True}, {"end": False}, {"start": 0.0},
                        {"end": "1"}, {"start": None}, {"page": 1}, {"sheet": "Sheet"}):
            values = dict(kind="text", artifact_id=new_id("artifact"), start=0, end=1)
            values.update(changes)
            with self.subTest(changes=changes):
                self.assert_code(ErrorCode.INVALID_RANGE, Locator, **values)

    def test_text_locator_requires_fixed_artifact_identity(self):
        for artifact in (None, "bad", new_id("src")):
            with self.subTest(artifact=artifact):
                self.assert_code(ErrorCode.INVALID_ARGUMENT, Locator, "text", artifact_id=artifact, start=0, end=1)

    def test_unknown_locator_kind_is_explicitly_unsupported(self):
        for kind in ("word", "", None):
            with self.subTest(kind=kind):
                self.assert_code(ErrorCode.UNSUPPORTED, Locator, kind)

    def test_evidence_has_fixed_source_version_checksum_and_locator_json(self):
        evidence = self.evidence()
        data = json.loads(canonical_json(evidence))
        self.assertEqual(evidence.source_id, data["source_id"])
        self.assertEqual(evidence.version_id, data["version_id"])
        self.assertEqual("a" * 64, data["sha256"])
        self.assertEqual("pdf", data["locator"]["kind"])

    def test_evidence_rejects_bad_ids_checksums_and_missing_derived_artifact(self):
        for changes in ({"source_id": "bad"}, {"version_id": "bad"},
                        {"sha256": "a" * 63}, {"sha256": "A" * 64},
                        {"method": "ocr"}, {"artifact_id": "bad"}):
            with self.subTest(changes=changes):
                self.assert_code(ErrorCode.INVALID_ARGUMENT, self.evidence, **changes)
        self.assertEqual("ocr", self.evidence(method="ocr", artifact_id=new_id("artifact")).method)

    def test_evidence_wrong_checksum_json_type_is_contract_error(self):
        for checksum in (None, 1, ["a"]):
            with self.subTest(checksum=checksum):
                self.assert_code(ErrorCode.INVALID_ARGUMENT, self.evidence, sha256=checksum)

    def test_result_all_coverage_states_and_json_envelope(self):
        for coverage in (None, "not_processed", "partial", "success", "failed", "unsupported"):
            with self.subTest(coverage=coverage):
                result = Result(data={"text": "中文", "bounds": (1, 2)},
                                evidence=[self.evidence()], coverage=coverage,
                                warnings=["remaining pages not processed"], next_cursor="fixed-version:2")
                envelope = result.to_dict()
                self.assertEqual(CONTRACT_VERSION, envelope["contract_version"])
                self.assertEqual(coverage, envelope["coverage"])
                self.assertEqual([1, 2], envelope["data"]["bounds"])
                self.assertEqual("fixed-version:2", envelope["next_cursor"])
                self.assertEqual(envelope, json.loads(canonical_json(result)))
                self.assertEqual("pdf", envelope["evidence"][0]["locator"]["kind"])

    def test_result_unknown_coverage_is_contract_error(self):
        for coverage in ("ok", "complete", "", 1):
            with self.subTest(coverage=coverage):
                self.assert_code(ErrorCode.INVALID_ARGUMENT, Result, data=None, coverage=coverage)

    def test_result_wrong_coverage_json_type_is_contract_error(self):
        for coverage in ([], {}):
            with self.subTest(coverage=coverage):
                self.assert_code(ErrorCode.INVALID_ARGUMENT, Result, data=None, coverage=coverage)

    def test_result_mutable_defaults_are_not_shared(self):
        one, two = Result(None), Result(None)
        one.warnings.append("one only")
        one.evidence.append(self.evidence())
        self.assertEqual([], two.warnings)
        self.assertEqual([], two.evidence)

    def test_canonical_json_is_deterministic_unicode_and_strict(self):
        self.assertEqual('{"a":["中文","conflict"],"z":1}',
                         canonical_json({"z": 1, "a": ("中文", ErrorCode.CONFLICT)}))
        self.assertEqual(canonical_json({"b": 2, "a": 1}), canonical_json({"a": 1, "b": 2}))
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonical_json({"number": value})

    def test_error_codes_have_stable_serializable_envelope(self):
        self.assertEqual(10, len(ErrorCode))
        for code in ErrorCode:
            with self.subTest(code=code):
                error = CoreError(code, "明确错误", requested="A1:A0", retryable=False)
                envelope = error.to_dict()
                self.assertEqual({"code": code.value, "message": "明确错误",
                                  "details": {"requested": "A1:A0", "retryable": False}}, envelope)
                self.assertEqual(envelope, json.loads(canonical_json(envelope)))

    def test_import_dto_is_json_not_python_object_serialization(self):
        source_id, version_id = new_id("src"), new_id("ver")
        source = Source(source_id, "标题", version_id, now_iso())
        version = SourceVersion(source_id, version_id, 1, "a" * 64, 3, "文件.txt", source.created_at)
        data = json.loads(canonical_json(ImportResult(source, version, True)))
        self.assertEqual(source, Source(**data["source"]))
        self.assertEqual(version, SourceVersion(**data["version"]))
        self.assertIs(True, data["changed"])


if __name__ == "__main__":
    unittest.main()
