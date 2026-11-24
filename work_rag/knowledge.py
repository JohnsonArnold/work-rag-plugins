"""Version-pinned document reads, derivative storage and lookup."""
from base64 import urlsafe_b64decode, urlsafe_b64encode
from contextlib import closing
from hashlib import sha256
from io import BytesIO
from math import ceil, floor
import json
from pathlib import Path
from zipfile import ZipFile

from . import documents, xlsx
from .contracts import CoreError, ErrorCode, EvidenceRef, Locator, Result, canonical_json, new_id, require_id


def encode_cursor(values):
    return urlsafe_b64encode(canonical_json(values).encode()).decode().rstrip("=")


def decode_cursor(cursor, fields):
    try:
        if not isinstance(cursor, str) or len(cursor) > 2048:
            raise ValueError()
        values = json.loads(urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not isinstance(values, dict) or values.keys() != set(fields):
            raise ValueError()
        return values
    except (ValueError, TypeError) as error:
        raise CoreError(ErrorCode.INVALID_ARGUMENT, "Invalid cursor") from error


def checked_limit(limit, maximum):
    if type(limit) is not int or not 1 <= limit <= maximum:
        raise CoreError(ErrorCode.INVALID_ARGUMENT, f"Limit must be between 1 and {maximum}")


def file_kind(version):
    return Path(version.original_name).suffix.casefold()


class Knowledge:
    def __init__(self, store, sources):
        self.store = store
        self.sources = sources
        self._ocr = None

    def _get_artifact(self, connection, version_id, backend, scope, args):
        return connection.execute(
            """SELECT * FROM artifacts WHERE version_id=? AND backend=? AND scope=? AND args=?""",
            (version_id, backend, scope, args),
        ).fetchone()

    def _cache(self, version, backend, scope, params, produce, build_index=None):
        args = canonical_json(params)
        with closing(self.store.connect()) as connection:
            existing = self._get_artifact(connection, version.version_id, backend, scope, args)
        if existing is not None:
            return existing["artifact_id"], json.loads(existing["payload"])
        try:
            path = self.store.verify_blob(version.sha256, version.size)
            payload = produce(path)
        except CoreError as error:
            if error.code in (ErrorCode.UNSUPPORTED, ErrorCode.INTEGRITY):
                with self.store.transaction() as connection:
                    if self._get_artifact(connection, version.version_id, backend, scope, args) is None:
                        connection.execute("INSERT OR REPLACE INTO artifact_failures VALUES (?,?,?,?,?)",
                                           (version.version_id, backend, scope, args, canonical_json(error.to_dict())))
            raise
        artifact_id = new_id("artifact")
        with self.store.transaction() as connection:
            existing = self._get_artifact(connection, version.version_id, backend, scope, args)
            if existing is not None:
                return existing["artifact_id"], json.loads(existing["payload"])
            connection.execute("DELETE FROM artifact_failures WHERE version_id=? AND backend=? AND scope=? AND args=?",
                               (version.version_id, backend, scope, args))
            connection.execute(
                "INSERT INTO artifacts VALUES (?,?,?,?,?,?,?)",
                (artifact_id, version.version_id, backend, scope, args,
                 payload.get("coverage", "partial" if payload.get("warnings") else "success"), canonical_json(payload)),
            )
            if build_index:
                entries = build_index(payload)
                connection.executemany("INSERT INTO search_entries VALUES (?,?,?,?,?)",
                                       [(artifact_id, position, text, text.casefold(), canonical_json(locator))
                                        for position, (text, locator) in enumerate(entries)])
        return artifact_id, payload

    @staticmethod
    def _text_index(payload):
        entries = []
        start = 0
        for line in payload["text"].splitlines(keepends=True):
            if line.strip():
                entries.append((line, {"kind": "text", "start": start, "end": start + len(line)}))
            start += len(line)
        return entries

    @staticmethod
    def _sheet_index(payload):
        entries = []
        for sheet, data in payload["sheets"].items():
            for address, cell in data["cells"].items():
                value = cell.get("value", cell.get("cached"))
                if value is not None:
                    entries.append((str(value), {"kind": "xlsx", "sheet": sheet,
                                                 "cell_range": address + ":" + address}))
        return entries

    def _xlsx(self, version):
        import openpyxl
        if file_kind(version) != ".xlsx":
            raise CoreError(ErrorCode.UNSUPPORTED, "Source is not an XLSX workbook")
        return self._cache(version, "openpyxl/" + openpyxl.__version__, "workbook", {},
                           xlsx.extract, self._sheet_index)

    def _plain(self, version):
        kind = file_kind(version)
        if version.size > 16 * 1024**2 and kind in (".md", ".txt"):
            raise CoreError(ErrorCode.UNSUPPORTED, "Text exceeds 16 MiB")
        if kind == ".docx":
            backend, parser = "docx-xml/2", documents.read_docx
        elif kind in (".md", ".txt"):
            backend, parser = "utf8/1", documents.read_text
        else:
            raise CoreError(ErrorCode.UNSUPPORTED, "Source does not support text extraction")
        return self._cache(version, backend, "document", {}, parser, self._text_index)

    def _pdf(self, version, page=None):
        from pypdfium2.version import PYPDFIUM_INFO
        if file_kind(version) != ".pdf":
            raise CoreError(ErrorCode.UNSUPPORTED, "Source is not a PDF")
        return self._cache(version, "pdfium/" + str(PYPDFIUM_INFO), "metadata" if page is None else "page",
                           {"page": page}, lambda path: documents.read_pdf(path, page),
                           self._text_index if page is not None else None)

    def status(self, source_id, version_id=None):
        version = self.sources.version(source_id, version_id)
        with closing(self.store.connect()) as connection:
            artifacts = [dict(row) for row in connection.execute(
                """SELECT a.artifact_id,a.backend,a.scope,a.args,a.state,
                (SELECT count(*) FROM search_entries i WHERE i.artifact_id=a.artifact_id) AS indexed_entries
                FROM artifacts a WHERE version_id=? ORDER BY a.rowid""", (version.version_id,),
            )]
            failures = [dict(row) for row in connection.execute(
                "SELECT backend,scope,args,error FROM artifact_failures WHERE version_id=?",
                (version.version_id,),
            )]
        for item in artifacts + failures:
            item["args"] = json.loads(item["args"])
        for item in failures:
            item["error"] = json.loads(item["error"])
        return Result({"version": version, "artifacts": artifacts, "failures": failures})

    def describe(self, source_id, version_id=None):
        version = self.sources.version(source_id, version_id)
        kind = file_kind(version)
        if kind == ".xlsx":
            artifact, data = self._xlsx(version)
            sheets = [{"name": name, **{key: value for key, value in sheet.items() if key != "cells"}}
                      for name, sheet in data["sheets"].items()]
            return Result({"version": version, "artifact_id": artifact, "epoch": data["epoch"], "sheets": sheets},
                          coverage="partial" if any(s["unsupported_formulas"] for s in sheets) else "success")
        if kind == ".pdf":
            artifact, data = self._pdf(version)
            return Result({"version": version, "artifact_id": artifact, **data}, coverage="not_processed")
        if kind in (".docx", ".txt", ".md"):
            artifact, data = self._plain(version)
            return Result({"version": version, "artifact_id": artifact, "parts": data["parts"],
                           "media": data.get("media", []), "attachments": self.sources.attachments(version.version_id)},
                          warnings=data["warnings"], coverage="partial" if data["warnings"] else "success")
        if kind in (".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"):
            from PIL import Image
            path = self.store.verify_blob(version.sha256, version.size)
            with Image.open(path) as image:
                return Result({"version": version, "width": image.width, "height": image.height,
                               "frames": getattr(image, "n_frames", 1)}, coverage="not_processed")
        return Result({"version": version}, coverage="unsupported")

    def _evidence(self, version, artifact, locator, backend="extracted"):
        return EvidenceRef(version.source_id, version.version_id, version.sha256, locator, backend, artifact)

    def read_sheet(self, source_id, sheet, cell_range, version_id=None, limit=100, cursor=None):
        from openpyxl.utils.cell import get_column_letter, range_boundaries
        Locator("xlsx", sheet=sheet, cell_range=cell_range)
        checked_limit(limit, 500)
        start = 0
        expected_artifact = None
        if cursor is not None:
            values = decode_cursor(cursor, ("source", "version", "artifact", "sheet", "range", "start"))
            if (values["source"] != source_id or values["sheet"] != sheet or values["range"] != cell_range or
                    (version_id is not None and values["version"] != version_id)):
                raise CoreError(ErrorCode.INVALID_ARGUMENT, "Cursor does not match this request")
            version_id = values["version"]
            expected_artifact = require_id(values["artifact"], "artifact")
            start = values["start"]
        version = self.sources.version(source_id, version_id)
        artifact, data = self._xlsx(version)
        if expected_artifact is not None and expected_artifact != artifact:
            raise CoreError(ErrorCode.CONFLICT, "Parser changed; restart range read")
        if sheet not in data["sheets"]:
            raise CoreError(ErrorCode.NOT_FOUND, "Worksheet does not exist", sheet=sheet)
        left, top, right, bottom = range_boundaries(cell_range)
        width = right - left + 1
        cells = width * (bottom - top + 1)
        if type(start) is not int or not 0 <= start < cells:
            raise CoreError(ErrorCode.INVALID_RANGE, "Cursor range exceeds the selection")
        stop = min(start + limit, cells)
        item = data["sheets"][sheet]
        output = []
        for offset in range(start, stop):
            address = get_column_letter(left + offset % width) + str(top + offset // width)
            output.append(item["cells"].get(address, {"cell": address, "type": "blank", "value": None}))
        token = encode_cursor({"source": source_id, "version": version.version_id, "artifact": artifact,
                               "sheet": sheet, "range": cell_range, "start": stop}) if stop < cells else None
        header_columns = {get_column_letter(col) for col in range(left, min(right, left + 31) + 1)}
        header = [cell for marker, cell in item["cells"].items()
                  if marker.rstrip("0123456789") in header_columns
                  and int(marker.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")) <= min(top, 2)]
        first_cell, last_cell = output[0]["cell"], output[-1]["cell"]
        if start % width == 0 and (stop % width == 0 or stop == cells):
            page_locators = [Locator("xlsx", sheet=sheet, cell_range=f"{first_cell}:{last_cell}")]
        else:
            page_locators = []
            for position in range(start, stop, 1):
                if position != start and position % width:
                    continue
                boundary = min(stop, (position // width + 1) * width) - 1
                page_locators.append(Locator("xlsx", sheet=sheet, cell_range=(
                    output[position - start]["cell"] + ":" + output[boundary - start]["cell"])))
        return Result({"version": version, "artifact_id": artifact, "requested": cell_range,
                       "first_cell": output[0]["cell"], "last_cell": output[-1]["cell"],
                       "offset": start, "cells": output, "header_context": header, "merged": item["merged"]},
                      evidence=[self._evidence(version, artifact, location) for location in page_locators],
                      coverage="partial" if token or any(cell["cell"] in item["unsupported_formulas"] for cell in output) else "success",
                      next_cursor=token)

    def calculate(self, source_id, sheet, cell_range, operation, version_id=None, unit=None):
        result = self.read_sheet(source_id, sheet, cell_range, version_id, limit=500)
        if result.next_cursor is not None:
            raise CoreError(ErrorCode.INVALID_RANGE, "Calculation accepts at most 500 cells")
        cells = result.data["cells"]
        summary = xlsx.calculate(cells, operation, unit)
        summary["inputs"] = [{"cell": item["cell"], "value": item.get("cached", item.get("value")),
                              "value_source": "workbook_cache" if item["type"] == "f" else "original",
                              "included": item.get("cached", item.get("value")) is not None} for item in cells]
        warnings = ["Formula caches have unknown freshness"] if any(item["type"] == "f" for item in cells) else []
        return Result(summary, evidence=result.evidence, coverage=result.coverage, warnings=warnings)

    def read_text(self, source_id, version_id=None, page=None, start=0, limit=4000):
        checked_limit(limit, 10000)
        if type(start) is not int or start < 0:
            raise CoreError(ErrorCode.INVALID_RANGE, "Text offset must be nonnegative")
        version = self.sources.version(source_id, version_id)
        if page is None:
            artifact, data = self._plain(version)
        else:
            artifact, data = self._pdf(version, page)
        text = data["text"]
        if start > len(text):
            raise CoreError(ErrorCode.INVALID_RANGE, "Text offset exceeds the document")
        end = min(start + limit, len(text))
        locator = Locator("text", artifact_id=artifact, start=start, end=end)
        value = {key: item for key, item in data.items() if key not in ("text", "warnings")}
        return Result({**value, "version": version, "text": text[start:end], "start": start,
                       "end": end, "next_start": end if end < len(text) else None},
                      evidence=[self._evidence(version, artifact, locator)],
                      coverage=data.get("coverage", "partial" if data.get("warnings") else "success"),
                      warnings=data.get("warnings", []))

    def _image(self, version, page, rect):
        from PIL import Image
        path = self.store.verify_blob(version.sha256, version.size)
        if file_kind(version) == ".pdf":
            Locator("pdf", page=page, rect=rect)
            return documents.render_pdf(path, page, rect)
        if page is not None or file_kind(version) not in (".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"):
            raise CoreError(ErrorCode.UNSUPPORTED, "Image input requires a supported image or a PDF page")
        with Image.open(path) as image:
            if rect is not None:
                Locator("pdf", page=1, rect=rect)
                if rect[2] > image.width or rect[3] > image.height:
                    raise CoreError(ErrorCode.INVALID_RANGE, "Region exceeds image bounds")
                image = image.crop((floor(rect[0]), floor(rect[1]), ceil(rect[2]), ceil(rect[3])))
            image.thumbnail((3200, 3200))
            return image.convert("RGB"), None

    def render(self, source_id, page=None, version_id=None, rect=None):
        version = self.sources.version(source_id, version_id)
        image, scale = self._image(version, page, rect)
        buffer = BytesIO()
        image.save(buffer, "PNG")
        evidence = [EvidenceRef(source_id, version.version_id, version.sha256, Locator("pdf", page=page, rect=rect))] if page else []
        return buffer.getvalue(), Result({"version": version, "page": page, "scale": scale, "size": image.size},
                                         evidence=evidence, coverage="success")

    def render_attachment(self, source_id, name, version_id=None):
        from PIL import Image, UnidentifiedImageError

        if not isinstance(name, str) or not name:
            raise CoreError(ErrorCode.INVALID_ARGUMENT, "Provide an attachment name")
        version = self.sources.version(source_id, version_id)
        kind = file_kind(version)
        if kind == ".md":
            entry = next((item for item in self.sources.attachments(version.version_id)
                          if item["relative_path"] == name), None)
            if entry is None or entry["sha256"] is None:
                raise CoreError(ErrorCode.NOT_FOUND, "Attachment snapshot is missing")
            data = self.store.verify_blob(entry["sha256"], entry["size"])
        elif kind == ".docx":
            _, body = self._plain(version)
            if name not in body["media"]:
                raise CoreError(ErrorCode.NOT_FOUND, "Attachment is not present in the document")
            with ZipFile(self.store.verify_blob(version.sha256, version.size)) as archive:
                data = BytesIO(archive.read(name))
        else:
            raise CoreError(ErrorCode.UNSUPPORTED, "This source has no supported attachments")
        try:
            with Image.open(data) as image:
                image.thumbnail((1600, 1600))
                buffer = BytesIO()
                image.convert("RGBA").save(buffer, "PNG")
                return buffer.getvalue(), Result({"version": version, "attachment": name, "size": image.size})
        except (UnidentifiedImageError, Image.DecompressionBombError) as error:
            raise CoreError(ErrorCode.UNSUPPORTED, "Attachment is not a supported raster image") from error

    def ocr(self, source_id, page=None, version_id=None, rect=None):
        from importlib.metadata import version as package_version
        try:
            from rapidocr_onnxruntime import RapidOCR
            from numpy import asarray
        except ImportError as error:
            raise CoreError(ErrorCode.UNSUPPORTED, "Local OCR is unavailable; check the OCR extra and runtime libraries",
                            dependency=str(error)) from error
        version = self.sources.version(source_id, version_id)
        def recognize(_path):
            image, scale = self._image(version, page, rect)
            if self._ocr is None:
                self._ocr = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1)
            blocks, _ = self._ocr(asarray(image.convert("RGB"))[:, :, ::-1].copy())
            lines = [{"points": points, "text": text, "confidence": float(score)} for points, text, score in (blocks or [])]
            return {"text": "\n".join(line["text"] for line in lines), "lines": lines,
                    "page": page, "rect": rect, "scale": scale, "coordinates": "render_pixels",
                    "coverage": "success" if lines else "partial"}
        artifact, data = self._cache(version, "rapidocr/" + package_version("rapidocr-onnxruntime"),
                                     "ocr", {"page": page, "rect": rect}, recognize, self._text_index)
        locator = Locator("text", artifact_id=artifact, start=0, end=len(data["text"]))
        return Result(data, evidence=[self._evidence(version, artifact, locator, "ocr")],
                      coverage=data["coverage"])

    def search(self, query, limit=30, cursor=None):
        checked_limit(limit, 100)
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise CoreError(ErrorCode.INVALID_ARGUMENT, "Provide a nonempty search query")
        query = query.strip().casefold()
        with closing(self.store.connect()) as connection:
            connection.execute("BEGIN")
            generation = sha256()
            for row in connection.execute(
                """SELECT s.current_version_id,s.title,a.artifact_id FROM sources s
                LEFT JOIN artifacts a ON s.current_version_id=a.version_id
                WHERE NOT s.archived ORDER BY s.current_version_id,a.artifact_id"""
            ):
                generation.update(canonical_json(tuple(row)).encode())
            digest = generation.hexdigest()
            offset = 0
            if cursor:
                values = decode_cursor(cursor, ("generation", "query", "offset"))
                if values["generation"] != digest or values["query"] != query:
                    raise CoreError(ErrorCode.CONFLICT, "Search inputs changed; restart search")
                offset = values["offset"]
                if type(offset) is not int or not 0 <= offset < 2**63:
                    raise CoreError(ErrorCode.INVALID_ARGUMENT, "Invalid search offset")
            rows = connection.execute(
                """SELECT s.title,v.source_id,v.version_id,v.sha256,a.artifact_id,a.scope,i.text,i.locator
                FROM search_entries i JOIN artifacts a USING (artifact_id)
                JOIN source_versions v USING (version_id)
                JOIN sources s ON s.current_version_id=v.version_id
                WHERE NOT s.archived AND instr(i.normalized,?)>0 AND a.rowid = (
                    SELECT max(newer.rowid) FROM artifacts newer
                    WHERE newer.version_id=a.version_id AND newer.scope=a.scope AND newer.args=a.args)
                ORDER BY v.source_id,a.artifact_id,i.ordinal LIMIT ? OFFSET ?""", (query, limit+1, offset),
            ).fetchall()
            uncovered = connection.execute(
                """SELECT count(*) FROM sources s WHERE NOT s.archived AND NOT EXISTS (
                    SELECT 1 FROM artifacts a WHERE a.version_id=s.current_version_id AND (
                        (a.scope IN ('workbook','document') AND a.state='success')
                        OR (a.scope='metadata' AND json_extract(a.payload,'$.pages')=(
                            SELECT count(DISTINCT json_extract(p.args,'$.page')) FROM artifacts p
                            WHERE p.version_id=a.version_id AND p.state='success'
                            AND (p.scope='page' OR (p.scope='ocr' AND json_extract(p.args,'$.rect') IS NULL))
                        ))
                    )
                )"""
            ).fetchone()[0]
        hits, evidence = [], []
        for row in rows[:limit]:
            args = json.loads(row["locator"])
            if args["kind"] == "text":
                args["artifact_id"] = row["artifact_id"]
            evidence.append(EvidenceRef(row["source_id"], row["version_id"], row["sha256"],
                                        Locator(**args), "ocr" if row["scope"] == "ocr" else "extracted", row["artifact_id"]))
            index = row["text"].casefold().find(query)
            hits.append({"title": row["title"], "excerpt": row["text"][max(0, index-40):index+len(query)+40]})
        token = encode_cursor({"generation": digest, "query": query, "offset": offset+limit}) if len(rows) > limit else None
        return Result({"hits": hits, "sources_without_complete_index": uncovered}, evidence=evidence,
                      coverage="partial" if uncovered else "success", next_cursor=token)

    def resolve(self, reference):
        if not isinstance(reference, dict) or not isinstance(reference.get("locator"), dict):
            raise CoreError(ErrorCode.INVALID_ARGUMENT, "Provide a structured evidence reference")
        try:
            ref = EvidenceRef(**{**reference, "locator": Locator(**reference["locator"])})
        except TypeError as error:
            raise CoreError(ErrorCode.INVALID_ARGUMENT, "Invalid evidence fields") from error
        version = self.sources.version(ref.source_id, ref.version_id)
        if version.sha256 != ref.sha256:
            raise CoreError(ErrorCode.INTEGRITY, "Evidence checksum mismatch")
        self.store.verify_blob(version.sha256, version.size)
        locator = ref.locator
        value = {"version": version, "locator": locator,
                 "is_current": self.sources.get(ref.source_id).current_version_id == ref.version_id}
        if ref.artifact_id:
            with closing(self.store.connect()) as connection:
                artifact = connection.execute(
                    "SELECT * FROM artifacts WHERE artifact_id=? AND version_id=?",
                    (ref.artifact_id, ref.version_id),
                ).fetchone()
            if artifact is None:
                raise CoreError(ErrorCode.NOT_FOUND, "Artifact does not belong to this version")
            if ref.method != ("ocr" if artifact["scope"] == "ocr" else "extracted"):
                raise CoreError(ErrorCode.INVALID_ARGUMENT, "Evidence processing method does not match the artifact")
            data = json.loads(artifact["payload"])
            if locator.kind == "text" and "text" in data and locator.end <= len(data["text"]):
                value["text"] = data["text"][locator.start:locator.end]
                value["page"] = data.get("page")
            elif locator.kind == "xlsx" and locator.sheet in data.get("sheets", {}):
                from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries
                left, top, right, bottom = range_boundaries(locator.cell_range)
                selected = []
                for label, cell in data["sheets"][locator.sheet]["cells"].items():
                    row, col = coordinate_to_tuple(label)
                    if top <= row <= bottom and left <= col <= right:
                        selected.append(cell)
                        if len(selected) > 500:
                            raise CoreError(ErrorCode.INVALID_RANGE, "Evidence exceeds 500 cells")
                value["cells"] = selected
            else:
                raise CoreError(ErrorCode.INVALID_RANGE, "Evidence locator does not match the artifact")
        elif locator.kind == "pdf":
            self._image(version, locator.page, locator.rect)
        else:
            raise CoreError(ErrorCode.INVALID_ARGUMENT, "This evidence requires an artifact")
        return Result(value, evidence=[ref], coverage="success")
