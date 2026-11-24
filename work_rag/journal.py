"""Append-only follow-up records and user-selected review snapshots."""
from contextlib import closing
import json

from .contracts import CoreError, ErrorCode, canonical_json, jsonable, new_id, now_iso, require_id
from .storage import remember_request, replay_request
from .tasks import _get, _invalid, _text


def _logs(connection, task_id, after, limit):
    return [dict(row) for row in connection.execute(
        """SELECT * FROM task_logs WHERE task_id=? AND task_revision>?
        ORDER BY task_revision LIMIT ?""", (task_id, after, limit),
    )]


class Journal:
    def __init__(self, store):
        self.store = store

    def append(self, task_id, content, expected_revision, *, corrects=None, request_id=None):
        require_id(task_id, "task")
        content = _text(content, "content")
        if type(expected_revision) is not int or expected_revision < 1:
            raise _invalid("expected_revision must be a positive integer")
        if corrects is not None:
            require_id(corrects, "log")
        payload = {"task_id": task_id, "content": content, "corrects": corrects,
                   "expected_revision": expected_revision}
        with self.store.transaction() as connection:
            fingerprint, old = replay_request(connection, "task_append_log", request_id, payload)
            if old is not None:
                return old
            task = _get(connection, task_id)
            if task.revision != expected_revision:
                raise CoreError(ErrorCode.CONFLICT, "Task revision changed", actual_revision=task.revision)
            if corrects is not None and connection.execute(
                "SELECT 1 FROM task_logs WHERE task_id=? AND log_id=?", (task_id, corrects),
            ).fetchone() is None:
                raise CoreError(ErrorCode.NOT_FOUND, "Correction target must belong to this task")
            result = {"log_id": new_id("log"), "task_id": task_id,
                      "task_revision": task.revision + 1, "content": content,
                      "corrects": corrects, "created_at": now_iso()}
            connection.execute(
                """INSERT INTO task_logs (log_id,task_id,task_revision,content,corrects,created_at)
                VALUES (:log_id,:task_id,:task_revision,:content,:corrects,:created_at)""", result,
            )
            connection.execute(
                "UPDATE tasks SET revision=?,updated_at=? WHERE task_id=?",
                (result["task_revision"], result["created_at"], task_id),
            )
            remember_request(connection, "task_append_log", request_id, fingerprint, result)
            return result

    def logs(self, task_id, *, after=0, limit=100):
        require_id(task_id, "task")
        if type(after) is not int or not 0 <= after < 2**63 or type(limit) is not int or not 1 <= limit <= 200:
            raise _invalid("Invalid log page")
        with closing(self.store.connect()) as connection:
            _get(connection, task_id)
            return _logs(connection, task_id, after, limit)

    def create_review(self, task_ids, sections, *, request_id=None):
        if not isinstance(task_ids, list) or not 1 <= len(task_ids) <= 50:
            raise _invalid("Select 1 to 50 tasks")
        for task_id in task_ids:
            require_id(task_id, "task")
        if len(set(task_ids)) != len(task_ids):
            raise _invalid("Select each task only once")
        if not isinstance(sections, dict) or not sections or sections.keys() - {
            "background", "process", "result", "improvement"
        }:
            raise _invalid("Provide review sections: background, process, result, improvement")
        normalized = {}
        for section, entries in sections.items():
            if not isinstance(entries, list) or not entries:
                raise _invalid("Review sections require a nonempty list of entries")
            normalized[section] = []
            for entry in entries:
                if not isinstance(entry, dict) or entry.keys() != {"kind", "text"} or entry["kind"] not in (
                    "user_statement", "source_excerpt", "ai_inference"
                ):
                    raise _invalid("Review entry needs a kind and text")
                normalized[section].append({"kind": entry["kind"], "text": _text(entry["text"], "text")})
        payload = {"task_ids": task_ids, "sections": normalized}
        with self.store.transaction() as connection:
            fingerprint, old = replay_request(connection, "review_create", request_id, payload)
            if old is not None:
                return old
            snapshots = [
                {"task": jsonable(_get(connection, task_id)), "logs": _logs(connection, task_id, 0, -1)}
                for task_id in task_ids
            ]
            result = {"review_id": new_id("review"), "created_at": now_iso(),
                      "sections": normalized, "snapshots": snapshots}
            connection.execute("INSERT INTO reviews VALUES (?,?,?)",
                               (result["review_id"], result["created_at"], canonical_json(result)))
            remember_request(connection, "review_create", request_id, fingerprint, result)
            return result

    def review(self, review_id):
        require_id(review_id, "review")
        with closing(self.store.connect()) as connection:
            row = connection.execute("SELECT snapshot_json FROM reviews WHERE review_id=?", (review_id,)).fetchone()
        if row is None:
            raise CoreError(ErrorCode.NOT_FOUND, "Review not found")
        return json.loads(row["snapshot_json"])

    def reviews(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise _invalid("Invalid review limit")
        with closing(self.store.connect()) as connection:
            return [dict(row) for row in connection.execute(
                "SELECT review_id,created_at FROM reviews ORDER BY created_at DESC,review_id LIMIT ?", (limit,),
            )]
