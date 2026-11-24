"""Read-only sample inventory; stdlib only, not a production Office/PDF parser.

Run: python3 tools/inspect_samples.py /path/to/documents
Outputs structural evidence, not extracted full document contents or OCR results.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import re
import zipfile
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def inspect_xlsx(path: Path) -> dict:
    with zipfile.ZipFile(path) as book:
        strings = []
        if "xl/sharedStrings.xml" in book.namelist():
            strings = ["".join(node.itertext()) for node in ET.fromstring(
                book.read("xl/sharedStrings.xml")).findall("s:si", NS)]
        workbook = ET.fromstring(book.read("xl/workbook.xml"))
        relationships = {item.get("Id"): item.get("Target") for item in
                         ET.fromstring(book.read("xl/_rels/workbook.xml.rels"))}
        styles = ET.fromstring(book.read("xl/styles.xml")) if "xl/styles.xml" in book.namelist() else None
        result = {
            "kind": "xlsx", "date1904": None, "sheets": [],
            "external_links": sum(name.startswith("xl/externalLinks/externalLink") and name.endswith(".xml")
                                  for name in book.namelist()),
            "number_formats": [],
        }
        properties = workbook.find("s:workbookPr", NS)
        if properties is not None:
            result["date1904"] = properties.get("date1904", "0")
        if styles is not None:
            result["number_formats"] = [item.attrib for item in styles.findall("s:numFmts/s:numFmt", NS)]
        for sheet in workbook.findall("s:sheets/s:sheet", NS):
            target = relationships[sheet.get(f"{{{REL}}}id")]
            member = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
            tree = ET.fromstring(book.read(member))
            dimension = tree.find("s:dimension", NS)
            rows = []
            formulas = []
            errors = []
            types = Counter()
            row_count = 0
            nonempty_cells = 0
            for row in tree.findall("s:sheetData/s:row", NS):
                cells = []
                for cell in row.findall("s:c", NS):
                    raw = cell.findtext("s:v", default="", namespaces=NS)
                    formula = cell.find("s:f", NS)
                    kind = cell.get("t", "n")
                    text = raw
                    if kind == "s" and raw:
                        text = strings[int(raw)]
                    elif kind == "inlineStr":
                        inline = cell.find("s:is", NS)
                        text = "".join(inline.itertext()) if inline is not None else ""
                    if text or formula is not None:
                        nonempty_cells += 1
                        types[kind] += 1
                        cells.append({"cell": cell.get("r"), "value": text[:160], "type": kind,
                                      "style": cell.get("s")})
                    if formula is not None:
                        formulas.append({"cell": cell.get("r"), "formula": formula.text,
                                         "formula_attributes": formula.attrib,
                                         "cached_value": raw, "cached_type": kind})
                    if kind == "e":
                        errors.append({"cell": cell.get("r"), "error": raw})
                if cells:
                    row_count += 1
                    if len(rows) < 6:
                        rows.append(cells[:12])
            result["sheets"].append({
                "name": sheet.get("name"), "state": sheet.get("state", "visible"),
                "dimension": dimension.get("ref") if dimension is not None else None,
                "nonempty_rows": row_count, "nonempty_cells": nonempty_cells,
                "cell_types": dict(types), "preview_rows": rows,
                "merged_cells": [item.get("ref") for item in tree.findall("s:mergeCells/s:mergeCell", NS)],
                "formula_count": len(formulas),
                "formulas_without_cached_values": sum(not item["cached_value"] for item in formulas),
                "formula_examples": formulas[:8], "errors": errors[:12],
            })
        return result


def inspect_pdf(path: Path) -> dict:
    # These byte-pattern counts are hints only: compressed object streams may hide objects.
    # A PDF parser and page rendering/OCR are still needed for a reliable page/content count.
    raw = path.read_bytes()
    return {
        "kind": "pdf", "header": raw[:8].decode("ascii", errors="replace"),
        "raw_page_dictionary_hints": len(re.findall(rb"/Type\s*/Page\b", raw)),
        "raw_image_dictionary_hints": len(re.findall(rb"/Subtype\s*/Image\b", raw)),
        "raw_font_dictionary_hints": len(re.findall(rb"/Type\s*/Font\b", raw)),
        "encrypted_marker": bool(re.search(rb"/Encrypt\b", raw)),
        "limitation": "Raw markers only; no PDF text extraction, page rendering, or OCR performed.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect XLSX and PDF files in a directory.")
    parser.add_argument("directory", type=Path)
    directory = parser.parse_args().directory
    report = []
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        if path.suffix.lower() not in (".xlsx", ".pdf"):
            continue
        entry = {"source": path.name, "bytes": path.stat().st_size,
                 "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        entry.update(inspect_xlsx(path) if path.suffix.lower() == ".xlsx" else inspect_pdf(path))
        report.append(entry)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
