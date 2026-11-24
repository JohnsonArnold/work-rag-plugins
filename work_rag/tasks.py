"""Persistent task operations, optimistic revisions, and Beijing date views."""
from __future__ import annotations

from contextlib import closing
from datetime import date, datetime
import hashlib
import json
import re
import sqlite3

from .contracts import (
    BEIJING,
    CoreError,
    ErrorCode,
    Task,
    canonical_json,
    jsonable,
    new_id,
    now_iso,
    require_id,
)
from .storage import Store, remember_request, replay_request


_STATUSES = frozenset({"pending", "in_progress", "completed", "cancelled"})
_UNFINISHED = frozenset({"pending", "in_progress"})
_VIEWS = frozenset({"unfinished", "today", "overdue", "recent"})
_FIELDS = frozenset({"title", "content", "status", "due", "tags"})
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
# Accept extended ISO dates/times, optional seconds, and explicit ISO offsets.
_DATETIME = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}"
    r"(?::\d{2}(?:[.,]\d{1,6})?)?"
    r"(?:[Zz]|[+-](?:[01]\d|2[0-3])(?::?[0-5]\d)?)",
    re.ASCII,
)


def _invalid(message: str, **details) -> CoreError:
    return CoreError(ErrorCode.INVALID_ARGUMENT, message, **details)


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{field} must be a non-empty string", field=field)
    return value.strip()


def _tags(value) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise _invalid("tags must be a list of non-empty strings", field="tags")
    return sorted({_text(tag, "tags") for tag in value})


def _status(value: str) -> str:
    if not isinstance(value, str) or value not in _STATUSES:
        raise _invalid("Unknown task status", field="status")
    return value


def _due(value: str | None) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, str):
        raise _invalid("due must be an ISO date or timezone-aware datetime", field="due")
    try:
        if _DATE.fullmatch(value):
            return date.fromisoformat(value).isoformat(), "date"
        if _DATETIME.fullmatch(value):
            parsed = datetime.fromisoformat(value.replace("t", "T").replace("z", "Z"))
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                return parsed.astimezone(BEIJING).isoformat(), "datetime"
    except (ValueError, OverflowError):
        pass
    raise _invalid("due must be an ISO date or timezone-aware datetime", field="due")


