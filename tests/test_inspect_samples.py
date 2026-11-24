"""Tests for the read-only inventory, not production parsing or OCR quality."""
import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.inspect_samples import inspect_pdf, inspect_xlsx


class SampleInventoryTests(unittest.TestCase):
    def make_book(self, path, target):
        namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        with zipfile.ZipFile(path, "w") as book:
            book.writestr("xl/workbook.xml", f'''<workbook xmlns="{namespace}"
                xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
                <workbookPr date1904="0"/><sheets>
                <sheet name="电费" sheetId="1" r:id="rId1"/>
                </sheets></workbook>''')
            book.writestr("xl/_rels/workbook.xml.rels", f'''<Relationships
                xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                <Relationship Id="rId1" Target="{target}"/>
                </Relationships>''')
            book.writestr("xl/sharedStrings.xml", f'''<sst xmlns="{namespace}">
                <si><r><t>日期</t></r><r><t>区间</t></r></si></sst>''')
            book.writestr("xl/worksheets/sheet1.xml", f'''<worksheet xmlns="{namespace}">
                <dimension ref="A1:E2"/><sheetData><row r="1">
                <c r="A1" t="s"><v>0</v></c>
                <c r="B1"><f t="shared" si="0" ref="B1:B2">A1+1</f><v>0</v></c>
                <c r="C1" t="inlineStr"><is><t>10/1-10/7</t></is></c>
                <c r="D1" t="e"><v>#DIV/0!</v></c>
                <c r="E1"/>
                </row><row r="2">
                <c r="B2"><f t="shared" si="0"/><v>2</v></c>
                <c r="C2"><f>1+2</f></c>
                </row></sheetData><mergeCells><mergeCell ref="A1:A2"/>
                </mergeCells></worksheet>''')

    def test_xlsx_preserves_shared_formulas_caches_and_intervals(self):
        with tempfile.TemporaryDirectory() as directory:
            for target in ("worksheets/sheet1.xml", "/xl/worksheets/sheet1.xml"):
                with self.subTest(target=target):
                    path = Path(directory) / "sample.xlsx"
                    self.make_book(path, target)
                    before = hashlib.sha256(path.read_bytes()).hexdigest()
                    report = inspect_xlsx(path)
                    sheet = report["sheets"][0]
                    self.assertEqual(report["date1904"], "0")
                    self.assertEqual(sheet["name"], "电费")
                    self.assertEqual(sheet["formula_count"], 3)
                    self.assertEqual(sheet["formulas_without_cached_values"], 1)
                    self.assertEqual(sheet["nonempty_cells"], 6)
                    self.assertEqual(sheet["merged_cells"], ["A1:A2"])
                    self.assertEqual(sheet["preview_rows"][0][0]["value"], "日期区间")
                    self.assertEqual(sheet["preview_rows"][0][2]["value"], "10/1-10/7")
                    self.assertEqual(sheet["formula_examples"][0]["cached_value"], "0")
                    shared = sheet["formula_examples"][1]
                    self.assertIsNone(shared["formula"])
                    self.assertEqual(shared["formula_attributes"], {"t": "shared", "si": "0"})
                    self.assertEqual(sheet["errors"], [{"cell": "D1", "error": "#DIV/0!"}])
                    self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_pdf_reports_only_raw_hints_and_does_not_modify_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "markers.pdf"
            raw = b"%PDF-1.4\n/Type /Pages /Type /Page /Subtype /Image /Type /Font /Encrypt"
            path.write_bytes(raw)
            report = inspect_pdf(path)
            self.assertEqual(report["raw_page_dictionary_hints"], 1)
            self.assertEqual(report["raw_image_dictionary_hints"], 1)
            self.assertEqual(report["raw_font_dictionary_hints"], 1)
            self.assertTrue(report["encrypted_marker"])
            self.assertIn("no PDF text extraction", report["limitation"])
            self.assertEqual(path.read_bytes(), raw)

    def test_pdf_empty_input_is_not_mistaken_for_extracted_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.pdf"
            path.write_bytes(b"")
            report = inspect_pdf(path)
            self.assertEqual(report["header"], "")
            self.assertEqual(report["raw_page_dictionary_hints"], 0)
            self.assertIn("Raw markers only", report["limitation"])


if __name__ == "__main__":
    unittest.main()
