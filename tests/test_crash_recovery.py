"""Abrupt child-process exits exercise recovery without Python finally handlers."""
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from work_rag.sources import SourceService
from work_rag.storage import Store


class CrashRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data_dir = self.root / "data"
        self.path = self.root / "input.txt"
        self.path.write_bytes(b"old")
        self.store = Store(self.data_dir)
        self.first = SourceService(self.store).import_file(self.path)
        self.path.write_bytes(b"new")

    def crash(self, hook):
        program = '''
from contextlib import contextmanager
import os
import sys
from work_rag.storage import Store
from work_rag.sources import SourceService
store = Store(sys.argv[1])
''' + hook + '''
SourceService(store).import_file(sys.argv[2], request_id="interrupted-import")
raise RuntimeError("Crash hook was not reached")
'''
        result = subprocess.run([sys.executable, "-c", program, str(self.data_dir), str(self.path)],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 73, result.stdout + result.stderr)

    def assert_old_current_after_restart(self):
        restarted = Store(self.data_dir)
        sources = SourceService(restarted)
        self.assertEqual(sources.get(self.first.source.source_id), self.first.source)
        self.assertEqual(sources.versions(self.first.source.source_id), [self.first.version])
        self.assertEqual(sources.snapshot_path(self.first.source.source_id).read_bytes(), b"old")
        report = restarted.recovery_report()
        self.assertEqual(report["sqlite_integrity"], "ok")
        self.assertEqual(report["foreign_key_errors"], [])
        self.assertEqual(report["corrupt_or_missing"], [])
        return restarted, sources, report

    def test_exit_before_blob_publication_leaves_only_reported_staging(self):
        self.crash('''
def exit_before_publication(*args, **kwargs):
    os._exit(73)
import work_rag.storage
work_rag.storage.publish_blob = exit_before_publication
''')
        restarted, sources, report = self.assert_old_current_after_restart()
        self.assertEqual(report["orphan_blobs"], [])
        self.assertEqual(len(report["staging_files"]), 1)
        leftover = restarted.staging / report["staging_files"][0]
        self.assertEqual(leftover.read_bytes(), b"new")
        self.assertEqual(sources.import_file(self.path).version.sequence, 2)
        self.assertTrue(leftover.exists(), "Recovery must not silently delete staging")

    def test_exit_after_pointer_update_before_commit_rolls_back_database(self):
        self.crash('''
original = store.transaction
@contextmanager
def interrupted_transaction():
    with original() as connection:
        yield connection
        os._exit(73)
store.transaction = interrupted_transaction
''')
        restarted, sources, report = self.assert_old_current_after_restart()
        self.assertEqual(report["staging_files"], [])
        self.assertEqual(report["orphan_blobs"], [hashlib.sha256(b"new").hexdigest()])
        with restarted.transaction() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM requests").fetchone()[0], 0)
        retry = sources.import_file(self.path, request_id="interrupted-import")
        self.assertEqual(retry.version.sequence, 2)
        self.assertEqual(sources.snapshot_path(retry.source.source_id).read_bytes(), b"new")
        self.assertEqual(restarted.recovery_report()["orphan_blobs"], [])
