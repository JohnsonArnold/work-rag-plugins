"""T01 behavior tests against real temporary SQLite stores; standard library only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from work_rag.contracts import BEIJING, CoreError, ErrorCode, Task, new_id
from work_rag.storage import Store
from work_rag.tasks import TaskService


class TaskServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.directory.name)
        self.service = TaskService(self.store)
        self.now = datetime(2026, 11, 20, 12, tzinfo=BEIJING)

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(CoreError) as raised:
            function(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def ids(self, **filters):
        return {task.task_id for task in self.service.list(**filters)}

    def count(self, table):
        self.assertIn(table, {"tasks", "requests"})
        with closing(self.store.connect()) as connection:
            return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def test_create_normalizes_and_survives_new_store_and_process(self):
        task = self.service.create("  检查网络  ", "  确认交换机日志\n  ", tags=[" IT ", "行政", "IT"])
        self.assertIsInstance(task, Task)
        self.assertRegex(task.task_id, r"^task_[0-9a-f]{32}$")
        self.assertEqual((task.title, task.content, task.tags), ("检查网络", "确认交换机日志", ["IT", "行政"]))
        self.assertEqual((task.status, task.revision), ("pending", 1))
        self.assertEqual(task.created_at, task.updated_at)
        self.assertEqual(datetime.fromisoformat(task.created_at).utcoffset(), timedelta(hours=8))
        self.assertIsNone(task.due)
        self.assertIsNone(task.due_kind)
        restarted = TaskService(Store(self.directory.name))
        self.assertEqual(restarted.get(task.task_id), task)
        result = subprocess.run(
            [sys.executable, "-c", (
                "import json,sys; from work_rag.storage import Store; "
                "from work_rag.tasks import TaskService; from work_rag.contracts import jsonable; "
                "print(json.dumps(jsonable(TaskService(Store(sys.argv[1])).get(sys.argv[2]))))"
            ), self.directory.name, task.task_id],
            check=True, capture_output=True, text=True,
        )
        self.assertEqual(Task(**json.loads(result.stdout)), task)

    def test_blank_or_non_string_title_and_content_are_rejected(self):
        for field in ("title", "content"):
            for value in ("", "  \n\t ", None, 12, [], True):
                with self.subTest(field=field, value=value):
                    values = {"title": "title", "content": "content", field: value}
                    self.assert_code(ErrorCode.INVALID_ARGUMENT, self.service.create, **values)
        self.assertEqual(self.count("tasks"), 0)

    def test_tags_reject_bad_collections_and_entries(self):
        for tags in ("IT", {"IT"}, {"IT": 1}, 1, [""], [" \n"], [None], [True], [1]):
            with self.subTest(tags=tags):
                self.assert_code(ErrorCode.INVALID_ARGUMENT, self.service.create, "a", "b", tags=tags)
        self.assertEqual(self.service.create("a", "b", tags=None).tags, [])

    def test_due_accepts_date_and_normalizes_explicit_timezone(self):
        values = [
            ("2028-02-29", "2028-02-29", "date"),
            ("2026-11-19T17:30:00Z", "2026-11-20T01:30:00+08:00", "datetime"),
            ("2026-11-20T23:30:00-05:00", "2026-11-21T12:30:00+08:00", "datetime"),
            ("2026-11-20T08:00+08:00", "2026-11-20T08:00:00+08:00", "datetime"),
            ("2026-11-20T08:00:00.000001+0800", "2026-11-20T08:00:00.000001+08:00", "datetime"),
        ]
        for raw, normalized, kind in values:
            with self.subTest(raw=raw):
                task = self.service.create("a", "b", due=raw)
                self.assertEqual((task.due, task.due_kind), (normalized, kind))
                self.assertEqual(self.service.get(task.task_id), task)

    def test_due_rejects_invalid_dates_datetimes_and_missing_timezone(self):
        for due in (
            "", " ", "2026-2-1", "20261120", "2026-02-29", "2026-13-01", "2026-11-31",
            "2026-W47-5", "tomorrow", "2026-11-20Z", "2026-11-20T12:00:00",
            "2026-11-20T12:00:00 Asia/Shanghai", "2026-11-20T12:00:00+24:00",
            "2026-11-20T12:00:00+08:99", "2026-11-20T25:00:00+08:00",
            "2026-11-20T12:00:60Z", "2026-11-20T12:00:00+08:00 trailing",
            "2026-11-20Q12:00:00+08:00", " 2026-11-20", 123,
            date(2026, 11, 20), datetime(2026, 11, 20), self.now,
        ):
            with self.subTest(due=due):
                self.assert_code(ErrorCode.INVALID_ARGUMENT, self.service.create, "a", "b", due=due)
        self.assertEqual(self.count("tasks"), 0)

    def test_update_all_supported_fields_and_clear_due(self):
        task = self.service.create("a", "b", due="2026-11-20")
        updated = self.service.update(
            task.task_id, 1, title=" new ", content=" changed ", status="in_progress",
            due="2026-11-20T05:00:00Z", tags=["z", " a ", "a"],
        )
        self.assertEqual((updated.title, updated.content, updated.status), ("new", "changed", "in_progress"))
        self.assertEqual((updated.due, updated.due_kind), ("2026-11-20T13:00:00+08:00", "datetime"))
        self.assertEqual(updated.tags, ["a", "z"])
        self.assertEqual(updated.revision, 2)
        self.assertEqual(updated.created_at, task.created_at)
        self.assertGreaterEqual(updated.updated_at, task.updated_at)
        cleared = self.service.update(task.task_id, 2, due=None, tags=None)
        self.assertEqual((cleared.due, cleared.due_kind, cleared.tags, cleared.revision), (None, None, [], 3))
        self.assertEqual(self.service.get(task.task_id), cleared)

    def test_all_statuses_are_persisted_and_filterable(self):
        expected = {}
        for status in ("pending", "in_progress", "completed", "cancelled"):
            task = self.service.create(status, "body")
            if status != "pending":
                task = self.service.update(task.task_id, task.revision, status=status)
            expected[status] = task
        for status, task in expected.items():
            self.assertEqual(self.service.list(status=status), [task])
        self.assertEqual(self.ids(view="unfinished"), {expected[s].task_id for s in ("pending", "in_progress")})
        self.assertEqual(self.service.list(status="completed", view="unfinished"), [])

    def test_update_rejects_invalid_changes_without_modifying_task(self):
        task = self.service.create("a", "b")
        changes = [
            {}, {"title": " "}, {"content": None}, {"status": "done"}, {"status": None},
            {"status": []}, {"due": "today"}, {"tags": [""]}, {"revision": 100},
            {"deleted": True}, {"created_at": "now"},
        ]
        for values in changes:
            with self.subTest(changes=values):
                self.assert_code(ErrorCode.INVALID_ARGUMENT, self.service.update, task.task_id, 1, **values)
                self.assertEqual(self.service.get(task.task_id), task)

    def test_expected_revision_must_be_positive_integer(self):
        task = self.service.create("a", "b")
        for revision in (None, 0, -1, True, False, 1.0, "1"):
            with self.subTest(revision=revision):
                self.assert_code(
                    ErrorCode.INVALID_ARGUMENT, self.service.update, task.task_id, revision, title="new",
                )
        self.assertEqual(self.service.get(task.task_id), task)

    def test_stale_revision_is_conflict_and_preserves_winner(self):
        task = self.service.create("a", "b")
        winner = self.service.update(task.task_id, 1, status="completed")
        error = self.assert_code(ErrorCode.CONFLICT, self.service.update, task.task_id, 1, title="loser")
        self.assertEqual(error.details["actual_revision"], 2)
        self.assertEqual(self.service.get(task.task_id), winner)
        self.assert_code(ErrorCode.CONFLICT, self.service.update, task.task_id, 3, title="future")

    def test_missing_and_invalid_ids(self):
        missing = new_id("task")
        self.assert_code(ErrorCode.NOT_FOUND, self.service.get, missing)
        self.assert_code(ErrorCode.NOT_FOUND, self.service.update, missing, 1, title="new")
        for task_id in ("", "task_1", new_id("src"), None):
            with self.subTest(task_id=task_id):
                self.assert_code(ErrorCode.INVALID_ARGUMENT, self.service.get, task_id)
                self.assert_code(ErrorCode.INVALID_ARGUMENT, self.service.update, task_id, 1, title="new")

    def test_today_uses_beijing_date_and_includes_finished_records(self):
        today_date = self.service.create("date", "b", due="2026-11-20")
        today_utc = self.service.create("instant", "b", due="2026-11-19T16:00:00Z")
        completed = self.service.create("done", "b", due="2026-11-20T00:00:00+08:00")
        self.service.update(completed.task_id, 1, status="completed")
        cancelled = self.service.create("cancelled", "b", due="2026-11-20")
        self.service.update(cancelled.task_id, 1, status="cancelled")
        self.service.create("yesterday", "b", due="2026-11-19T15:59:59Z")
        self.service.create("tomorrow", "b", due="2026-11-20T16:00:00Z")
        self.service.create("no due", "b")
        # UTC is still yesterday; the view must use Beijing's local date.
        utc_now = datetime(2026, 11, 19, 16, 30, tzinfo=timezone.utc)
        self.assertEqual(
            self.ids(view="today", now=utc_now),
            {today_date.task_id, today_utc.task_id, completed.task_id, cancelled.task_id},
        )
        self.assertEqual(self.ids(status="completed", view="today", now=utc_now), {completed.task_id})

    def test_date_deadline_only_overdue_after_local_day_ends(self):
        task = self.service.create("deadline", "b", due="2026-11-20")
        end_of_day = datetime(2026, 11, 20, 23, 59, 59, 999999, tzinfo=BEIJING)
        self.assertEqual(self.ids(view="overdue", now=end_of_day), set())
        midnight_utc = datetime(2026, 11, 20, 16, 0, tzinfo=timezone.utc)
        self.assertEqual(self.ids(view="overdue", now=midnight_utc), {task.task_id})

    def test_datetime_deadline_is_strict_instant_and_only_unfinished(self):
        instant = self.service.create("instant", "b", due="2026-11-20T04:00:00Z")
        prior = self.service.create("prior", "b", due="2026-11-20T11:59:59.999999+08:00")
        progress = self.service.create("progress", "b", due="2026-11-19")
        self.service.update(progress.task_id, 1, status="in_progress")
        for status in ("completed", "cancelled"):
            task = self.service.create(status, "b", due="2026-11-19")
            self.service.update(task.task_id, 1, status=status)
        self.service.create("future", "b", due="2026-11-20T12:00:00.000001+08:00")
        self.service.create("none", "b")
        self.assertEqual(self.ids(view="overdue", now=self.now), {prior.task_id, progress.task_id})
        self.assertIn(instant.task_id, self.ids(view="overdue", now=self.now + timedelta(microseconds=1)))
        self.assertEqual(self.ids(status="completed", view="overdue", now=self.now), set())

    def test_now_rejects_naive_or_non_datetime_values(self):
        for now in (datetime(2026, 11, 20), "2026-11-20T12:00:00Z", date(2026, 11, 20), 0):
            with self.subTest(now=now):
                self.assert_code(ErrorCode.INVALID_ARGUMENT, self.service.list, now=now)
        self.assertEqual(self.service.list(now=datetime(2026, 11, 20, tzinfo=timezone.utc)), [])

    def test_query_is_literal_unicode_casefold_across_title_content_tags(self):
        title = self.service.create("维护网络", "普通记录")
        body = self.service.create("行政", "查验网络情况")
        tagged = self.service.create("记录", "普通", tags=["网络", "Straße"])
        self.service.create("unrelated", "nothing")
        self.assertEqual(self.ids(query="网络"), {title.task_id, body.task_id, tagged.task_id})
        self.assertEqual(self.ids(query="STRASSE"), {tagged.task_id})
        for query in ("%", "_", "' OR 1=1 --"):
            self.assertEqual(self.service.list(query=query), [])
        self.assertEqual(len(self.service.list(query=" \n ")), 4)
        self.service.update(body.task_id, 1, status="completed")
        self.assertEqual(self.ids(query="网络", status="completed"), {body.task_id})
        self.assertEqual(self.ids(query="网络", view="unfinished"), {title.task_id, tagged.task_id})

    def test_order_and_pagination_are_stable_after_filters(self):
        with patch("work_rag.tasks.now_iso", return_value="2026-11-20T08:00:00.000000+08:00"):
            early = [self.service.create(f"early-{index}", "match") for index in range(3)]
        with patch("work_rag.tasks.now_iso", return_value="2026-11-20T09:00:00.000000+08:00"):
            late = self.service.create("late", "match")
        expected = sorted(early, key=lambda task: task.task_id) + [late]
        self.assertEqual(self.service.list(), expected)
        self.assertEqual(self.service.list(), self.service.list())
        self.assertEqual(self.service.list(view="recent"), [late] + expected[:-1])
        self.assertEqual(self.service.list(query="match", limit=2, offset=1), expected[1:3])
        self.assertEqual(self.service.list(limit=0), [])
        self.assertEqual(self.service.list(offset=100), [])

    def test_list_rejects_invalid_filters(self):
        for filters in (
            {"status": "done"}, {"status": []}, {"view": "tomorrow"}, {"view": []},
            {"query": 1}, {"limit": -1}, {"limit": True}, {"limit": 1.0},
            {"offset": -1}, {"offset": "0"}, {"offset": False},
        ):
            with self.subTest(filters=filters):
                self.assert_code(ErrorCode.INVALID_ARGUMENT, self.service.list, **filters)

    def test_create_request_replays_original_snapshot_after_update_and_restart(self):
        original = self.service.create(
            " a ", " b ", due="2026-11-20T00:00:00Z", tags=["z", "a", "z"], request_id="req-1",
        )
        updated = self.service.update(original.task_id, 1, title="changed", status="completed", tags=["new"])
        restarted = TaskService(Store(self.directory.name))
        replay = restarted.create(
            "a", "b", due="2026-11-20T08:00:00+08:00", tags=["a", " z "], request_id="req-1",
        )
        self.assertEqual(replay, original)
        self.assertEqual(restarted.get(original.task_id), updated)
        self.assertEqual((self.count("tasks"), self.count("requests")), (1, 1))
        replay.tags.append("caller mutation")
        self.assertEqual(restarted.create(
            "a", "b", due="2026-11-20T00:00:00Z", tags=["a", "z"], request_id="req-1",
        ), original)
        with closing(self.store.connect()) as connection:
            row = connection.execute("SELECT * FROM requests").fetchone()
        self.assertRegex(row["fingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(json.loads(row["response_json"])["revision"], 1)

    def test_create_request_conflicts_for_any_different_business_input(self):
        task = self.service.create("a", "b", request_id="same")
        for changed in (
            {"title": "other"}, {"content": "other"}, {"due": "2026-11-20"}, {"tags": ["tag"]},
        ):
            with self.subTest(changed=changed):
                values = {"title": "a", "content": "b", "request_id": "same", **changed}
                self.assert_code(ErrorCode.CONFLICT, self.service.create, **values)
        self.assertEqual(self.service.list(), [task])
        self.assertEqual(self.count("requests"), 1)
        # No request ID does not implicitly deduplicate ordinary creates.
        self.assertNotEqual(self.service.create("a", "b").task_id, self.service.create("a", "b").task_id)

    def test_create_request_id_validation_and_length_boundary(self):
        for request_id in ("", " \n", "x" * 201, 1, False, []):
            with self.subTest(request_id=request_id):
                self.assert_code(ErrorCode.INVALID_ARGUMENT, self.service.create, "a", "b", request_id=request_id)
        allowed = "x" * 200
        self.assertEqual(
            self.service.create("a", "b", request_id=allowed),
            self.service.create("a", "b", request_id=allowed),
        )

    def test_create_rolls_back_task_if_request_snapshot_insert_fails(self):
        with self.store.transaction() as connection:
            connection.execute(
                "CREATE TRIGGER fail_request BEFORE INSERT ON requests "
                "BEGIN SELECT RAISE(ABORT, 'injected request failure'); END"
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected request failure"):
            self.service.create("a", "b", request_id="rollback")
        self.assertEqual((self.count("tasks"), self.count("requests")), (0, 0))
        with self.store.transaction() as connection:
            connection.execute("DROP TRIGGER fail_request")
        self.assertEqual(self.service.create("a", "b", request_id="rollback").revision, 1)

    def test_create_rolls_back_both_records_on_transaction_exit_failure(self):
        original_transaction = self.store.transaction

        @contextmanager
        def failing_transaction():
            with original_transaction() as connection:
                yield connection
                raise RuntimeError("injected before commit")

        with patch.object(self.store, "transaction", failing_transaction):
            with self.assertRaisesRegex(RuntimeError, "injected before commit"):
                self.service.create("a", "b", request_id="rollback")
        self.assertEqual((self.count("tasks"), self.count("requests")), (0, 0))
        task = self.service.create("a", "b", request_id="rollback")
        self.assertEqual(self.service.create("a", "b", request_id="rollback"), task)

    def test_failed_update_rolls_back_fields_and_revision(self):
        task = self.service.create("a", "b")
        with self.store.transaction() as connection:
            connection.execute(
                "CREATE TRIGGER fail_update AFTER UPDATE ON tasks "
                "BEGIN SELECT RAISE(ABORT, 'injected update failure'); END"
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected update failure"):
            self.service.update(task.task_id, 1, title="failed", status="completed")
        self.assertEqual(self.service.get(task.task_id), task)

    def test_two_independent_stores_competing_revision_only_one_wins(self):
        task = self.service.create("a", "b")
        services = [TaskService(Store(self.directory.name)) for _ in range(2)]
        barrier = threading.Barrier(2)

        def update(index):
            barrier.wait(timeout=5)
            try:
                return services[index].update(task.task_id, 1, title=f"winner-{index}")
            except CoreError as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(update, range(2)))
        winners = [value for value in outcomes if isinstance(value, Task)]
        errors = [value for value in outcomes if isinstance(value, CoreError)]
        self.assertEqual((len(winners), len(errors)), (1, 1))
        self.assertEqual(errors[0].code, ErrorCode.CONFLICT)
        self.assertEqual(winners[0].revision, 2)
        self.assertEqual(self.service.get(task.task_id), winners[0])

    def test_two_independent_stores_concurrent_idempotent_create(self):
        services = [TaskService(Store(self.directory.name)) for _ in range(2)]
        barrier = threading.Barrier(2)

        def create(index):
            barrier.wait(timeout=5)
            return services[index].create("a", "b", request_id="concurrent")

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(create, range(2)))
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual((self.count("tasks"), self.count("requests")), (1, 1))


if __name__ == "__main__":
    unittest.main()
