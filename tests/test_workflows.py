"""Regression coverage for migrated data, stable evidence and the local MCP boundary."""
import asyncio
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from work_rag.application import Application
from work_rag.backup import copy_library
from work_rag.contracts import CoreError, new_id
from work_rag.storage import MIGRATIONS, Store


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_schema_one_migration_preserves_data_and_failed_upgrade_rolls_back(self):
        data = self.root / "v1"
        data.mkdir()
        path = data / "core.sqlite3"
        task_id, source_id, version_id = new_id("task"), new_id("src"), new_id("ver")
        with closing(sqlite3.connect(path)) as db, db:
            for statement in MIGRATIONS:
                db.execute(statement)
            db.execute("PRAGMA user_version=1")
            db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (task_id, "Original", "Content", "pending", None, None, "[]", 1, "now", "now"))
            db.execute("INSERT INTO sources VALUES (?,?,?,?)", (source_id, "Original source", version_id, "now"))
            db.execute("INSERT INTO source_versions VALUES (?,?,?,?,?,?,?)",
                       (version_id, source_id, 1, "a" * 64, 5, "original.txt", "now"))
        with patch("work_rag.storage.MIGRATION_2", ["CREATE TABLE transient (id int)", "invalid SQL"]):
            with self.assertRaises(sqlite3.Error):
                Store(data)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='transient'").fetchone())
        app = Application(data)
        self.assertEqual(app.tasks.get(task_id).title, "Original")
        self.assertEqual(app.sources.version(source_id).version_id, version_id)
        self.assertEqual(Application(data).journal.logs(task_id), [])

    def test_journal_replays_committed_writes_and_review_keeps_old_log_boundary(self):
        app = Application(self.root / "library")
        task_id = app.tasks.create("Task", "Body").task_id
        log = app.journal.append(task_id, "started", 1, request_id="request")
        review = app.journal.create_review([task_id], {"process": [{"kind": "user_statement", "text": "Started"}]})
        correction = app.journal.append(task_id, "corrected", 2, corrects=log["log_id"])
        update = app.tasks.update(task_id, 3, status="completed", request_id="completed")
        reopened = Application(app.store.data_dir)
        self.assertEqual(reopened.tasks.update(task_id, 3, status="completed", request_id="completed"), update)
        self.assertEqual(reopened.journal.append(task_id, "started", 1, request_id="request"), log)
        self.assertEqual(reopened.journal.logs(task_id), [log, correction])
        saved = reopened.journal.review(review["review_id"])["snapshots"][0]
        self.assertEqual(saved["task"]["revision"], 2)
        self.assertEqual(saved["logs"], [log])

    def test_attachment_change_updates_version_and_backup_retains_old_evidence(self):
        app = Application(self.root / "library")
        document = self.root / "manual.md"
        attachment = self.root / "diagram.png"
        document.write_text("说明：宽带服务\n![图](diagram.png)", encoding="utf8")
        attachment.write_bytes(b"old image")
        original = app.sources.import_file(document)
        source_id = original.source.source_id
        old = app.knowledge.read_text(source_id)
        citation = old.to_dict()["evidence"][0]
        attachment.write_bytes(b"new image")
        latest = app.sources.import_file(document)
        self.assertNotEqual(original.version.version_id, latest.version.version_id)
        self.assertEqual(original.version.sha256, latest.version.sha256)
        self.assertFalse(app.sources.import_file(document).changed)
        self.assertEqual(app.knowledge.search("宽带").data["hits"], [])
        app.knowledge.read_text(source_id)
        self.assertEqual(len(app.knowledge.search("宽带").data["hits"]), 1)
        app.sources.edit(source_id, archived=True)
        self.assertEqual(app.sources.catalog(), [])
        self.assertEqual(app.knowledge.search("宽带").data["hits"], [])
        copied = self.root / "copy"
        copy_library(app.store, copied)
        restored = self.root / "restored"
        copy_library(Store(copied), restored)
        restored = Application(restored)
        self.assertEqual(restored.knowledge.resolve(citation).data["text"], old.data["text"])
        manifest = restored.sources.attachments(original.version.version_id)[0]
        self.assertEqual(restored.store.verify_blob(manifest["sha256"], manifest["size"]).read_bytes(), b"old image")

    def test_workbook_paging_stays_on_old_version_after_business_cell_update(self):
        from openpyxl import Workbook

        app = Application(self.root / "library")
        path = self.root / "workbook.xlsx"
        workbook = Workbook()
        workbook.active.title = "记录"
        workbook.active.append(["表头", 2, 3])
        workbook.save(path)
        source_id = app.sources.import_file(path).source.source_id
        first = app.knowledge.read_sheet(source_id, "记录", "B1:C1", limit=1)
        workbook.active["C1"] = 30
        workbook.save(path)
        latest = app.sources.import_file(path)
        second = app.knowledge.read_sheet(source_id, "记录", "B1:C1", cursor=first.next_cursor)
        self.assertEqual(second.data["cells"][0]["value"], 3)
        self.assertEqual(app.knowledge.calculate(source_id, "记录", "B1:C1", "sum").data["value"], "32")
        self.assertEqual(app.knowledge.resolve(second.to_dict()["evidence"][0]).data["version"].sequence, 1)
        self.assertEqual(latest.version.sequence, 2)

    def test_docx_blocks_and_native_pdf_text_are_searchable(self):
        app = Application(self.root / "library")
        document = self.root / "sample.docx"
        namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        with ZipFile(document, "w") as archive:
            archive.writestr("word/document.xml", f"""<w:document xmlns:w="{namespace}"><w:body>
                <w:p><w:r><w:t>会议记录</w:t></w:r></w:p>
                <w:tbl><w:tr><w:tc><w:p><w:r><w:t>型号A12</w:t></w:r></w:p></w:tc>
                <w:tc><w:p><w:r><w:t>金额</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
                </w:body></w:document>""")
        imported = app.sources.import_file(document)
        source_id = imported.source.source_id
        desc = app.knowledge.describe(source_id)
        self.assertEqual([part["kind"] for part in desc.data["parts"]], ["paragraph", "table"])
        self.assertIn("型号A12\t金额", app.knowledge.read_text(source_id).data["text"])
        self.assertEqual(len(app.knowledge.search("型号").data["hits"]), 1)

        path = self.root / "text.pdf"
        stream = b"BT /F1 14 Tf 20 50 Td (Invoice 2026-11-20) Tj ET"
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
        header = b"%PDF-1.4\n"
        offsets = []
        for index, obj in enumerate(objects, 1):
            offsets.append(len(header))
            header += b"%d 0 obj\n" % index + obj + b"\nendobj\n"
        xref = b"xref\n0 6\n0000000000 65535 f \n"
        xref += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
        path.write_bytes(header + xref + b"trailer << /Size 6 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % len(header))
        pdf_id = app.sources.import_file(path).source.source_id
        result = app.knowledge.read_text(pdf_id, page=1)
        self.assertEqual(result.coverage, "success")
        self.assertIn("2026-11-20", result.data["text"])
        img, meta = app.knowledge.render(pdf_id, page=1, rect=(0, 0, 100, 50))
        self.assertEqual(meta.data["size"], (250, 125))
        self.assertTrue(img.startswith(b"\x89PNG"))

    def test_local_mcp_round_trip_and_write_retry(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from PIL import Image

        image = self.root / "image.png"
        Image.new("RGB", (12, 10), "white").save(image)
        library = self.root / "资料库 with spaces"
        app = Application(library)
        source_id = app.sources.import_file(image).source.source_id

        async def check():
            # Use each platform's documented entry point from an unrelated cwd.
            project = Path(__file__).resolve().parents[1]
            env = {**os.environ, "WORK_RAG_PYTHON": sys.executable}
            if os.name == "nt":
                pythonpath = [str(project)]
                if env.get("PYTHONPATH"):
                    pythonpath.append(env["PYTHONPATH"])
                env["PYTHONPATH"] = os.pathsep.join(pythonpath)
                executable = sys.executable
                args = ["-m", "work_rag", "--data-dir", str(library), "mcp"]
            else:
                executable = "/bin/sh"
                args = [str(project / "tools" / "run_mcp.sh"), str(library)]
            command = StdioServerParameters(command=executable, args=args, cwd=str(self.root), env=env)
            async with stdio_client(command) as (receive, send):
                async with ClientSession(receive, send) as session:
                    await session.initialize()
                    tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                    self.assertTrue(tools["task_list"].annotations.readOnlyHint)
                    self.assertNotIn("source_import", tools)
                    rendered = await session.call_tool("kb_render", {"source_id": source_id})
                    self.assertFalse(rendered.isError, rendered.content)
                    self.assertEqual(rendered.content[0].mimeType, "image/png")
                    args = {"title": "事项", "content": "多行\n正文", "request_id": "create"}
                    first = await session.call_tool("task_create", args)
                    second = await session.call_tool("task_create", args)
                    self.assertFalse(first.isError, first.content)
                    self.assertEqual(first.structuredContent, second.structuredContent)
                    task = first.structuredContent["data"]
                    self.assertEqual(task["content"], "多行\n正文")
                    args = {"task_id": task["task_id"], "content": "follow-up",
                            "expected_revision": 1, "request_id": "follow-up"}
                    log = await session.call_tool("task_append_log", args)
                    retry = await session.call_tool("task_append_log", args)
                    self.assertFalse(log.isError, log.content)
                    self.assertEqual(log.structuredContent, retry.structuredContent)
                    changed = await session.call_tool("task_append_log", {**args, "content": "different"})
                    self.assertTrue(changed.isError)
                    self.assertIn("conflict", changed.content[0].text)

        asyncio.run(check())
