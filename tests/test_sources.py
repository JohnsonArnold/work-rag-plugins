"""Independent K01 behavior tests; all input bytes live in temporary directories."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from work_rag.contracts import CoreError, ErrorCode, new_id
from work_rag.sources import SourceService
from work_rag.storage import Store


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "data")
        self.service = SourceService(self.store)

    def input(self, name="input.txt", content=b"first"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(CoreError) as caught:
            function(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)
        return caught.exception

    def test_same_origin_same_bytes_has_no_new_version(self):
        path = self.input()
        first = self.service.import_file(path)
        again = self.service.import_file(path)
        self.assertTrue(first.changed)
        self.assertFalse(again.changed)
        self.assertEqual(first.source, again.source)
        self.assertEqual(first.version, again.version)
        self.assertEqual([first.version], self.service.versions(first.source.source_id))
        self.assertEqual([], self.store.recovery_report()["orphan_blobs"])

    def test_same_bytes_different_paths_are_distinct_sources(self):
        first = self.service.import_file(self.input("one.txt"))
        second = self.service.import_file(self.input("two.txt"))
        self.assertNotEqual(first.source.source_id, second.source.source_id)
        self.assertNotEqual(first.version.version_id, second.version.version_id)
        self.assertEqual(first.version.sha256, second.version.sha256)
        self.assertEqual(2, len(self.service.list()))
        self.assertEqual(1, len(list(self.store.blobs.iterdir())))

    def test_same_filename_different_directories_are_not_merged(self):
        first = self.service.import_file(self.input("left/report.txt", b"one"))
        second = self.service.import_file(self.input("right/report.txt", b"two"))
        self.assertNotEqual(first.source.source_id, second.source.source_id)
        self.assertEqual("report.txt", first.source.title)
        self.assertEqual("report.txt", second.source.title)

    def test_explicit_identity_rename_preserves_both_origin_mappings(self):
        original = self.input("old.txt")
        first = self.service.import_file(original)
        renamed = self.input("new.txt")
        same = self.service.import_file(renamed, source_id=first.source.source_id)
        self.assertFalse(same.changed)
        self.assertEqual(first.version, same.version)
        for path, value in ((renamed, b"second"), (original, b"third")):
            path.write_bytes(value)
            result = self.service.import_file(path)
            self.assertEqual(first.source.source_id, result.source.source_id)
        self.assertEqual([1, 2, 3], [v.sequence for v in self.service.versions(first.source.source_id)])
        with closing(self.store.connect()) as connection:
            mappings = connection.execute("SELECT origin_key,source_id FROM origin_mappings").fetchall()
        self.assertEqual({original.as_uri(), renamed.as_uri()}, {row["origin_key"] for row in mappings})
        self.assertEqual({first.source.source_id}, {row["source_id"] for row in mappings})

    def test_origin_conflict_does_not_reassign_or_update_either_source(self):
        one = self.service.import_file(self.input("one.txt"), origin_key="desk:one")
        two = self.service.import_file(self.input("two.txt", b"two"), origin_key="desk:two")
        self.assert_code(ErrorCode.CONFLICT, self.service.import_file,
                         self.input("third.txt", b"third"), source_id=two.source.source_id,
                         origin_key="desk:one")
        self.assertEqual(one.source, self.service.get(one.source.source_id))
        self.assertEqual(two.source, self.service.get(two.source.source_id))
        self.assertEqual(1, len(self.service.versions(two.source.source_id)))

    def test_explicit_origin_can_track_file_copies(self):
        first = self.service.import_file(self.input("copy1.txt"), origin_key="office:report")
        second = self.service.import_file(self.input("copy2.txt", b"updated"), origin_key="office:report")
        self.assertEqual(first.source.source_id, second.source.source_id)
        self.assertEqual(2, second.version.sequence)

    def test_old_snapshots_remain_readable_after_input_change_and_restart(self):
        path = self.input()
        first = self.service.import_file(path)
        path.write_bytes(b"updated")
        second = self.service.import_file(path)
        self.assertEqual(2, second.version.sequence)
        self.assertEqual(first.source.source_id, second.source.source_id)
        restarted = SourceService(Store(self.store.data_dir))
        self.assertEqual(b"first", restarted.snapshot_path(first.source.source_id, first.version.version_id).read_bytes())
        self.assertEqual(b"updated", restarted.snapshot_path(first.source.source_id).read_bytes())
        self.assertEqual(second.source, restarted.get(first.source.source_id))
        self.assertEqual([first.version, second.version], restarted.versions(first.source.source_id))

    def test_reverting_bytes_creates_new_sequence_but_reuses_blob(self):
        path = self.input()
        first = self.service.import_file(path)
        path.write_bytes(b"second")
        self.service.import_file(path)
        path.write_bytes(b"first")
        reverted = self.service.import_file(path)
        self.assertTrue(reverted.changed)
        self.assertEqual(3, reverted.version.sequence)
        self.assertNotEqual(first.version.version_id, reverted.version.version_id)
        self.assertEqual(first.version.sha256, reverted.version.sha256)

    def test_version_must_belong_to_requested_source(self):
        one = self.service.import_file(self.input("one.txt"))
        two = self.service.import_file(self.input("two.txt"))
        self.assert_code(ErrorCode.VERSION_NOT_FOUND, self.service.version,
                         one.source.source_id, two.version.version_id)
        self.assert_code(ErrorCode.VERSION_NOT_FOUND, self.service.snapshot_path,
                         one.source.source_id, two.version.version_id)
        self.assert_code(ErrorCode.VERSION_NOT_FOUND, self.service.version,
                         one.source.source_id, new_id("ver"))
        self.assert_code(ErrorCode.NOT_FOUND, self.service.get, new_id("src"))

    def test_create_request_id_replays_original_result_even_after_update_and_restart(self):
        path = self.input()
        first = self.service.import_file(path, request_id="create-1", title=" Report ")
        path.write_bytes(b"second")
        updated = self.service.import_file(path)
        path.write_bytes(b"first")
        restarted = SourceService(Store(self.store.data_dir))
        replay = restarted.import_file(path, request_id="create-1", title=" Report ")
        self.assertEqual(first, replay)
        self.assertEqual("Report", first.source.title)
        self.assertEqual(updated.source, restarted.get(first.source.source_id))
        self.assertEqual(2, len(restarted.versions(first.source.source_id)))

    def test_create_request_id_payload_conflicts(self):
        path = self.input()
        first = self.service.import_file(path, request_id="create-1")
        for changed in ({"title": "different"}, {"origin_key": "different"},
                        {"source_id": first.source.source_id}):
            with self.subTest(changed=changed):
                self.assert_code(ErrorCode.CONFLICT, self.service.import_file, path,
                                 request_id="create-1", **changed)
        path.write_bytes(b"different bytes")
        self.assert_code(ErrorCode.CONFLICT, self.service.import_file, path, request_id="create-1")
        self.assertEqual([first.version], self.service.versions(first.source.source_id))

    def test_expected_current_version_conflict_even_for_unchanged_bytes(self):
        path = self.input()
        first = self.service.import_file(path)
        path.write_bytes(b"second")
        updated = self.service.import_file(path, source_id=first.source.source_id,
                                           expected_current_version=first.version.version_id)
        error = self.assert_code(ErrorCode.CONFLICT, self.service.import_file, path,
                                 source_id=first.source.source_id,
                                 expected_current_version=first.version.version_id)
        self.assertEqual(updated.version.version_id, error.details["current_version_id"])
        self.assertEqual(2, len(self.service.versions(first.source.source_id)))
        self.assert_code(ErrorCode.INVALID_ARGUMENT, self.service.import_file, path,
                         expected_current_version=updated.version.version_id)

    def test_unknown_explicit_source_is_not_created(self):
        self.assert_code(ErrorCode.NOT_FOUND, self.service.import_file, self.input(), source_id=new_id("src"))
        self.assertEqual([], self.service.list())

    def test_invalid_arguments_are_explicit_errors(self):
        path = self.input()
        for arguments in ({"source_id": "wrong"}, {"origin_key": " "},
                          {"title": " "}, {"request_id": ""}, {"request_id": "a" * 201}):
            with self.subTest(arguments=arguments):
                self.assert_code(ErrorCode.INVALID_ARGUMENT, self.service.import_file, path, **arguments)
        self.assert_code(ErrorCode.INVALID_ARGUMENT, self.service.import_file, self.root)
        self.assertEqual([], self.service.list())
        self.assertEqual([], list(self.store.blobs.iterdir()))

    def test_db_failure_after_blob_publication_leaves_identifiable_orphan(self):
        path = self.input()
        with patch.object(self.service, "_insert_version", side_effect=sqlite3.OperationalError("injected DB write failure")):
            with self.assertRaisesRegex(sqlite3.OperationalError, "injected DB write failure"):
                self.service.import_file(path, request_id="failed-create")
        self.assertEqual([], self.service.list())
        report = self.store.recovery_report()
        self.assertEqual(1, len(report["orphan_blobs"]))
        self.assertEqual([], report["staging_files"])
        self.assertEqual([], report["corrupt_or_missing"])
        self.assertEqual(b"first", self.store.verify_blob(report["orphan_blobs"][0]).read_bytes())
        with closing(self.store.connect()) as connection:
            self.assertEqual(0, connection.execute("SELECT count(*) FROM requests").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT count(*) FROM origin_mappings").fetchone()[0])
        successful_retry = self.service.import_file(path, request_id="failed-create")
        self.assertEqual(1, successful_retry.version.sequence)
        self.assertEqual([], self.store.recovery_report()["orphan_blobs"])

    def test_failure_after_current_update_before_commit_does_not_publish(self):
        first = self.service.import_file(self.input())
        path = self.input("new-origin.txt", b"second")
        original_transaction = self.store.transaction

        @contextmanager
        def fail_before_commit():
            with original_transaction() as connection:
                yield connection
                raise RuntimeError("injected before commit")

        with patch.object(self.store, "transaction", fail_before_commit):
            with self.assertRaisesRegex(RuntimeError, "injected before commit"):
                self.service.import_file(path, source_id=first.source.source_id, request_id="uncommitted")
        restarted = SourceService(Store(self.store.data_dir))
        self.assertEqual(first.source, restarted.get(first.source.source_id))
        self.assertEqual([first.version], restarted.versions(first.source.source_id))
        self.assertEqual(b"first", restarted.snapshot_path(first.source.source_id).read_bytes())
        with closing(self.store.connect()) as connection:
            self.assertEqual(0, connection.execute("SELECT count(*) FROM requests WHERE request_id='uncommitted'").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT count(*) FROM origin_mappings WHERE origin_key=?", (path.as_uri(),)).fetchone()[0])
        self.assertEqual(1, len(self.store.recovery_report()["orphan_blobs"]))

    def concurrent_imports(self, paths, **kwargs):
        stores = [Store(self.store.data_dir), Store(self.store.data_dir)]
        barrier = threading.Barrier(2, timeout=10)

        def worker(index):
            store = stores[index]
            original_stage = store.stage_file

            def staged(path):
                result = original_stage(path)
                barrier.wait()
                return result

            with patch.object(store, "stage_file", staged):
                try:
                    return SourceService(store).import_file(paths[index], **kwargs)
                except CoreError as error:
                    return error

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(worker, index) for index in range(2)]
            return [future.result(timeout=20) for future in futures]

    def test_concurrent_same_origin_same_content_two_stores(self):
        path = self.input()
        results = self.concurrent_imports([path, path])
        self.assertFalse(any(isinstance(result, CoreError) for result in results))
        self.assertEqual([False, True], sorted(result.changed for result in results))
        self.assertEqual(results[0].source.source_id, results[1].source.source_id)
        self.assertEqual(1, len(self.service.list()))
        self.assertEqual(1, len(self.service.versions(results[0].source.source_id)))
        self.assertEqual([], self.store.recovery_report()["orphan_blobs"])

    def test_concurrent_create_same_request_id_is_idempotent(self):
        path = self.input()
        results = self.concurrent_imports([path, path], request_id="concurrent-create")
        self.assertEqual(results[0], results[1])
        self.assertEqual(1, len(self.service.list()))

    def test_concurrent_updates_without_expected_version_are_serialized(self):
        first = self.service.import_file(self.input())
        paths = [self.input("left.txt", b"left"), self.input("right.txt", b"right")]
        results = self.concurrent_imports(paths, source_id=first.source.source_id)
        self.assertFalse(any(isinstance(result, CoreError) for result in results))
        self.assertEqual([2, 3], sorted(result.version.sequence for result in results))
        self.assertEqual([1, 2, 3], [v.sequence for v in self.service.versions(first.source.source_id)])
        self.assertIn(self.service.snapshot_path(first.source.source_id).read_bytes(), (b"left", b"right"))

    def test_concurrent_expected_version_only_one_update_commits(self):
        first = self.service.import_file(self.input())
        results = self.concurrent_imports(
            [self.input("left.txt", b"left"), self.input("right.txt", b"right")],
            source_id=first.source.source_id, expected_current_version=first.version.version_id)
        errors = [result for result in results if isinstance(result, CoreError)]
        self.assertEqual(1, len(errors))
        self.assertEqual(ErrorCode.CONFLICT, errors[0].code)
        self.assertEqual(2, len(self.service.versions(first.source.source_id)))
        self.assertEqual(1, len(self.store.recovery_report()["orphan_blobs"]))


if __name__ == "__main__":
    unittest.main()
