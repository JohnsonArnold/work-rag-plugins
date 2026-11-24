"""Explicit-file import, stable business identity and immutable byte versions."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .documents import markdown_links, read_text

from .contracts import (CoreError, ErrorCode, ImportResult, Source, SourceVersion,
                        canonical_json, new_id, now_iso, require_id)
from .storage import Store


class SourceService:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def _source(row) -> Source:
        return Source(*(row[key] for key in ("source_id", "title", "current_version_id", "created_at")))

    @staticmethod
    def _version(row) -> SourceVersion:
        return SourceVersion(**dict(row))

    def get(self, source_id: str) -> Source:
        require_id(source_id, "src")
        with closing(self.store.connect()) as connection:
            row = connection.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
        if row is None:
            raise CoreError(ErrorCode.NOT_FOUND, "Source does not exist", source_id=source_id)
        return self._source(row)

    def list(self) -> list[Source]:
        with closing(self.store.connect()) as connection:
            return [self._source(row) for row in connection.execute("SELECT * FROM sources ORDER BY created_at,source_id")]

    def catalog(self, query="", suffix=None, limit=50, *, include_archived=False):
        if not isinstance(query, str) or (suffix is not None and not isinstance(suffix, str)):
            raise CoreError(ErrorCode.INVALID_ARGUMENT, "Catalog query and suffix must be strings")
        if type(limit) is not int or not 1 <= limit <= 200 or type(include_archived) is not bool:
            raise CoreError(ErrorCode.INVALID_ARGUMENT, "Catalog limit must be between 1 and 200")
        with closing(self.store.connect()) as connection:
            connection.create_function("casefold", 1, str.casefold)
            return [dict(row) for row in connection.execute(
                """SELECT s.source_id,s.title,s.archived,v.version_id,v.original_name,v.size
                FROM sources s JOIN source_versions v ON s.current_version_id=v.version_id
                WHERE (? OR NOT s.archived)
                AND (instr(casefold(s.title),?) OR instr(casefold(v.original_name),?))
                AND (? IS NULL OR substr(casefold(v.original_name),-length(?))=?)
                ORDER BY s.created_at,s.source_id LIMIT ?""",
                (include_archived, query.casefold(), query.casefold(), *([suffix.casefold()] * 3 if suffix else [None]*3), limit),
            )]

    def edit(self, source_id, *, title=None, archived=None):
        require_id(source_id, "src")
        if (title is None and archived is None or title is not None and
                (not isinstance(title, str) or not title.strip()) or
                archived is not None and type(archived) is not bool):
            raise CoreError(ErrorCode.INVALID_ARGUMENT, "Provide a title or archive flag")
        with self.store.transaction() as connection:
            updated = connection.execute(
                """UPDATE sources SET title=COALESCE(?,title),archived=COALESCE(?,archived)
                WHERE source_id=? RETURNING *""",
                (title.strip() if title else None, archived, source_id),
            ).fetchone()
            if updated is None:
                raise CoreError(ErrorCode.NOT_FOUND, "Source does not exist")
            return dict(updated)

    def versions(self, source_id: str) -> list[SourceVersion]:
        self.get(source_id)
        with closing(self.store.connect()) as connection:
            return [self._version(row) for row in connection.execute(
                "SELECT * FROM source_versions WHERE source_id=? ORDER BY sequence", (source_id,))]

    def version(self, source_id: str, version_id: str | None = None) -> SourceVersion:
        require_id(source_id, "src")
        if version_id is not None:
            require_id(version_id, "ver")
        # Resolve current pointer and version in ONE query/snapshot.
        with closing(self.store.connect()) as connection:
            row = connection.execute(
                """SELECT v.* FROM source_versions v JOIN sources s ON s.source_id=v.source_id
                WHERE s.source_id=? AND v.version_id=COALESCE(?,s.current_version_id)""",
                (source_id, version_id)).fetchone()
        if row is None:
            raise CoreError(ErrorCode.VERSION_NOT_FOUND, "Source/version pair does not exist",
                            source_id=source_id, version_id=version_id)
        return self._version(row)

    def snapshot_path(self, source_id: str, version_id: str | None = None) -> Path:
        version = self.version(source_id, version_id)
        return self.store.verify_blob(version.sha256, version.size)

    def attachments(self, version_id):
        require_id(version_id, "ver")
        with closing(self.store.connect()) as connection:
            return [dict(row) for row in connection.execute(
                "SELECT relative_path,sha256,size FROM source_attachments WHERE version_id=? ORDER BY relative_path",
                (version_id,),
            )]

    def _stage_attachments(self, path, checksum, size):
        if path.suffix.casefold() != ".md":
            return []
        snapshot = self.store.verify_blob(checksum, size)
        if size > 16 * 1024**2:
            raise CoreError(ErrorCode.UNSUPPORTED, "Markdown exceeds the 16 MiB text limit")
        try:
            text = read_text(snapshot)["text"]
        except CoreError:
            return []
        entries = {}
        for link in markdown_links(text):
            try:
                target = urlsplit(link)
            except ValueError:
                continue
            if target.scheme or target.netloc or not target.path:
                continue
            relative = unquote(target.path)
            if not relative or "\x00" in relative or relative in entries:
                continue
            if Path(relative).is_absolute() or ".." in Path(relative).parts:
                continue
            resolved = (path.parent / relative).resolve()
            if not resolved.is_relative_to(path.parent):
                continue
            entries[relative] = self.store.stage_file(resolved) if resolved.is_file() else (None, None)
        return [(name, *entries[name]) for name in sorted(entries)]

    def import_file(self, path: str | Path, *, source_id: str | None = None,
                    origin_key: str | None = None, title: str | None = None,
                    request_id: str | None = None,
                    expected_current_version: str | None = None) -> ImportResult:
        path = Path(path).expanduser().resolve(strict=True)
        if not path.is_file():
            raise CoreError(ErrorCode.INVALID_ARGUMENT, "Import requires a regular file")
        if source_id is not None:
            require_id(source_id, "src")
        if expected_current_version is not None:
            require_id(expected_current_version, "ver")
            if source_id is None:
                raise CoreError(ErrorCode.INVALID_ARGUMENT, "Expected version requires explicit source ID")
        if origin_key is None:
            origin_key = path.as_uri()
        if not isinstance(origin_key, str) or not origin_key.strip():
            raise CoreError(ErrorCode.INVALID_ARGUMENT, "Origin key must be nonempty")
        if title is not None and (not isinstance(title, str) or not title.strip()):
            raise CoreError(ErrorCode.INVALID_ARGUMENT, "Title must be nonempty")
        if request_id is not None and (not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200):
            raise CoreError(ErrorCode.INVALID_ARGUMENT, "Request ID must be nonempty and at most 200 characters")
        checksum, size = self.store.stage_file(path)
        attachments = self._stage_attachments(path, checksum, size)
        payload = {"source_id": source_id, "origin_key": origin_key, "title": title,
                   "original_name": path.name, "sha256": checksum,
                   "expected_current_version": expected_current_version}
        if attachments:
            payload["attachments"] = attachments
        fingerprint = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
        with self.store.transaction() as connection:
            if request_id is not None:
                previous = connection.execute(
                    "SELECT * FROM requests WHERE operation='source_import' AND request_id=?", (request_id,)).fetchone()
                if previous:
                    if previous["fingerprint"] != fingerprint:
                        raise CoreError(ErrorCode.CONFLICT, "Request ID was used with different import arguments")
                    response = json.loads(previous["response_json"])
                    return ImportResult(Source(**response["source"]), SourceVersion(**response["version"]), response["changed"])
            mapping = connection.execute("SELECT source_id FROM origin_mappings WHERE origin_key=?", (origin_key,)).fetchone()
            if mapping is not None and source_id is not None and mapping["source_id"] != source_id:
                raise CoreError(ErrorCode.CONFLICT, "Origin key belongs to another source", source_id=mapping["source_id"])
            identity = source_id or (mapping["source_id"] if mapping else None)
            existing = None
            if identity is not None:
                existing = connection.execute("SELECT * FROM sources WHERE source_id=?", (identity,)).fetchone()
                if existing is None:
                    raise CoreError(ErrorCode.NOT_FOUND, "Explicit source does not exist", source_id=identity)
            created = now_iso()
            if existing is not None:
                if expected_current_version is not None and existing["current_version_id"] != expected_current_version:
                    raise CoreError(ErrorCode.CONFLICT, "Current source version changed", current_version_id=existing["current_version_id"])
                current = connection.execute("SELECT * FROM source_versions WHERE version_id=?", (existing["current_version_id"],)).fetchone()
                previous_attachments = [tuple(row) for row in connection.execute(
                    "SELECT relative_path,sha256,size FROM source_attachments WHERE version_id=? ORDER BY relative_path",
                    (current["version_id"],),
                )]
                previous_suffix = Path(current["original_name"]).suffix.casefold()
                changed = (current["sha256"] != checksum or previous_attachments != attachments or
                           (previous_suffix != path.suffix.casefold() and ".md" in (previous_suffix, path.suffix.casefold())))
                if changed:
                    version = SourceVersion(identity, new_id("ver"), current["sequence"] + 1, checksum, size, path.name, created)
                    self._insert_version(connection, version)
                    connection.execute("UPDATE sources SET current_version_id=? WHERE source_id=?", (version.version_id, identity))
                else:
                    version = self._version(current)
                source = Source(identity, existing["title"], version.version_id, existing["created_at"])
            else:
                identity = new_id("src")
                version = SourceVersion(identity, new_id("ver"), 1, checksum, size, path.name, created)
                source = Source(identity, title.strip() if title else path.name, version.version_id, created)
                connection.execute("INSERT INTO sources (source_id,title,current_version_id,created_at) VALUES (?,?,?,?)", (source.source_id, source.title, source.current_version_id, created))
                self._insert_version(connection, version)
                changed = True
            if changed:
                connection.executemany("INSERT INTO source_attachments VALUES (?,?,?,?)",
                                       [(version.version_id, *item) for item in attachments])
            connection.execute("INSERT OR IGNORE INTO origin_mappings VALUES (?,?)", (origin_key, identity))
            result = ImportResult(source, version, changed)
            if request_id is not None:
                connection.execute("INSERT INTO requests VALUES ('source_import',?,?,?)", (request_id, fingerprint, canonical_json(result)))
            return result

    @staticmethod
    def _insert_version(connection, version: SourceVersion) -> None:
        connection.execute("INSERT INTO source_versions (version_id,source_id,sequence,sha256,size,original_name,created_at) VALUES (?,?,?,?,?,?,?)",
                           (version.version_id, version.source_id, version.sequence, version.sha256,
                            version.size, version.original_name, version.created_at))
