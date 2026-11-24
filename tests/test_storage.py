"""Independent F03 tests for actual SQLite transactions and immutable blobs."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from work_rag.contracts import CoreError, ErrorCode, new_id
from work_rag.sources import SourceService
from work_rag.storage import SCHEMA_VERSION, Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "data")

    def input(self, content=b"snapshot"):
        path = self.root / "input.bin"
        path.write_bytes(content)
        return path

    def assert_integrity(self, function, *args):
        with self.assertRaises(CoreError) as caught:
            function(*args)
        self.assertEqual(ErrorCode.INTEGRITY, caught.exception.code)
        return caught.exception

    def test_initialization_is_repeatable_and_configurable(self):
        second = Store(self.store.data_dir)
        self.assertEqual(self.store.db_path, second.db_path)
        with closing(second.connect()) as connection:
            self.assertEqual(SCHEMA_VERSION, connection.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(1, connection.execute("PRAGMA foreign_keys").fetchone()[0])
            self.assertEqual(2, connection.execute("PRAGMA synchronous").fetchone()[0])
        self.assertEqual("ok", second.recovery_report()["sqlite_integrity"])

    def test_transaction_commits_persistent_data(self):
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO requests VALUES (?,?,?,?)", ("probe", "one", "hash", "{}"))
        with closing(Store(self.store.data_dir).connect()) as connection:
            self.assertEqual("hash", connection.execute("SELECT fingerprint FROM requests").fetchone()[0])

    def test_transaction_rolls_back_exception_and_baseexception(self):
        for exception in (RuntimeError("rollback"), KeyboardInterrupt("interrupted")):
            with self.subTest(exception=type(exception).__name__):
                with self.assertRaises(type(exception)):
                    with self.store.transaction() as connection:
                        connection.execute("INSERT INTO requests VALUES (?,?,?,?)", ("probe", "one", "hash", "{}"))
                        raise exception
                with closing(self.store.connect()) as connection:
                    self.assertEqual(0, connection.execute("SELECT count(*) FROM requests").fetchone()[0])

    def test_deferred_foreign_key_commit_failure_rolls_back(self):
        source_id = new_id("src")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.transaction() as connection:
                connection.execute("INSERT INTO sources (source_id,title,current_version_id,created_at) VALUES (?,?,?,?)", (source_id, "bad pointer", new_id("ver"), "now"))
                connection.execute("INSERT INTO requests VALUES (?,?,?,?)", ("probe", "one", "hash", "{}"))
        with closing(self.store.connect()) as connection:
            self.assertEqual(0, connection.execute("SELECT count(*) FROM sources").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT count(*) FROM requests").fetchone()[0])
        self.assertEqual([], self.store.recovery_report()["foreign_key_errors"])

    def test_stage_file_returns_checksum_size_and_immutable_complete_blob(self):
        content = b"\x00snapshot\xff" * 180000
        checksum, size = self.store.stage_file(self.input(content))
        self.assertEqual(hashlib.sha256(content).hexdigest(), checksum)
        self.assertEqual(len(content), size)
        self.assertEqual(content, self.store.verify_blob(checksum, size).read_bytes())
        self.assertEqual(0, self.store.blob_path(checksum).stat().st_mode & 0o222)
        self.assertEqual([], list(self.store.staging.iterdir()))
        self.assertEqual((checksum, size), self.store.stage_file(self.root / "input.bin"))
        self.assertEqual(1, len(list(self.store.blobs.iterdir())))

    def test_empty_file_snapshot(self):
        checksum, size = self.store.stage_file(self.input(b""))
        self.assertEqual(hashlib.sha256(b"").hexdigest(), checksum)
        self.assertEqual(0, size)
        self.assertEqual(b"", self.store.verify_blob(checksum, size).read_bytes())

    def test_blob_path_rejects_invalid_or_traversal_checksum(self):
        for checksum in (None, "", "a" * 63, "A" * 64, "../outside", "f" * 64 + "/x"):
            with self.subTest(checksum=checksum), self.assertRaises(CoreError) as caught:
                self.store.blob_path(checksum)
            self.assertEqual(ErrorCode.INVALID_ARGUMENT, caught.exception.code)

    def test_unreferenced_blob_is_reported_and_not_deleted_on_restart(self):
        checksum, size = self.store.stage_file(self.input())
        restarted = Store(self.store.data_dir)
        report = restarted.recovery_report()
        self.assertEqual([checksum], report["orphan_blobs"])
        self.assertEqual([], report["staging_files"])
        self.assertEqual([], report["corrupt_or_missing"])
        self.assertTrue(restarted.verify_blob(checksum, size).exists())
        self.assertIn("no automatic deletion", report["cleanup"])

    def test_interrupted_staging_is_report_only_even_after_restart(self):
        leftover = self.store.staging / "import-interrupted"
        leftover.write_bytes(b"partial snapshot")
        restarted = Store(self.store.data_dir)
        for unused in range(2):
            report = restarted.recovery_report()
            self.assertEqual([leftover.name], report["staging_files"])
            self.assertEqual(b"partial snapshot", leftover.read_bytes())
        self.assertEqual([], report["orphan_blobs"])

    def test_missing_blob_is_integrity_error_and_reported(self):
        service = SourceService(self.store)
        result = service.import_file(self.input())
        target = self.store.blob_path(result.version.sha256)
        self.assertEqual(self.store.blobs.resolve(), target.resolve().parent)
        target.chmod(0o644)
        target.unlink()
        error = self.assert_integrity(service.snapshot_path, result.source.source_id)
        self.assertEqual(result.version.sha256, error.details["sha256"])
        report = Store(self.store.data_dir).recovery_report()
        self.assertEqual(1, len(report["corrupt_or_missing"]))
        self.assertEqual(ErrorCode.INTEGRITY.value, report["corrupt_or_missing"][0]["code"])

    def test_corrupt_blob_is_integrity_error_and_never_silently_replaced(self):
        service = SourceService(self.store)
        path = self.input()
        result = service.import_file(path)
        target = self.store.blob_path(result.version.sha256)
        target.chmod(0o644)
        target.write_bytes(b"corrupt!")
        self.assert_integrity(service.snapshot_path, result.source.source_id)
        self.assert_integrity(self.store.stage_file, path)
        self.assertEqual(b"corrupt!", target.read_bytes())
        self.assertEqual([], list(self.store.staging.iterdir()))
        self.assertEqual(1, len(self.store.recovery_report()["corrupt_or_missing"]))

    def test_blob_metadata_size_mismatch_is_integrity_error(self):
        checksum, size = self.store.stage_file(self.input())
        self.assert_integrity(self.store.verify_blob, checksum, size + 1)

    def test_source_change_detected_and_no_blob_published(self):
        path = self.input()
        original_signature = self.store._signature
        calls = 0

        def changed(info):
            nonlocal calls
            calls += 1
            signature = original_signature(info)
            return signature if calls == 1 else signature + ("changed",)

        with patch.object(self.store, "_signature", changed):
            with self.assertRaises(CoreError) as caught:
                self.store.stage_file(path)
        self.assertEqual(ErrorCode.SOURCE_CHANGED, caught.exception.code)
        self.assertEqual([], list(self.store.blobs.iterdir()))
        self.assertEqual([], list(self.store.staging.iterdir()))

    def test_blob_publication_failure_cleans_staging(self):
        def fail_after_making_readonly(staging, destination):
            staging.chmod(0o444)
            raise OSError("injected publication failure")

        with patch("work_rag.storage.publish_blob", side_effect=fail_after_making_readonly):
            with self.assertRaisesRegex(OSError, "injected publication failure"):
                self.store.stage_file(self.input())
        self.assertEqual([], list(self.store.blobs.iterdir()))
        self.assertEqual([], list(self.store.staging.iterdir()))

    def test_newer_schema_is_rejected_without_rewriting_version(self):
        with self.store.transaction() as connection:
            connection.execute("PRAGMA user_version=999")
        with self.assertRaises(CoreError) as caught:
            Store(self.store.data_dir)
        self.assertEqual(ErrorCode.UNSUPPORTED, caught.exception.code)
        self.assertEqual(999, caught.exception.details["version"])
        with closing(self.store.connect()) as connection:
            self.assertEqual(999, connection.execute("PRAGMA user_version").fetchone()[0])

    def test_unknown_negative_schema_is_rejected(self):
        with self.store.transaction() as connection:
            connection.execute("PRAGMA user_version=-1")
        with self.assertRaises(CoreError) as caught:
            Store(self.store.data_dir)
        self.assertEqual(ErrorCode.UNSUPPORTED, caught.exception.code)


if __name__ == "__main__":
    unittest.main()
