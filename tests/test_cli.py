"""Cross-process management commands exercise the same persistent core."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from work_rag.storage import SCHEMA_VERSION


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "core"

    def run_cli(self, *args, expected=0):
        process = subprocess.run([sys.executable, "-m", "work_rag", "--data-dir", str(self.data), *args],
                                 capture_output=True, encoding="utf-8", cwd=Path(__file__).resolve().parents[1], timeout=20)
        self.assertEqual(process.returncode, expected, process.stdout + process.stderr)
        return json.loads(process.stdout if expected == 0 else process.stderr)

    def test_init_source_versions_and_tasks_survive_new_processes(self):
        self.assertEqual(self.run_cli("init")["data"]["schema_version"], SCHEMA_VERSION)
        input_file = self.root / "资料.txt"
        input_file.write_text("first", encoding="utf-8")
        first = self.run_cli("source", "import", str(input_file))
        self.assertEqual(first["coverage"], "not_processed")
        identity = first["data"]["source"]["source_id"]
        old = first["data"]["version"]["version_id"]
        self.assertFalse(self.run_cli("source", "import", str(input_file))["data"]["changed"])
        input_file.write_text("second", encoding="utf-8")
        second = self.run_cli("source", "import", str(input_file))["data"]
        self.assertEqual(second["source"]["source_id"], identity)
        self.assertEqual(second["version"]["sequence"], 2)
        show = self.run_cli("source", "show", identity, "--version-id", old)["data"]
        self.assertEqual(Path(show["snapshot_path"]).read_text(), "first")
        self.assertEqual(len(self.run_cli("source", "versions", identity)["data"]), 2)
        created = self.run_cli("task", "create", "--title", "开发测试", "--content", "不执行岗位业务", "--tag", "测试", "--request-id", "cli-create")["data"]
        self.assertEqual(created["title"], "开发测试")
        task = created["task_id"]
        self.assertEqual(self.run_cli("task", "get", task)["data"], created)
        updated = self.run_cli("task", "update", task, "--expected-revision", "1", "--status", "in_progress", "--due", "2026-11-21")["data"]
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(len(self.run_cli("task", "list", "--view", "unfinished")["data"]), 1)
        error = self.run_cli("task", "update", task, "--expected-revision", "1", "--title", "冲突", expected=2)
        self.assertEqual(error["error"]["code"], "conflict")
        cleared = self.run_cli("task", "update", task, "--expected-revision", "2", "--clear-due", "--clear-tags")["data"]
        self.assertIsNone(cleared["due"])
        self.assertEqual(cleared["tags"], [])
        report = self.run_cli("status")["data"]
        self.assertEqual(report["sqlite_integrity"], "ok")
        self.assertEqual(report["corrupt_or_missing"], [])

    def test_invalid_input_errors_and_no_unrequested_data(self):
        response = self.run_cli("task", "create", "--title", " ", "--content", "body", expected=2)
        self.assertEqual(response["error"]["code"], "invalid_argument")
        self.assertEqual(self.run_cli("task", "list")["data"], [])
        response = self.run_cli("source", "import", str(self.root / "missing"), expected=3)
        self.assertEqual(response["error"]["code"], "integrity_error")
        self.assertEqual(self.run_cli("source", "list")["data"], [])

    def test_help_and_version_do_not_initialize_data(self):
        for argument in ("--help", "--version"):
            result = subprocess.run([sys.executable, "-m", "work_rag", "--data-dir", str(self.data), argument],
                                    capture_output=True, encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(self.data.exists())
