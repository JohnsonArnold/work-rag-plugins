"""S5-DD2 JSON bridge contracts against temporary libraries only."""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from work_rag.application import Application
from work_rag.backup import copy_library
from work_rag.contracts import canonical_json
from work_rag.local_bridge import MAX_STDIN_BYTES, MAX_STDOUT_BYTES, dispatch
from work_rag.storage import MIGRATIONS, MIGRATION_2, SCHEMA_VERSION, Store, library_lock

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "tools" / "run_local_bridge.sh"


class LocalBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = Application(self.root / "library")
        self.binding = self.app.store.binding()
        pythonpath = [str(ROOT)]
        if os.environ.get("PYTHONPATH"):
            pythonpath.append(os.environ["PYTHONPATH"])
        self.env = {**os.environ, "WORK_RAG_PYTHON": sys.executable, "PYTHONPATH": os.pathsep.join(pythonpath)}

    def request(self, operation, params=None, **extra):
        request = {"protocol_version": "1", "call_id": "call_test", "operation": operation,
                   "params": {} if params is None else params}
        if operation in {"task.create", "task.update", "task.append_log"}:
            request["binding"] = self.binding
        return {**request, **extra}

    def call(self, operation, params=None, *, app=None, **extra):
        return dispatch(app or self.app, self.request(operation, params, **extra))

    def data(self, operation, params=None, **extra):
        result = self.call(operation, params, **extra)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["protocol_version"], "1")
        self.assertEqual(set(result["result"]), {"data", "contract_version", "warnings", "next_cursor"})
        self.assertEqual(result["result"]["contract_version"], "1.0")
        return result["result"]["data"]

    def create(self, **params):
        return self.data("task.create", {"title": "事项", "content": "正文", "request_id": "create", **params})

    def process(self, raw, *, directory=None):
        return subprocess.run(self.launch_command(directory or self.app.store.data_dir),
                              cwd=self.root, env=self.env, input=raw, capture_output=True, timeout=15)

    @staticmethod
    def launch_command(directory):
        if os.name == "nt":
            return [sys.executable, "-m", "work_rag.local_bridge", "--data-dir", str(directory)]
        return ["/bin/sh", str(LAUNCHER), str(directory)]

    def test_all_six_operations_use_same_core_data_and_bounded_receipts(self):
        created = self.create(due="2026-11-23", tags=["IT"])
        self.assertEqual(set(created), {"task_id", "revision", "status"})
        self.assertEqual(created["revision"], 1)
        task_id = created["task_id"]
        task = self.data("task.get", {"task_id": task_id})
        self.assertEqual(task["content"], "正文")
        listing = self.call("task.list", {"view": "unfinished", "query": "事项"})
        self.assertEqual([task], listing["result"]["data"])
        self.assertTrue(listing["result"]["warnings"])
        log = self.data("task.append_log", {"task_id": task_id, "content": "跟进", "expected_revision": 1,
                                           "request_id": "follow"})
        self.assertEqual(set(log), {"task_id", "revision", "log_id"})
        self.assertEqual(log["revision"], 2)
        updated = self.data("task.update", {"task_id": task_id, "expected_revision": 2,
                                           "changes": {"status": "completed", "due": None, "tags": []},
                                           "request_id": "done"})
        self.assertEqual(updated["revision"], 3)
        self.assertEqual(self.app.tasks.get(task_id).status, "completed")
        logs = self.data("task.logs", {"task_id": task_id, "after": 0})
        self.assertEqual(logs[0]["task_revision"], 2)
        self.assertEqual(self.data("task.logs", {"task_id": task_id, "after": 2}), [])

    def test_sparse_log_revisions_and_correction(self):
        task_id = self.create()["task_id"]
        self.app.tasks.update(task_id, 1, title="changed")
        first = self.data("task.append_log", {"task_id": task_id, "content": "first", "expected_revision": 2,
                                             "request_id": "log1"})
        self.app.tasks.update(task_id, 3, title="again")
        second = self.data("task.append_log", {"task_id": task_id, "content": "correction", "expected_revision": 4,
                                              "corrects": first["log_id"], "request_id": "log2"})
        logs = self.data("task.logs", {"task_id": task_id, "after": 3})
        self.assertEqual([entry["task_revision"] for entry in logs], [5])
        self.assertEqual(logs[0]["log_id"], second["log_id"])

    def test_historical_replays_succeed_after_subsequent_cancellation(self):
        created = self.create()
        task_id = created["task_id"]
        edit = {"task_id": task_id, "expected_revision": 1, "changes": {"title": "edited"}, "request_id": "edit"}
        updated = self.data("task.update", edit)
        follow = {"task_id": task_id, "expected_revision": 2, "content": "follow", "request_id": "follow"}
        appended = self.data("task.append_log", follow)
        self.app.tasks.update(task_id, 3, status="cancelled")
        restarted = Application(self.app.store.data_dir)
        self.assertEqual(self.data("task.create", {
            "title": "事项", "content": "正文", "request_id": "create"}, app=restarted), created)
        self.assertEqual(self.data("task.update", edit, app=restarted), updated)
        self.assertEqual(self.data("task.append_log", follow, app=restarted), appended)
        self.assertEqual(restarted.tasks.get(task_id).revision, 4)
        self.assertEqual(restarted.tasks.get(task_id).status, "cancelled")

    def test_request_id_conflict_and_stale_revision_do_not_write(self):
        task_id = self.create()["task_id"]
        changed = self.call("task.create", {"title": "different", "content": "正文", "request_id": "create"})
        self.assertFalse(changed["ok"])
        self.assertEqual(changed["error"]["code"], "conflict")
        for operation, params in [
            ("task.update", {"task_id": task_id, "expected_revision": 2, "changes": {"title": "new"}, "request_id": "bad"}),
            ("task.append_log", {"task_id": task_id, "expected_revision": 2, "content": "new", "request_id": "bad"}),
        ]:
            result = self.call(operation, params)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "conflict")
        self.assertEqual(self.app.tasks.get(task_id).revision, 1)
        self.assertEqual(self.app.journal.logs(task_id), [])

    def test_update_and_log_retries_reject_changed_frozen_input(self):
        task_id = self.create()["task_id"]
        edit = {"task_id": task_id, "expected_revision": 1, "changes": {"title": "edit"}, "request_id": "edit"}
        self.data("task.update", edit)
        conflicting_edit = self.call("task.update", {**edit, "changes": {"title": "different"}})
        follow = {"task_id": task_id, "expected_revision": 2, "content": "follow", "request_id": "follow"}
        self.data("task.append_log", follow)
        conflicting_follow = self.call("task.append_log", {**follow, "content": "different"})
        for result in (conflicting_edit, conflicting_follow):
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "conflict")
        self.assertEqual(self.app.tasks.get(task_id).revision, 3)
        self.assertEqual(len(self.app.journal.logs(task_id)), 1)

    def test_read_discovery_and_optional_binding_check(self):
        result = self.call("task.list")
        self.assertEqual(result["binding"], self.binding)
        self.assertEqual(self.call("task.list", binding=self.binding)["binding"], self.binding)
        stale = {**self.binding, "restore_generation": "generation_stale"}
        result = self.call("task.list", binding=stale)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["details"]["reason"], "binding_mismatch")

    def test_binding_survives_normal_restart_and_reads_do_not_migrate_or_fsync(self):
        before = self.app.store.db_path.read_bytes()
        with patch("work_rag.storage.fsync_dir", side_effect=AssertionError("read fsync")), \
                patch.object(Store, "transaction", side_effect=AssertionError("read migration write")):
            restarted = Application(self.app.store.data_dir)
            self.assertEqual(restarted.store.binding(), self.binding)
            self.assertTrue(self.call("task.list", app=restarted)["ok"])
        self.assertEqual(self.app.store.db_path.read_bytes(), before)

    def test_schema_two_migration_assigns_stable_binding_and_preserves_data(self):
        directory = self.root / "schema2"
        directory.mkdir()
        with closing(sqlite3.connect(directory / "core.sqlite3")) as connection, connection:
            for statement in [*MIGRATIONS, *MIGRATION_2]:
                connection.execute(statement)
            connection.execute("PRAGMA user_version=2")
            connection.execute("INSERT INTO requests VALUES ('probe','r','hash','{}')")
        first = Store(directory)
        self.assertEqual(Store(directory).binding(), first.binding())
        with closing(first.connect()) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(connection.execute("SELECT fingerprint FROM requests").fetchone()[0], "hash")

    def test_supported_backup_restore_regenerates_before_publication_and_rejects_old_binding(self):
        created = self.create()
        backup, restored = self.root / "backup", self.root / "restored"
        copy_library(self.app.store, backup)
        copy_library(Store(backup), restored)
        app = Application(restored)
        binding = app.store.binding()
        self.assertEqual(self.app.store.binding(), self.binding)
        self.assertEqual(binding["library_id"], self.binding["library_id"])
        self.assertNotEqual(binding["restore_generation"], self.binding["restore_generation"])
        self.assertNotEqual(binding["restore_generation"], Store(backup).binding()["restore_generation"])
        request = {"title": "事项", "content": "正文", "request_id": "create"}
        stale = self.call("task.create", request, app=app)
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["error"]["details"]["reason"], "binding_mismatch")
        self.assertEqual(self.data("task.create", request, app=app, binding=binding), created)
        self.assertEqual(len(app.tasks.list()), 1)

    def test_binding_is_rechecked_inside_business_write_transaction(self):
        original = self.app.tasks.create

        def switch_before_transaction(**params):
            with closing(self.app.store.connect()) as connection:
                connection.execute("UPDATE library_binding SET restore_generation='generation_switched'")
            return original(**params)

        with patch.object(self.app.tasks, "create", side_effect=switch_before_transaction):
            result = self.call("task.create", {"title": "new", "content": "body", "request_id": "r"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["details"]["reason"], "binding_mismatch")
        self.assertEqual(self.app.tasks.list(), [])

    def test_restore_publication_and_bridge_initialization_share_process_lock(self):
        self.create()
        destination = self.root / "published"
        process = None
        try:
            with library_lock(destination):
                process = subprocess.Popen(self.launch_command(destination), cwd=self.root,
                                           env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE)
                process.stdin.write(canonical_json(self.request("task.list")).encode())
                process.stdin.close()
                process.stdin = None
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.wait(timeout=0.15)
                self.assertFalse(destination.exists())
                copy_library(self.app.store, destination)
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stderr)
            response = json.loads(stdout)
            self.assertTrue(response["ok"], response)
            self.assertEqual(len(response["result"]["data"]), 1)
            self.assertEqual(response["binding"], Store(destination).binding())
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()

    def test_preinitialized_bridge_write_waits_for_publication_lock_and_rechecks_binding(self):
        script = (
            "import json,sys; from work_rag.application import Application; "
            "from work_rag.local_bridge import dispatch; "
            "app=Application(sys.argv[1]); print('ready', file=sys.stderr, flush=True); "
            "request=json.loads(sys.stdin.read()); print(json.dumps(dispatch(app,request)))"
        )
        process = subprocess.Popen([sys.executable, "-c", script, str(self.app.store.data_dir)],
                                   cwd=ROOT, env=self.env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertEqual(process.stderr.readline().rstrip(), b"ready")
            with library_lock(self.app.store.data_dir):
                request = self.request("task.create", {"title": "new", "content": "body", "request_id": "r"})
                process.stdin.write(canonical_json(request).encode())
                process.stdin.close()
                process.stdin = None
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.wait(timeout=0.15)
                self.app.store.renew_restore_generation()
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stderr)
            response = json.loads(stdout)
            self.assertFalse(response["ok"])
            self.assertEqual(response["error"]["details"]["reason"], "binding_mismatch")
            self.assertEqual(self.app.tasks.list(), [])
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_failed_copy_publication_does_not_change_source_or_leave_empty_target(self):
        destination = self.root / "failed-copy"
        publication_error = OSError("publication failed")
        if os.name == "nt":
            fail_publish = patch("work_rag.backup.publish_directory", side_effect=publication_error)
        else:
            fail_publish = patch.object(Path, "rename", side_effect=publication_error)
        with fail_publish:
            with self.assertRaisesRegex(OSError, "publication failed"):
                copy_library(self.app.store, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(self.app.store.binding(), self.binding)
        self.assertEqual(self.app.store.recovery_report()["sqlite_integrity"], "ok")

    def test_oversized_existing_task_still_confirms_write_and_replay(self):
        task = self.app.tasks.create("large", "x" * (MAX_STDOUT_BYTES + 1))
        params = {"task_id": task.task_id, "expected_revision": 1,
                  "changes": {"status": "completed"}, "request_id": "large_update"}
        first = self.call("task.update", params)
        self.assertTrue(first["ok"], first)
        self.assertLess(len(canonical_json(first).encode()), 1024)
        self.assertEqual(self.call("task.update", params), first)
        read = self.call("task.get", {"task_id": task.task_id})
        self.assertFalse(read["ok"])
        self.assertEqual(read["error"]["code"], "response_too_large")
        request = canonical_json(self.request("task.update", params)).encode()
        result = self.process(request)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), first)
        oversized = self.process(canonical_json(self.request("task.get", {"task_id": task.task_id})).encode())
        self.assertEqual(json.loads(oversized.stdout)["error"]["code"], "response_too_large")
        self.assertLessEqual(len(oversized.stdout), MAX_STDOUT_BYTES)

    def test_strict_validation_for_fields_types_and_required_binding(self):
        invalid = [
            self.request("task.list", {"limit": True}), self.request("task.list", {"limit": 201}),
            self.request("task.list", {"limit": 0}), self.request("task.list", {"offset": -1}),
            self.request("task.list", {"offset": 2**63}), self.request("task.list", {"unknown": 1}),
            self.request("task.list", {"query": []}), self.request("task.list", params=[]),
            self.request("task.create", {"title": "x", "content": "x", "request_id": ""}),
            self.request("task.create", {"title": "x", "content": "x", "request_id": "x", "tags": "tag"}),
            self.request("task.create", {"title": "x", "content": "x", "request_id": "x", "tags": [1]}),
            self.request("task.create", {"title": "x", "content": "x", "request_id": "x"}, binding={}),
            self.request("task.list", protocol_version=1), self.request("unknown"),
            self.request("task.list", call_id=[]), self.request("task.list", extra=True),
        ]
        missing_binding = self.request("task.create", {"title": "x", "content": "x", "request_id": "x"})
        del missing_binding["binding"]
        invalid.append(missing_binding)
        for request in invalid:
            with self.subTest(request=request):
                result = dispatch(self.app, request)
                self.assertFalse(result["ok"], result)
                self.assertIn(result["error"]["layer"], {"validation", "protocol"})
        self.assertEqual(self.app.tasks.list(), [])

    def test_real_launcher_short_process_error_exit_zero_and_one_json_line(self):
        request = self.request("task.create", {"title": "中文\n标题", "content": "body", "request_id": "wire"})
        result = self.process(canonical_json(request).encode())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        response = json.loads(result.stdout)
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["call_id"], "call_test")
        self.assertEqual(self.app.tasks.get(response["result"]["data"]["task_id"]).title, "中文\n标题")
        invalid = self.process(canonical_json(self.request("task.list", {"limit": False})).encode())
        self.assertEqual(invalid.returncode, 0)
        self.assertEqual(json.loads(invalid.stdout)["call_id"], "call_test")
        self.assertFalse(json.loads(invalid.stdout)["ok"])

    def test_bad_wire_input_is_bounded_and_does_not_initialize_library(self):
        directory = self.root / "must-not-initialize"
        raw_inputs = [b"", b"{} {}", b"[]", b"\xff", b'{"x":NaN}', b'{"x":1,"x":2}',
                      b"[" * 2000, b"x" * (MAX_STDIN_BYTES + 1),
                      b'{"protocol_version":"1","call_id":"\\ud800","operation":"task.list","params":{}}']
        for raw in raw_inputs:
            with self.subTest(raw_prefix=raw[:50]):
                result = self.process(raw, directory=directory)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertLess(len(result.stdout), 1024)
                self.assertFalse(json.loads(result.stdout)["ok"])
                self.assertFalse(directory.exists())

    def test_default_list_page_size_and_maximum(self):
        for index in range(52):
            self.app.tasks.create(str(index), "body")
        self.assertEqual(len(self.data("task.list")), 50)
        self.assertEqual(len(self.data("task.list", {"offset": 50})), 2)
        self.assertEqual(len(self.data("task.list", {"limit": 200})), 52)


