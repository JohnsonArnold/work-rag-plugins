"""Managed local source directories: registration, diff scanning, and explicit sync."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import string
from urllib.parse import unquote, urlsplit

from .contracts import CoreError, ErrorCode, SourceRoot, canonical_json, new_id, now_iso, require_id
from .documents import markdown_links, read_text
from .sources import SourceService
from .storage import Store

DEFAULT_SUFFIXES = (
    ".xlsx",
    ".pdf",
    ".docx",
    ".md",
    ".txt",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".tif",
    ".tiff",
)

SKIP_DIR_NAMES = frozenset({
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "node_modules",
    ".venv",
    ".venv-win",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".work-rag-data",
    ".it-rag-data",
})


def _norm_key(path: Path) -> str:
    resolved = str(path.resolve())
    return resolved.casefold() if os.name == "nt" else resolved


def _is_temp_file(name: str) -> bool:
    return name.startswith("~$") or name.startswith(".~") or name.endswith(".tmp")


def _normalize_suffixes(suffixes) -> list[str]:
    if suffixes is None:
        return list(DEFAULT_SUFFIXES)
    if not isinstance(suffixes, (list, tuple)) or not suffixes:
        raise CoreError(ErrorCode.INVALID_ARGUMENT, "suffixes must be a non-empty list of file extensions")
    cleaned = []
    for item in suffixes:
        if not isinstance(item, str) or not item.strip():
            raise CoreError(ErrorCode.INVALID_ARGUMENT, "Each suffix must be a non-empty string")
        value = item.strip().casefold()
        if not value.startswith("."):
            value = "." + value
        if len(value) < 2 or any(ch in value for ch in "/\\\x00 "):
            raise CoreError(ErrorCode.INVALID_ARGUMENT, f"Invalid file suffix: {item}")
        cleaned.append(value)
    return sorted(set(cleaned))


def _file_digest(path: Path) -> tuple[str, int]:
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
        size = handle.tell()
    return digest, size


def _markdown_attachment_signature(path: Path) -> list[tuple[str, str | None, int | None]]:
    if path.suffix.casefold() != ".md":
        return []
    try:
        if path.stat().st_size > 16 * 1024**2:
            return []
        text = read_text(path)["text"]
    except (OSError, CoreError):
        return []
    entries: dict[str, tuple[str | None, int | None]] = {}
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
        if resolved.is_file():
            try:
                entries[relative] = _file_digest(resolved)
            except OSError:
                entries[relative] = (None, None)
        else:
            entries[relative] = (None, None)
    return [(name, *entries[name]) for name in sorted(entries)]


def browse_directories(path: str | None = None) -> dict:
    """List drives/shortcuts or immediate subdirectories for UI folder selection."""
    if path is None or not str(path).strip():
        entries = []
        home = Path.home()
        if home.is_dir():
            entries.append({"name": f"主目录 ({home})", "path": str(home.resolve()), "kind": "shortcut"})
        if os.name == "nt":
            for letter in string.ascii_uppercase:
                drive = Path(f"{letter}:\\")
                if drive.exists():
                    entries.append({"name": f"{letter}:\\", "path": str(drive), "kind": "drive"})
        else:
            entries.append({"name": "/", "path": "/", "kind": "drive"})
        return {"current": None, "parent": None, "entries": entries}

    target = Path(path).expanduser()
    if not target.is_absolute():
        raise CoreError(ErrorCode.INVALID_ARGUMENT, "Directory path must be absolute")
    try:
        resolved = target.resolve(strict=True)
    except OSError as error:
        raise CoreError(ErrorCode.NOT_FOUND, "Directory does not exist", path=str(target)) from error
    if not resolved.is_dir():
        raise CoreError(ErrorCode.INVALID_ARGUMENT, "Path is not a directory", path=str(resolved))

    parent = str(resolved.parent) if resolved.parent != resolved else None
    entries = []
    try:
        with os.scandir(resolved) as it:
            for item in it:
                try:
                    if not item.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if item.name.startswith(".") and item.name not in {".config"}:
                    continue
                if item.name in {"$RECYCLE.BIN", "System Volume Information"}:
                    continue
                entries.append({"name": item.name, "path": str(Path(item.path).resolve()), "kind": "dir"})
    except PermissionError as error:
        raise CoreError(ErrorCode.UNSUPPORTED, "Permission denied while reading directory", path=str(resolved)) from error
    entries.sort(key=lambda item: item["name"].casefold())
    return {"current": str(resolved), "parent": parent, "entries": entries}


class RootService:
    """Register local working folders, preview changes, and explicitly sync files."""

    def __init__(self, store: Store, sources: SourceService, knowledge=None):
        self.store = store
        self.sources = sources
        self.knowledge = knowledge

    @staticmethod
    def _row(row) -> SourceRoot:
        return SourceRoot(
            root_id=row["root_id"],
            name=row["name"],
            path=row["path"],
            recursive=bool(row["recursive"]),
            suffixes=json.loads(row["suffixes_json"]),
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_synced_at=row["last_synced_at"],
        )

    def _validate_dir(self, raw_path: str | Path) -> tuple[Path, str]:
        if not isinstance(raw_path, (str, Path)) or not str(raw_path).strip():
            raise CoreError(ErrorCode.INVALID_ARGUMENT, "Directory path must be a non-empty string")
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            raise CoreError(ErrorCode.INVALID_ARGUMENT, "Managed directory path must be absolute")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise CoreError(ErrorCode.NOT_FOUND, "Directory does not exist", path=str(candidate)) from error
        if not resolved.is_dir():
            raise CoreError(ErrorCode.INVALID_ARGUMENT, "Managed root must be an existing directory", path=str(resolved))
        if resolved == self.store.data_dir or resolved.is_relative_to(self.store.data_dir):
            raise CoreError(ErrorCode.INVALID_ARGUMENT, "Managed directory cannot be inside the work-rag data directory")
        return resolved, _norm_key(resolved)

    def add(
        self,
        path: str | Path,
        *,
        name: str | None = None,
        recursive: bool = True,
        suffixes: list[str] | None = None,
        enabled: bool = True,
    ) -> SourceRoot:
        if type(recursive) is not bool or type(enabled) is not bool:
            raise CoreError(ErrorCode.INVALID_ARGUMENT, "recursive and enabled must be booleans")
        resolved, norm = self._validate_dir(path)
        label = (name.strip() if isinstance(name, str) else "") or resolved.name or str(resolved)
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise CoreError(ErrorCode.INVALID_ARGUMENT, "name must be a non-empty string")
        normalized_suffixes = _normalize_suffixes(suffixes)
        timestamp = now_iso()
        root = SourceRoot(
            root_id=new_id("root"),
            name=label,
            path=str(resolved),
            recursive=recursive,
            suffixes=normalized_suffixes,
            enabled=enabled,
            created_at=timestamp,
            updated_at=timestamp,
            last_synced_at=None,
        )
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT root_id FROM source_roots WHERE normalized_path=?",
                (norm,),
            ).fetchone()
            if existing is not None:
                raise CoreError(ErrorCode.CONFLICT, "Directory is already registered", root_id=existing["root_id"])
            connection.execute(
                """INSERT INTO source_roots
                (root_id, name, path, normalized_path, recursive, suffixes_json, enabled, created_at, updated_at, last_synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    root.root_id,
                    root.name,
                    root.path,
                    norm,
                    int(root.recursive),
                    canonical_json(root.suffixes),
                    int(root.enabled),
                    root.created_at,
                    root.updated_at,
                    root.last_synced_at,
                ),
            )
        return root

    def get(self, root_id: str) -> SourceRoot:
        require_id(root_id, "root")
        with closing(self.store.connect()) as connection:
            row = connection.execute("SELECT * FROM source_roots WHERE root_id=?", (root_id,)).fetchone()
        if row is None:
            raise CoreError(ErrorCode.NOT_FOUND, "Managed directory not found", root_id=root_id)
        return self._row(row)

    def list(self, *, enabled_only: bool = False) -> list[SourceRoot]:
        if type(enabled_only) is not bool:
            raise CoreError(ErrorCode.INVALID_ARGUMENT, "enabled_only must be a boolean")
        sql = "SELECT * FROM source_roots"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY created_at, root_id"
        with closing(self.store.connect()) as connection:
            return [self._row(row) for row in connection.execute(sql)]

    def update(
        self,
        root_id: str,
        *,
        name: str | None = None,
        path: str | Path | None = None,
        recursive: bool | None = None,
        suffixes: list[str] | None = None,
        enabled: bool | None = None,
    ) -> SourceRoot:
        require_id(root_id, "root")
        changes: dict[str, object] = {}
        if name is not None:
            if not isinstance(name, str) or not name.strip():
                raise CoreError(ErrorCode.INVALID_ARGUMENT, "name must be a non-empty string")
            changes["name"] = name.strip()
        if path is not None:
            resolved, norm = self._validate_dir(path)
            changes["path"] = str(resolved)
            changes["normalized_path"] = norm
        if recursive is not None:
            if type(recursive) is not bool:
                raise CoreError(ErrorCode.INVALID_ARGUMENT, "recursive must be a boolean")
            changes["recursive"] = int(recursive)
        if suffixes is not None:
            changes["suffixes_json"] = canonical_json(_normalize_suffixes(suffixes))
        if enabled is not None:
            if type(enabled) is not bool:
                raise CoreError(ErrorCode.INVALID_ARGUMENT, "enabled must be a boolean")
            changes["enabled"] = int(enabled)
        if not changes:
            raise CoreError(ErrorCode.INVALID_ARGUMENT, "Provide at least one field to update")
        changes["updated_at"] = now_iso()
        with self.store.transaction() as connection:
            current = connection.execute("SELECT * FROM source_roots WHERE root_id=?", (root_id,)).fetchone()
            if current is None:
                raise CoreError(ErrorCode.NOT_FOUND, "Managed directory not found", root_id=root_id)
            if "normalized_path" in changes:
                dup = connection.execute(
                    "SELECT root_id FROM source_roots WHERE normalized_path=? AND root_id<>?",
                    (changes["normalized_path"], root_id),
                ).fetchone()
                if dup is not None:
                    raise CoreError(ErrorCode.CONFLICT, "Directory is already registered", root_id=dup["root_id"])
            assignments = ", ".join(f"{column}=?" for column in changes)
            connection.execute(
                f"UPDATE source_roots SET {assignments} WHERE root_id=?",
                (*changes.values(), root_id),
            )
            row = connection.execute("SELECT * FROM source_roots WHERE root_id=?", (root_id,)).fetchone()
        return self._row(row)

    def remove(self, root_id: str) -> dict:
        require_id(root_id, "root")
        with self.store.transaction() as connection:
            cursor = connection.execute("DELETE FROM source_roots WHERE root_id=?", (root_id,))
            if cursor.rowcount != 1:
                raise CoreError(ErrorCode.NOT_FOUND, "Managed directory not found", root_id=root_id)
        return {"root_id": root_id, "removed": True, "imported_sources_preserved": True}

    def _load_existing_origins(self) -> dict[str, dict]:
        with closing(self.store.connect()) as connection:
            rows = connection.execute(
                """SELECT m.origin_key, s.source_id, s.title, s.archived,
                          v.version_id, v.sequence, v.sha256, v.size, v.original_name
                   FROM origin_mappings m
                   JOIN sources s ON s.source_id = m.source_id
                   JOIN source_versions v ON v.version_id = s.current_version_id"""
            ).fetchall()
            attachments_by_version: dict[str, list[tuple[str, str | None, int | None]]] = {}
            for att in connection.execute(
                "SELECT version_id, relative_path, sha256, size FROM source_attachments ORDER BY relative_path"
            ):
                attachments_by_version.setdefault(att["version_id"], []).append(
                    (att["relative_path"], att["sha256"], att["size"])
                )
        result = {}
        for row in rows:
            item = dict(row)
            item["attachments"] = attachments_by_version.get(row["version_id"], [])
            result[row["origin_key"]] = item
        return result

    def _iter_root_files(self, root_path: Path, *, recursive: bool, suffixes: set[str]):
        data_dir = self.store.data_dir.resolve()
        if not recursive:
            try:
                with os.scandir(root_path) as it:
                    for entry in sorted(it, key=lambda e: e.name.casefold()):
                        try:
                            if not entry.is_file(follow_symlinks=False):
                                continue
                        except OSError:
                            continue
                        if _is_temp_file(entry.name):
                            continue
                        file_path = Path(entry.path).resolve()
                        if file_path.suffix.casefold() in suffixes:
                            yield file_path
            except OSError:
                return
            return

        for current_dir, dirnames, filenames in os.walk(root_path, topdown=True):
            current_path = Path(current_dir).resolve()
            kept_dirs = []
            for name in sorted(dirnames, key=str.casefold):
                if name in SKIP_DIR_NAMES or name.startswith("."):
                    continue
                child = (current_path / name).resolve()
                if child == data_dir or child.is_relative_to(data_dir):
                    continue
                kept_dirs.append(name)
            dirnames[:] = kept_dirs
            for name in sorted(filenames, key=str.casefold):
                if _is_temp_file(name):
                    continue
                file_path = (current_path / name).resolve()
                if file_path.suffix.casefold() in suffixes:
                    yield file_path

    def scan(self, root_id: str | None = None, *, include_unchanged: bool = False) -> dict:
        if type(include_unchanged) is not bool:
            raise CoreError(ErrorCode.INVALID_ARGUMENT, "include_unchanged must be a boolean")
        roots = [self.get(root_id)] if root_id is not None else self.list(enabled_only=True)
        existing_map = self._load_existing_origins()
        counts = {"new": 0, "changed": 0, "unchanged": 0, "missing": 0, "errors": 0}
        root_reports = []
        items = []

        for root in roots:
            root_path = Path(root.path)
            if not root_path.is_dir():
                root_reports.append({
                    "root": root,
                    "exists": False,
                    "counts": {"new": 0, "changed": 0, "unchanged": 0, "missing": 0, "errors": 1},
                    "error": "Directory does not exist on disk",
                })
                counts["errors"] += 1
                continue

            resolved_root = root_path.resolve()
            root_uri = resolved_root.as_uri().rstrip("/") + "/"
            suffix_set = {s.casefold() for s in root.suffixes}
            seen_origins: set[str] = set()
            r_counts = {"new": 0, "changed": 0, "unchanged": 0, "missing": 0, "errors": 0}

            for file_path in self._iter_root_files(resolved_root, recursive=root.recursive, suffixes=suffix_set):
                origin_key = file_path.as_uri()
                seen_origins.add(origin_key)
                relative = str(file_path.relative_to(resolved_root))
                existing = existing_map.get(origin_key)
                try:
                    digest, size = _file_digest(file_path)
                except OSError as error:
                    r_counts["errors"] += 1
                    counts["errors"] += 1
                    items.append({
                        "root_id": root.root_id,
                        "root_name": root.name,
                        "path": str(file_path),
                        "relative_path": relative,
                        "status": "error",
                        "error": str(error),
                    })
                    continue

                if existing is None:
                    state = "new"
                else:
                    changed = existing["sha256"] != digest or existing["size"] != size
                    if not changed and file_path.suffix.casefold() == ".md":
                        changed = existing["attachments"] != _markdown_attachment_signature(file_path)
                    state = "changed" if changed else "unchanged"

                r_counts[state] += 1
                counts[state] += 1
                if state != "unchanged" or include_unchanged:
                    entry = {
                        "root_id": root.root_id,
                        "root_name": root.name,
                        "path": str(file_path),
                        "relative_path": relative,
                        "name": file_path.name,
                        "suffix": file_path.suffix.casefold(),
                        "size": size,
                        "status": state,
                    }
                    if existing is not None:
                        entry["source_id"] = existing["source_id"]
                        entry["version_id"] = existing["version_id"]
                        entry["sequence"] = existing["sequence"]
                        entry["archived"] = bool(existing["archived"])
                    items.append(entry)

            # Detect previously imported files under this root that are no longer present.
            for origin_key, existing in existing_map.items():
                if origin_key in seen_origins or existing["archived"]:
                    continue
                if not origin_key.startswith(root_uri):
                    continue
                tail = unquote(origin_key[len(root_uri):])
                if not root.recursive and "/" in tail:
                    continue
                r_counts["missing"] += 1
                counts["missing"] += 1
                items.append({
                    "root_id": root.root_id,
                    "root_name": root.name,
                    "path": str(resolved_root / Path(tail)),
                    "relative_path": tail,
                    "name": existing["original_name"],
                    "source_id": existing["source_id"],
                    "version_id": existing["version_id"],
                    "status": "missing",
                })

            root_reports.append({"root": root, "exists": True, "counts": r_counts})

        return {"roots": root_reports, "counts": counts, "items": items}

    def sync(
        self,
        root_id: str | None = None,
        *,
        paths: list[str] | None = None,
        auto_index: bool = True,
        ocr: bool = False,
    ) -> dict:
        if type(auto_index) is not bool or type(ocr) is not bool:
            raise CoreError(ErrorCode.INVALID_ARGUMENT, "auto_index and ocr must be booleans")
        selected: set[str] | None = None
        if paths is not None:
            if not isinstance(paths, list) or not all(isinstance(p, str) and p.strip() for p in paths):
                raise CoreError(ErrorCode.INVALID_ARGUMENT, "paths must be a list of file paths")
            selected = {_norm_key(Path(p).expanduser()) for p in paths}

        scan_report = self.scan(root_id=root_id, include_unchanged=False)
        imported, updated, indexed, skipped, errors = 0, 0, 0, 0, 0
        synced_root_ids: set[str] = set()
        results = []

        for entry in scan_report["items"]:
            if entry["status"] not in ("new", "changed"):
                continue
            file_path = Path(entry["path"])
            if selected is not None and _norm_key(file_path) not in selected:
                skipped += 1
                continue
            try:
                outcome = self.sources.import_file(file_path)
                synced_root_ids.add(entry["root_id"])
                if entry["status"] == "new":
                    imported += 1
                elif outcome.changed:
                    updated += 1
                else:
                    skipped += 1
                index_info = None
                if auto_index and self.knowledge is not None and (outcome.changed or entry["status"] == "new"):
                    try:
                        idx = self.knowledge.index_version(
                            outcome.source.source_id, outcome.version.version_id, ocr=ocr
                        )
                        index_info = {"coverage": idx.coverage, "warnings": idx.warnings}
                        if idx.coverage in ("success", "partial"):
                            indexed += 1
                    except CoreError as idx_error:
                        index_info = {"coverage": "failed", "error": idx_error.to_dict()}
                results.append({
                    "root_id": entry["root_id"],
                    "path": entry["path"],
                    "relative_path": entry["relative_path"],
                    "action": "imported" if entry["status"] == "new" else ("updated" if outcome.changed else "unchanged"),
                    "source_id": outcome.source.source_id,
                    "version_id": outcome.version.version_id,
                    "sequence": outcome.version.sequence,
                    "index": index_info,
                })
            except (CoreError, OSError) as error:
                errors += 1
                results.append({
                    "root_id": entry["root_id"],
                    "path": entry["path"],
                    "relative_path": entry["relative_path"],
                    "action": "error",
                    "error": error.to_dict() if isinstance(error, CoreError) else {"code": "integrity_error", "message": str(error)},
                })

        # Update last_synced_at for all scanned existing roots (even if 0 files changed).
        for report in scan_report["roots"]:
            if report["exists"]:
                synced_root_ids.add(report["root"].root_id)
        if synced_root_ids:
            timestamp = now_iso()
            with self.store.transaction() as connection:
                for rid in sorted(synced_root_ids):
                    connection.execute(
                        "UPDATE source_roots SET last_synced_at=?, updated_at=? WHERE root_id=?",
                        (timestamp, timestamp, rid),
                    )

        return {
            "imported": imported,
            "updated": updated,
            "indexed": indexed,
            "skipped": skipped,
            "errors": errors,
            "missing_on_disk": scan_report["counts"]["missing"],
            "results": results,
        }