def _task(row: sqlite3.Row) -> Task:
    return Task(
        task_id=row["task_id"],
        title=row["title"],
        content=row["content"],
        status=row["status"],
        due=row["due"],
        due_kind=row["due_kind"],
        tags=json.loads(row["tags_json"]),
        revision=row["revision"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _get(connection: sqlite3.Connection, task_id: str) -> Task:
    row = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        raise CoreError(ErrorCode.NOT_FOUND, "Task not found", task_id=task_id)
    return _task(row)


class TaskService:
    """Task business rules over Store; no notification or deletion side effects.

    Text and tags are trimmed. Default ordering is creation time ascending then
    task ID; ``recent`` reverses creation time with task ID as a stable tie-break.
    Query is a literal Unicode-case-insensitive substring of title/content/tags.
    """

    def __init__(self, store: Store):
        self.store = store

    def create(
        self,
        title: str,
        content: str,
        *,
        due: str | None = None,
        tags: list[str] | None = None,
        request_id: str | None = None,
    ) -> Task:
        title, content = _text(title, "title"), _text(content, "content")
        due, due_kind = _due(due)
        tags = _tags(tags)
        if request_id is not None and (
            not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200
        ):
            raise _invalid("request_id must be a non-empty string of at most 200 characters")
        payload = {
            "title": title, "content": content, "status": "pending",
            "due": due, "due_kind": due_kind, "tags": tags,
        }
        fingerprint = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        with self.store.transaction() as connection:
            if request_id is not None:
                previous = connection.execute(
                    "SELECT fingerprint, response_json FROM requests "
                    "WHERE operation = ? AND request_id = ?",
                    ("task_create", request_id),
                ).fetchone()
                if previous is not None:
                    if previous["fingerprint"] != fingerprint:
                        raise CoreError(
                            ErrorCode.CONFLICT, "request_id already used with different task input",
                            request_id=request_id,
                        )
                    return Task(**json.loads(previous["response_json"]))
            timestamp = now_iso()
            task = Task(
                task_id=new_id("task"), revision=1,
                created_at=timestamp, updated_at=timestamp, **payload,
            )
            connection.execute(
                "INSERT INTO tasks "
                "(task_id, title, content, status, due, due_kind, tags_json, revision, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task.task_id, title, content, task.status, due, due_kind,
                 canonical_json(tags), task.revision, timestamp, timestamp),
            )
            if request_id is not None:
                connection.execute(
                    "INSERT INTO requests (operation, request_id, fingerprint, response_json) "
                    "VALUES (?, ?, ?, ?)",
                    ("task_create", request_id, fingerprint, canonical_json(jsonable(task))),
                )
            return task

    def get(self, task_id: str) -> Task:
        require_id(task_id, "task")
        with closing(self.store.connect()) as connection:
            return _get(connection, task_id)

    def update(self, task_id: str, expected_revision: int, *, request_id=None, **changes) -> Task:
        require_id(task_id, "task")
        if type(expected_revision) is not int or expected_revision < 1:
            raise _invalid("expected_revision must be a positive integer")
        if not changes or changes.keys() - _FIELDS:
            raise _invalid("Provide only supported task changes", fields=sorted(changes))
        normalized = {}
        for field, value in changes.items():
            if field in {"title", "content"}:
                normalized[field] = _text(value, field)
            elif field == "status":
                normalized[field] = _status(value)
            elif field == "tags":
                normalized["tags_json"] = canonical_json(_tags(value))
            elif field == "due":
                normalized["due"], normalized["due_kind"] = _due(value)
        payload = {"task_id": task_id, "expected_revision": expected_revision, "changes": normalized}
        with self.store.transaction() as connection:
            fingerprint, previous = replay_request(connection, "task_update", request_id, payload)
            if previous is not None:
                return Task(**previous)
            current = _get(connection, task_id)
            if current.revision != expected_revision:
                raise CoreError(
                    ErrorCode.CONFLICT, "Task revision changed", task_id=task_id,
                    expected_revision=expected_revision, actual_revision=current.revision,
                )
            normalized["updated_at"] = now_iso()
            assignments = ", ".join(f"{field} = ?" for field in normalized)
            cursor = connection.execute(
                f"UPDATE tasks SET {assignments}, revision = revision + 1 "
                "WHERE task_id = ? AND revision = ?",
                (*normalized.values(), task_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise CoreError(ErrorCode.CONFLICT, "Task revision changed", task_id=task_id)
            result = _get(connection, task_id)
            remember_request(connection, "task_update", request_id, fingerprint, result)
            return result

    def list(
        self,
        *,
        status: str | None = None,
        view: str | None = None,
        query: str | None = None,
        limit: int = 100,
        offset: int = 0,
        now: datetime | None = None,
    ) -> list[Task]:
        if status is not None:
            _status(status)
        if view is not None and (not isinstance(view, str) or view not in _VIEWS):
            raise _invalid("Unknown task view", field="view")
        if query is not None and not isinstance(query, str):
            raise _invalid("query must be a string", field="query")
        if type(limit) is not int or limit < 0 or type(offset) is not int or offset < 0:
            raise _invalid("limit and offset must be non-negative integers")
        if now is None:
            now = datetime.now(BEIJING)
        elif not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise _invalid("now must be a timezone-aware datetime", field="now")
        now = now.astimezone(BEIJING)
        today = now.date().isoformat()
        needle = query.strip().casefold() if query else ""
        direction = "DESC" if view == "recent" else "ASC"
        sql = "SELECT * FROM tasks"
        parameters = ()
        if status is not None:
            sql += " WHERE status = ?"
            parameters = (status,)
        sql += f" ORDER BY created_at {direction}, task_id ASC"
        # Python comparison retains Unicode casefold semantics (SQLite lower()
        # handles ASCII only) and compares datetime deadlines as absolute instants.
        with closing(self.store.connect()) as connection:
            tasks = [_task(row) for row in connection.execute(sql, parameters)]
        matches = []
        for task in tasks:
            if view in {"unfinished", "overdue"} and task.status not in _UNFINISHED:
                continue
            if view == "today" and (task.due is None or task.due[:10] != today):
                continue
            if view == "overdue":
                if task.due is None:
                    continue
                if task.due_kind == "date":
                    overdue = task.due < today
                else:
                    overdue = datetime.fromisoformat(task.due) < now
                if not overdue:
                    continue
            if needle and not any(needle in text.casefold() for text in (task.title, task.content, *task.tags)):
                continue
            matches.append(task)
        return matches[offset:offset + limit]
