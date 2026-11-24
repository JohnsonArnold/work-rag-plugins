"""Tests for managed source directories, diff scanning, explicit sync, and batch indexing."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from work_rag.application import Application
from work_rag.backup import copy_library
from work_rag.contracts import CoreError, ErrorCode
from work_rag.roots import browse_directories
from work_rag.storage import Store


class ManagedRootsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.data_dir = self.base / "library"
        self.work_a = self.base / "work_a"
        self.work_b = self.base / "work_b"
        self.work_a.mkdir()
        self.work_b.mkdir()
        self.app = Application(self.data_dir)

    def test_add_update_list_and_remove_roots(self):
        root_a = self.app.roots.add(self.work_a, name="日常资料")
        self.assertTrue(root_a.root_id.startswith("root_"))
        self.assertEqual(root_a.name, "日常资料")
        self.assertTrue(root_a.recursive)
        self.assertTrue(root_a.enabled)
        self.assertIn(".xlsx", root_a.suffixes)

        # Duplicate path rejected.
        with self.assertRaises(CoreError) as dup:
            self.app.roots.add(self.work_a)
        self.assertEqual(dup.exception.code, ErrorCode.CONFLICT)

        # Path inside data_dir rejected.
        with self.assertRaises(CoreError) as inside:
            self.app.roots.add(self.data_dir)
        self.assertEqual(inside.exception.code, ErrorCode.INVALID_ARGUMENT)

        # Update fields.
        updated = self.app.roots.update(
            root_a.root_id, name="电费与文档", recursive=False, suffixes=["md", ".TXT"], enabled=False
        )
        self.assertEqual(updated.name, "电费与文档")
        self.assertFalse(updated.recursive)
        self.assertFalse(updated.enabled)
        self.assertEqual(updated.suffixes, [".md", ".txt"])
        self.assertEqual(self.app.roots.list(enabled_only=True), [])
        self.assertEqual(len(self.app.roots.list()), 1)

        # Remove keeps imported sources untouched.
        removed = self.app.roots.remove(root_a.root_id)
        self.assertTrue(removed["removed"])
        self.assertEqual(self.app.roots.list(), [])

    def test_scan_sync_change_detection_missing_and_auto_index(self):
        (self.work_a / "notes.md").write_text("# 巡检\n每周记录机房温度\n![图](pic.png)\n", encoding="utf-8")
        (self.work_a / "pic.png").write_bytes(b"png-v1")
        (self.work_a / "~$temp.xlsx").write_bytes(b"office-lock")
        sub = self.work_a / "sub"
        sub.mkdir()
        (sub / "report.txt").write_text("月度总结：设备运行正常", encoding="utf-8")
        ignored = self.work_a / ".git"
        ignored.mkdir()
        (ignored / "config.txt").write_text("should be ignored", encoding="utf-8")

        root = self.app.roots.add(self.work_a, name="工作目录A", suffixes=[".md", ".txt"])
        scan1 = self.app.roots.scan(root.root_id)
        self.assertEqual(scan1["counts"]["new"], 2)
        self.assertEqual(scan1["counts"]["changed"], 0)
        self.assertEqual({item["name"] for item in scan1["items"]}, {"notes.md", "report.txt"})

        # Sync with auto_index=True makes files immediately searchable.
        sync1 = self.app.roots.sync(root.root_id, auto_index=True)
        self.assertEqual(sync1["imported"], 2)
        self.assertEqual(sync1["updated"], 0)
        self.assertEqual(sync1["indexed"], 2)
        self.assertEqual(sync1["errors"], 0)
        self.assertIsNotNone(self.app.roots.get(root.root_id).last_synced_at)

        hits = self.app.knowledge.search("机房温度").data["hits"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(len(self.app.knowledge.search("设备运行正常").data["hits"]), 1)

        # Re-scanning with no changes reports 0 new/changed.
        scan2 = self.app.roots.scan(root.root_id)
        self.assertEqual(scan2["counts"]["new"], 0)
        self.assertEqual(scan2["counts"]["changed"], 0)
        self.assertEqual(scan2["counts"]["unchanged"], 2)
        self.assertEqual(scan2["items"], [])

        # Modifying a Markdown relative attachment triggers "changed".
        (self.work_a / "pic.png").write_bytes(b"png-v2")
        # Removing report.txt triggers "missing" without deleting library snapshot.
        (sub / "report.txt").unlink()
        scan3 = self.app.roots.scan(root.root_id)
        self.assertEqual(scan3["counts"]["changed"], 1)
        self.assertEqual(scan3["counts"]["missing"], 1)

        sync2 = self.app.roots.sync(root.root_id, auto_index=True)
        self.assertEqual(sync2["updated"], 1)
        self.assertEqual(sync2["missing_on_disk"], 1)
        # Historical report.txt snapshot is still preserved and searchable!
        self.assertEqual(len(self.app.knowledge.search("设备运行正常").data["hits"]), 1)

        # Backup & restore preserves source_roots.
        backup_dir = self.base / "backup"
        copy_library(self.app.store, backup_dir)
        restored = Application(Store(backup_dir).data_dir)
        self.assertEqual(len(restored.roots.list()), 1)
        self.assertEqual(restored.roots.list()[0].name, "工作目录A")

    def test_selective_sync_and_browse_directories(self):
        f1 = self.work_b / "a.txt"
        f2 = self.work_b / "b.txt"
        f1.write_text("alpha", encoding="utf-8")
        f2.write_text("beta", encoding="utf-8")
        root = self.app.roots.add(self.work_b, suffixes=[".txt"])

        sync = self.app.roots.sync(root.root_id, paths=[str(f1)], auto_index=True)
        self.assertEqual(sync["imported"], 1)
        self.assertEqual(sync["skipped"], 1)
        self.assertEqual(len(self.app.sources.list()), 1)

        # Browse root drives/shortcuts and subdirectory.
        top = browse_directories(None)
        self.assertGreaterEqual(len(top["entries"]), 1)
        child = browse_directories(str(self.base))
        names = {entry["name"] for entry in child["entries"]}
        self.assertIn("work_a", names)
        self.assertIn("work_b", names)

    def test_cli_root_and_kb_index_and_task_update_request_id(self):
        (self.work_a / "doc.txt").write_text("CLI目录同步测试", encoding="utf-8")
        project = Path(__file__).resolve().parents[1]

        def run_cli(*args):
            proc = subprocess.run(
                [sys.executable, "-m", "work_rag", "--data-dir", str(self.data_dir), *args],
                capture_output=True, encoding="utf-8", cwd=project, timeout=20,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            return json.loads(proc.stdout)

        added = run_cli("root", "add", str(self.work_a), "--name", "目录1")["data"]
        root_id = added["root_id"]
        self.assertEqual(len(run_cli("root", "list")["data"]), 1)
        scanned = run_cli("root", "scan", root_id)["data"]
        self.assertEqual(scanned["counts"]["new"], 1)
        synced = run_cli("root", "sync", root_id, "--no-index")["data"]
        self.assertEqual(synced["imported"], 1)
        source_id = synced["results"][0]["source_id"]
        indexed = run_cli("kb", "index", source_id)
        self.assertEqual(indexed["coverage"], "success")
        self.assertEqual(len(run_cli("kb", "search", "目录同步")["data"]["hits"]), 1)

        # Verify task update --request-id idempotency via CLI.
        task_id = run_cli("task", "create", "--title", "T", "--content", "C")["data"]["task_id"]
        u1 = run_cli("task", "update", task_id, "--expected-revision", "1", "--status", "completed", "--request-id", "u1")["data"]
        u2 = run_cli("task", "update", task_id, "--expected-revision", "1", "--status", "completed", "--request-id", "u1")["data"]
        self.assertEqual(u1, u2)


if __name__ == "__main__":
    unittest.main()
