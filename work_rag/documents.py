"""Local text, DOCX and PDF extraction and page rendering."""
from contextlib import closing
from pathlib import Path
import posixpath
import re
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from .contracts import CoreError, ErrorCode
from .xlsx import check_archive


ATTACHMENT = re.compile(r"!?\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|([^\s)]+))(?:\s+[^)]+)?\)")
WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def markdown_links(text):
    return [first or second for first, second in ATTACHMENT.findall(text)]


def read_text(path):
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except UnicodeError as error:
        raise CoreError(ErrorCode.UNSUPPORTED, "Text must be UTF-8") from error
    return {"text": text, "parts": [{"name": "body", "start": 0, "end": len(text)}], "warnings": []}


def read_docx(path):
    try:
        check_archive(path)
        with ZipFile(path) as archive:
            names = [name for name in archive.namelist() if
                     re.fullmatch(r"word/(?:document|header\d+|footer\d+)\.xml", name)]
            if "word/document.xml" not in names:
                raise CoreError(ErrorCode.UNSUPPORTED, "DOCX is missing its main document")
            chunks, parts, warnings = [], [], []
            offset = 0
            for name in sorted(names, key=lambda name: (name != "word/document.xml", name)):
                root = ElementTree.fromstring(archive.read(name))
                parent = root.find(WORD + "body") if name == "word/document.xml" else root
                if parent is None:
                    warnings.append(f"{name}: missing body")
                    continue
                rel_name = "word/_rels/" + name.rsplit("/", 1)[1] + ".rels"
                relationships = {}
                if rel_name in archive.namelist():
                    relationships = {node.get("Id"): node.get("Target") for node in
                                     ElementTree.fromstring(archive.read(rel_name))
                                     if node.get("TargetMode") != "External" and node.get("Target")}
                def paragraph_text(paragraph):
                    return "".join(node.text or "" for node in paragraph.iter(WORD + "t"))

                for index, block in enumerate(parent):
                    if block.tag == WORD + "p":
                        kind = "paragraph"
                        text = paragraph_text(block)
                    elif block.tag == WORD + "tbl":
                        kind = "table"
                        text = "\n".join("\t".join(" ".join(paragraph_text(p) for p in cell.findall(WORD + "p"))
                                                  for cell in row.findall(WORD + "tc"))
                                         for row in block.findall(WORD + "tr"))
                        if block.find(".//" + WORD + "tbl") is not None:
                            warnings.append(f"{name}: nested tables are not included")
                    elif block.tag == WORD + "sectPr":
                        continue
                    else:
                        warnings.append(f"{name}: unsupported block {block.tag.rsplit('}', 1)[-1]}")
                        continue
                    images = []
                    for node in block.iter():
                        key = node.get(REL + "embed")
                        if key in relationships:
                            target = relationships[key]
                            member = target.lstrip("/") if target.startswith("/") else posixpath.normpath("word/" + target)
                            if member.startswith("word/media/") and member in archive.namelist():
                                images.append(member)
                    if block.find(".//" + WORD + "txbxContent") is not None:
                        warnings.append(f"{name}: text boxes are represented in XML order")
                    chunks.append(text + "\n")
                    parts.append({"name": name, "index": index, "kind": kind, "start": offset,
                                  "end": offset + len(text), "images": images})
                    offset += len(text) + 1
            return {"text": "".join(chunks), "parts": parts, "warnings": warnings,
                    "media": sorted(n for n in archive.namelist() if n.startswith("word/media/") and not n.endswith("/"))}
    except (BadZipFile, ElementTree.ParseError) as error:
        raise CoreError(ErrorCode.UNSUPPORTED, "Could not read this DOCX") from error


def read_pdf(path, page=None):
    import pypdfium2 as pdfium

    try:
        with pdfium.PdfDocument(path) as document:
            if page is None:
                return {"pages": len(document)}
            if type(page) is not int or not 1 <= page <= len(document):
                raise CoreError(ErrorCode.INVALID_RANGE, "PDF page is outside the document")
            with closing(document[page - 1]) as item:
                with closing(item.get_textpage()) as textpage:
                    text = textpage.get_text_bounded()
                from pypdfium2 import raw
                image_count = sum(1 for _ in item.get_objects(filter=[raw.FPDF_PAGEOBJ_IMAGE]))
                if not text.strip():
                    coverage = "not_processed"
                else:
                    coverage = "partial" if image_count else "success"
                return {"page": page, "rotation": item.get_rotation(), "size": item.get_size(),
                        "image_count": image_count, "text": text, "coverage": coverage}
    except pdfium.PdfiumError as error:
        raise CoreError(ErrorCode.UNSUPPORTED, "Could not read this PDF") from error


def render_pdf(path, page, rect=None):
    import pypdfium2 as pdfium

    try:
        with pdfium.PdfDocument(path) as document:
            if type(page) is not int or not 1 <= page <= len(document):
                raise CoreError(ErrorCode.INVALID_RANGE, "PDF page is outside the document")
            with closing(document[page - 1]) as item:
                width, height = item.get_size()
                if width <= 0 or height <= 0:
                    raise CoreError(ErrorCode.INVALID_RANGE, "PDF page has invalid dimensions")
                scale = min(2.5, 3200 / max(width, height))
                crop = (0, 0, 0, 0)
                if rect is not None:
                    if item.get_rotation() != 0:
                        raise CoreError(ErrorCode.UNSUPPORTED, "Regions on rotated PDF pages are not supported")
                    x0, y0, x1, y1 = rect
                    if x1 > width or y1 > height:
                        raise CoreError(ErrorCode.INVALID_RANGE, "Region exceeds the page bounds")
                    crop = (x0, height - y1, width - x1, y0)
                with closing(item.render(scale=scale, crop=crop)) as bitmap:
                    return bitmap.to_pil().copy(), scale
    except pdfium.PdfiumError as error:
        raise CoreError(ErrorCode.UNSUPPORTED, "Could not render this PDF") from error