@unittest.skipUnless(os.name == "posix", "POSIX shell launcher")
class LocalBridgeLauncherTests(unittest.TestCase):
    def test_runtime_fallback_spaces_environment_and_invalid_paths(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project with spaces"
            (project / "tools").mkdir(parents=True)
            launcher = project / "tools" / "run_local_bridge.sh"
            shutil.copyfile(LAUNCHER, launcher)
            env = {key: value for key, value in os.environ.items() if key not in {"WORK_RAG_PYTHON", "WORK_RAG_DATA_DIR"}}

            def run(*args):
                return subprocess.run(["/bin/sh", str(launcher), *args], cwd=root, env=env,
                                      capture_output=True, text=True, timeout=5)

            missing = run()
            self.assertEqual(missing.returncode, 1)
            self.assertEqual(missing.stdout, "")
            self.assertIn("Python runtime unavailable", missing.stderr)
            python = project / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" "$@"\n', encoding="utf-8")
            python.chmod(0o755)
            self.assertEqual(run().stdout.splitlines(), [str(project), "-m", "work_rag.local_bridge",
                                                        "--data-dir", str(project / ".work-rag-data")])
            env["WORK_RAG_PYTHON"] = str(python)
            env["WORK_RAG_DATA_DIR"] = str(root / "environment library")
            self.assertEqual(run().stdout.splitlines()[-1], env["WORK_RAG_DATA_DIR"])
            self.assertEqual(run(str(root / "explicit library")).stdout.splitlines()[-1], str(root / "explicit library"))
            for args in [("relative",), (str(root), "extra")]:
                self.assertEqual(run(*args).returncode, 2)
            env["WORK_RAG_PYTHON"] = "python"
            self.assertEqual(run().returncode, 2)
            self.assertEqual(run().stdout, "")
