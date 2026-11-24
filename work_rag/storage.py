"""SQLite repository and content-addressed immutable snapshots.

Publish durable blobs before DB pointers. Crashes can leave unreferenced blobs,
never a DB pointer to staging. Recovery reports leftovers, it never removes files
which another importer might still be using.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from uuid import uuid4

from .contracts import CoreError, ErrorCode, canonical_json
from .filesystem import exclusive_file_lock, fsync_dir, publish_blob, remove_staging

SCHEMA_VERSION = 4
MIGRATIONS = [
    """CREATE TABLE sources (
        source_id TEXT PRIMARY KEY, title TEXT NOT NULL,
        current_version_id TEXT NOT NULL REFERENCES source_versions(version_id)
            DEFERRABLE INITIALLY DEFERRED,
        created_at TEXT NOT NULL)""",
    """CREATE TABLE source_versions (
        version_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL REFERENCES sources(source_id),
        sequence INTEGER NOT NULL CHECK(sequence > 0), sha256 TEXT NOT NULL,
        size INTEGER NOT NULL CHECK(size >= 0), original_name TEXT NOT NULL,
        created_at TEXT NOT NULL, UNIQUE(source_id,sequence))""",
    """CREATE TABLE origin_mappings (
        origin_key TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(source_id))""",
    """CREATE TABLE tasks (
        task_id TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending','in_progress','completed','cancelled')),
        due TEXT, due_kind TEXT CHECK(due_kind IN ('date','datetime')),
        tags_json TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision > 0),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE requests (
        operation TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
        response_json TEXT NOT NULL, PRIMARY KEY(operation,request_id))""",
]

MIGRATION_2 = [
    """CREATE TABLE task_logs (
        log_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
        task_revision INTEGER NOT NULL CHECK(task_revision > 1), content TEXT NOT NULL,
        corrects TEXT REFERENCES task_logs(log_id), created_at TEXT NOT NULL,
        UNIQUE(task_id, task_revision))""",
    """CREATE TABLE reviews (
        review_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, snapshot_json TEXT NOT NULL)""",
    "ALTER TABLE sources ADD COLUMN archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1))",
    """CREATE TABLE artifacts (
        artifact_id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES source_versions(version_id),
        backend TEXT NOT NULL, scope TEXT NOT NULL, args TEXT NOT NULL, state TEXT NOT NULL,
        payload TEXT NOT NULL, UNIQUE(version_id,backend,scope,args))""",
    """CREATE TABLE artifact_failures (
        version_id TEXT NOT NULL REFERENCES source_versions(version_id),
        backend TEXT NOT NULL, scope TEXT NOT NULL, args TEXT NOT NULL, error TEXT NOT NULL,
        PRIMARY KEY(version_id,backend,scope,args))""",
    """CREATE TABLE search_entries (
        artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id), ordinal INTEGER NOT NULL,
        text TEXT NOT NULL, normalized TEXT NOT NULL, locator TEXT NOT NULL, PRIMARY KEY(artifact_id,ordinal))""",
    """CREATE TABLE source_attachments (
        version_id TEXT NOT NULL REFERENCES source_versions(version_id), relative_path TEXT NOT NULL,
        sha256 TEXT, size INTEGER, PRIMARY KEY(version_id, relative_path))""",
]

MIGRATION_4 = [
    """CREATE TABLE source_roots (
        root_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        path TEXT NOT NULL,
        normalized_path TEXT NOT NULL UNIQUE,
        recursive INTEGER NOT NULL DEFAULT 1 CHECK(recursive IN (0,1)),
        suffixes_json TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_synced_at TEXT
    )""",
]


_held_library_locks = ContextVar("held_library_locks", default=frozenset())


@contextmanager
def library_lock(data_dir: str | Path):
    """Coordinate short writes and supported publication, even across directory rename.

    The lock file lives beside, not inside, the directory being published. Keep
    it in place: unlinking a lock file would split concurrent processes' locks.
    Context-local reentrancy lets a bound bridge call enter core transactions.
    """
    directory = Path(data_dir).expanduser().resolve()
    held = _held_library_locks.get()
    if directory in held:
        yield
        return
    directory.parent.mkdir(parents=True, exist_ok=True)
    # Existing installations must take the same lock when sharing a library.
    lock_path = directory.parent / f".{directory.name}.it-rag-switch.lock"
    with lock_path.open("a+b") as handle, exclusive_file_lock(handle):
        token = _held_library_locks.set(held | {directory})
        try:
            yield
        finally:
            _held_library_locks.reset(token)


class Store:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.blobs = self.data_dir / "blobs"
        self.staging = self.data_dir / "staging"
        self.db_path = self.data_dir / "core.sqlite3"
        self._expected_binding = ContextVar("expected_library_binding", default=None)
        with library_lock(self.data_dir):
            created = not self.data_dir.exists()
            self.data_dir.mkdir(parents=True, exist_ok=True)
            changed = created or not self.blobs.exists() or not self.staging.exists()
            self.blobs.mkdir(exist_ok=True)
            self.staging.mkdir(exist_ok=True)
            if changed:
                fsync_dir(self.data_dir)
            if created:
                fsync_dir(self.data_dir.parent)
            self._migrate()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def transaction(self):
        with library_lock(self.data_dir):
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                expected = self._expected_binding.get()
                if expected is not None:
                    self.check_binding(expected, connection=connection)
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def binding(self, *, connection=None) -> dict:
        if connection is None:
            with closing(self.connect()) as opened:
                return self.binding(connection=opened)
        row = connection.execute(
            "SELECT library_id,restore_generation FROM library_binding WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise CoreError(ErrorCode.INTEGRITY, "Library binding is missing")
        return dict(row)

    def check_binding(self, expected: dict, *, connection=None) -> dict:
        actual = self.binding(connection=connection)
        if expected != actual:
            raise CoreError(ErrorCode.CONFLICT, "Library binding changed; prepare a new operation",
                            reason="binding_mismatch", expected_binding=expected, actual_binding=actual)
        return actual

    @contextmanager
    def bound_to(self, expected: dict | None = None):
        """Pin publication and check the binding again inside business transactions."""
        with library_lock(self.data_dir):
            actual = self.binding() if expected is None else self.check_binding(expected)
            token = self._expected_binding.set(actual)
            try:
                yield actual
            finally:
                self._expected_binding.reset(token)

    def renew_restore_generation(self) -> dict:
        """Stamp an unpublished supported copy before its directory is published."""
        with self.transaction() as connection:
            connection.execute("UPDATE library_binding SET restore_generation=? WHERE singleton=1",
                               (f"generation_{uuid4().hex}",))
            return self.binding(connection=connection)

    def _migrate(self) -> None:
        # The normal short-process read path takes no SQLite write lock or fsync.
        # Store initialization holds library_lock, so migration cannot race here.
        with closing(self.connect()) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version < 0 or version > SCHEMA_VERSION:
            raise CoreError(ErrorCode.UNSUPPORTED, "Unsupported database schema version", version=version)
        if version == SCHEMA_VERSION:
            return
        with self.transaction() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                for statement in MIGRATIONS:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version=1")
            if version < 2:
                for statement in MIGRATION_2:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version=2")
            if version < 3:
                connection.execute("""CREATE TABLE library_binding (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    library_id TEXT NOT NULL, restore_generation TEXT NOT NULL)""")
                connection.execute("INSERT INTO library_binding VALUES (1,?,?)",
                                   (f"library_{uuid4().hex}", f"generation_{uuid4().hex}"))
                connection.execute("PRAGMA user_version=3")
            if version < 4:
                for statement in MIGRATION_4:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version=4")
        fsync_dir(self.data_dir)

    def blob_path(self, sha256: str) -> Path:
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise CoreError(ErrorCode.INVALID_ARGUMENT, "Invalid blob SHA-256")
        return self.blobs / sha256

    def verify_blob(self, sha256: str, size: int | None = None) -> Path:
        path = self.blob_path(sha256)
        try:
            with path.open("rb") as file:
                digest = hashlib.file_digest(file, "sha256").hexdigest()
                actual_size = file.tell()
        except FileNotFoundError as error:
            raise CoreError(ErrorCode.INTEGRITY, "Referenced blob is missing", sha256=sha256) from error
        if digest != sha256 or (size is not None and size != actual_size):
            raise CoreError(ErrorCode.INTEGRITY, "Blob content does not match metadata", sha256=sha256)
        return path

    @staticmethod
    def _signature(info: os.stat_result) -> tuple:
        if os.name == "nt":
            # On Windows (Python 3.12+), os.fstat() and os.stat() can disagree on st_ctime_ns
            # (ChangeTime vs CreationTime). NTFS st_dev, st_ino, st_size, and 100ns st_mtime_ns
            # remain consistent across both APIs and still detect file replacement or edits.
            return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def stage_file(self, input_path: str | Path) -> tuple[str, int]:
        """Copy one explicitly named regular file; detect concurrent changes."""
        path = Path(input_path).expanduser().resolve(strict=True)
        descriptor, temporary = tempfile.mkstemp(prefix="import-", dir=self.staging)
        temporary_path = Path(temporary)
        try:
            digest = hashlib.sha256()
            size = 0
            with os.fdopen(descriptor, "wb") as output, path.open("rb") as source:
                before = os.fstat(source.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise CoreError(ErrorCode.INVALID_ARGUMENT, "Import requires a regular file")
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                after = os.fstat(source.fileno())
                if self._signature(before) != self._signature(after) or self._signature(after) != self._signature(path.stat()) or size != before.st_size:
                    raise CoreError(ErrorCode.SOURCE_CHANGED, "Input changed during import; retry a stable copy")
                output.flush()
                os.fsync(output.fileno())
            checksum = digest.hexdigest()
            # Verify the stored bytes before publishing, not just the input stream.
            with temporary_path.open("rb") as check:
                if hashlib.file_digest(check, "sha256").hexdigest() != checksum:
                    raise CoreError(ErrorCode.INTEGRITY, "Staged snapshot checksum mismatch")
            destination = self.blob_path(checksum)
            try:
                # Atomic no-clobber publication on the same data volume.
                publish_blob(temporary_path, destination)
            except FileExistsError:
                self.verify_blob(checksum, size)
            fsync_dir(self.blobs)
            return checksum, size
        finally:
            # mkstemp produced this exact path in the intended staging directory.
            if temporary_path.parent.resolve() != self.staging.resolve():
                raise RuntimeError("Unexpected staging path")
            remove_staging(temporary_path)
            fsync_dir(self.staging)

    def recovery_report(self) -> dict:
        with self.transaction() as connection:
            versions = connection.execute(
                "SELECT sha256,size FROM source_versions UNION SELECT sha256,size FROM source_attachments WHERE sha256 IS NOT NULL"
            ).fetchall()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
        corrupt = []
        referenced = {row["sha256"] for row in versions}
        for row in versions:
            try:
                self.verify_blob(row["sha256"], row["size"])
            except CoreError as error:
                corrupt.append(error.to_dict())
        return {"schema_version": SCHEMA_VERSION, "sqlite_integrity": integrity,
                "foreign_key_errors": foreign_keys, "corrupt_or_missing": corrupt,
                "orphan_blobs": sorted(p.name for p in self.blobs.iterdir() if p.name not in referenced),
                "staging_files": sorted(p.name for p in self.staging.iterdir()),
                "cleanup": "report_only; no automatic deletion during concurrent imports"}


def replay_request(connection, operation, request_id, payload):
    if request_id is None:
        return None, None
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
        raise CoreError(ErrorCode.INVALID_ARGUMENT, "request_id must be a non-empty string of at most 200 characters")
    fingerprint = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    previous = connection.execute(
        "SELECT fingerprint,response_json FROM requests WHERE operation=? AND request_id=?",
        (operation, request_id),
    ).fetchone()
    if previous is None:
        return fingerprint, None
    if previous["fingerprint"] != fingerprint:
        raise CoreError(ErrorCode.CONFLICT, "request_id already used with different input")
    return fingerprint, json.loads(previous["response_json"])


def remember_request(connection, operation, request_id, fingerprint, result):
    if request_id is not None:
        connection.execute(
            "INSERT INTO requests VALUES (?,?,?,?)",
            (operation, request_id, fingerprint, canonical_json(result)),
        )
