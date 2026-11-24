"""MCP hosts start from their own cwd, not the chat workspace."""
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest


LAUNCHER = Path(__file__).resolve().parents[1] / "tools" / "run_mcp.sh"


@unittest.skipUnless(os.name == "posix", "POSIX shell launcher")
class McpLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project with spaces"
        (self.project / "tools").mkdir(parents=True)
        self.launcher = self.project / "tools" / "run_mcp.sh"
        shutil.copyfile(LAUNCHER, self.launcher)
        self.env = {key: value for key, value in os.environ.items()
                    if key not in {"WORK_RAG_PYTHON", "WORK_RAG_DATA_DIR", "IT_RAG_PYTHON", "IT_RAG_DATA_DIR"}}

    def fake_python(self, relative):
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" "$@"\n', encoding="utf-8")
        path.chmod(0o755)
        return path

    def run_launcher(self, *args):
        return subprocess.run(["/bin/sh", str(self.launcher), *args], cwd=self.root,
                              env=self.env, text=True, capture_output=True)

    def test_private_runtime_and_default_data_are_independent_of_host_cwd(self):
        self.fake_python(".dev-data/run-python")
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [str(self.project), "-m", "work_rag",
                         "--data-dir", str(self.project / ".work-rag-data"), "mcp"])

    def test_venv_fallback_and_explicit_path_override_environment(self):
        self.fake_python(".venv/bin/python")
        self.env["WORK_RAG_DATA_DIR"] = str(self.root / "environment library")
        explicit = str(self.root / "explicit library")
        result = self.run_launcher(explicit)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-2:], [explicit, "mcp"])

    def test_previous_library_and_environment_still_work(self):
        old_library = self.project / ".it-rag-data"
        old_library.mkdir()
        self.env["IT_RAG_PYTHON"] = str(self.fake_python("previous python"))
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-2:], [str(old_library), "mcp"])

        self.env["IT_RAG_DATA_DIR"] = str(self.root / "previous environment library")
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-2:], [self.env["IT_RAG_DATA_DIR"], "mcp"])

        self.env["WORK_RAG_DATA_DIR"] = str(self.root / "current environment library")
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-2:], [self.env["WORK_RAG_DATA_DIR"], "mcp"])

    def test_absolute_runtime_override_and_environment_data(self):
        self.env["WORK_RAG_PYTHON"] = str(self.fake_python("custom python"))
        self.env["WORK_RAG_DATA_DIR"] = str(self.root / "environment library")
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-2:], [self.env["WORK_RAG_DATA_DIR"], "mcp"])

    def test_rejects_relative_data_and_extra_arguments_without_starting_python(self):
        for args in [("relative/library",), (str(self.root), "extra")]:
            with self.subTest(args=args):
                result = self.run_launcher(*args)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr)

    def test_missing_runtime_is_actionable_and_stays_off_protocol_stdout(self):
        result = self.run_launcher()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Python runtime unavailable", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_relative_runtime_override_is_rejected(self):
        self.env["WORK_RAG_PYTHON"] = "python"
        result = self.run_launcher()
        self.assertEqual(result.returncode, 2)
        self.assertIn("absolute executable path", result.stderr)
        self.assertEqual(result.stdout, "")
