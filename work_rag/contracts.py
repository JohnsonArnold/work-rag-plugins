"""Versioned, JSON-compatible contracts; no parser or transport dependencies."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime
from enum import Enum
import json
import re
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

CONTRACT_VERSION = "1.0"
BEIJING = ZoneInfo("Asia/Shanghai")


class ErrorCode(str, Enum):
    INVALID_ARGUMENT = "invalid_argument"
    NOT_FOUND = "not_found"
    VERSION_NOT_FOUND = "version_not_found"
    INVALID_RANGE = "invalid_range"
    NOT_PROCESSED = "not_processed"
    UNSUPPORTED = "unsupported"
    AMBIGUOUS = "ambiguous"
    CONFLICT = "conflict"
    SOURCE_CHANGED = "source_changed"
    INTEGRITY = "integrity_error"


class CoreError(Exception):
    def __init__(self, code: ErrorCode, message: str, **details: Any):
        super().__init__(message)
        self.code, self.details = code, details

    def to_dict(self) -> dict:
        return {"code": self.code.value, "message": str(self), "details": self.details}


def new_id(kind: str) -> str:
    if kind not in {"src", "ver", "task", "artifact", "evidence", "log", "review"}:
        raise ValueError("Unknown ID kind")
    return f"{kind}_{uuid4().hex}"


def require_id(value: str, kind: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(rf"{kind}_[0-9a-f]{{32}}", value):
        raise CoreError(ErrorCode.INVALID_ARGUMENT, f"Invalid {kind} ID")
    return value


def now_iso() -> str:
    return datetime.now(BEIJING).isoformat(timespec="microseconds")


def jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return jsonable(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class Locator:
    """External pages are 1-based; text slices are [start,end) Unicode chars.

    XLSX uses sheet + A1 range; PDF uses page + optional (x0,y0,x1,y1)
    in unrotated top-left points; text offsets require an immutable artifact ID.
    Additional formats can add explicitly versioned locator kinds later.
    """
    kind: str
    sheet: str | None = None
    cell_range: str | None = None
    page: int | None = None
    rect: tuple[float, float, float, float] | None = None
    artifact_id: str | None = None
    start: int | None = None
    end: int | None = None

    def __post_init__(self):
        invalid = False
        if self.kind == "xlsx":
            invalid = (not isinstance(self.sheet, str) or not self.sheet.strip()
                       or not isinstance(self.cell_range, str) or not self.cell_range
                       or any(v is not None for v in (self.page, self.rect, self.artifact_id, self.start, self.end)))
            if not invalid:
                match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]{0,6}):([A-Z]{1,3})([1-9][0-9]{0,6})", self.cell_range)
                if not match:
                    invalid = True
                else:
                    c1, r1, c2, r2 = match.groups()
                    column = lambda s: sum((ord(c) - 64) * 26 ** i for i, c in enumerate(reversed(s)))
                    invalid = not (column(c1) <= column(c2) <= 16384 and int(r1) <= int(r2) <= 1048576)
        elif self.kind == "pdf":
            invalid = type(self.page) is not int or self.page < 1 or any(v is not None for v in (self.sheet, self.cell_range, self.artifact_id, self.start, self.end))
            if self.rect is not None:
                import math
                invalid = (invalid or not isinstance(self.rect, (tuple, list)) or len(self.rect) != 4
                           or any(type(n) not in (int, float) or not math.isfinite(n) for n in self.rect))
                if not invalid:
                    x0, y0, x1, y1 = self.rect
                    invalid = not (0 <= x0 < x1 and 0 <= y0 < y1)
        elif self.kind == "text":
            require_id(self.artifact_id, "artifact")
            invalid = type(self.start) is not int or type(self.end) is not int
            if not invalid:
                invalid = not (0 <= self.start <= self.end)
            invalid = invalid or any(v is not None for v in (self.sheet, self.cell_range, self.page, self.rect))
        else:
            raise CoreError(ErrorCode.UNSUPPORTED, "Unsupported locator kind", kind=self.kind)
        if invalid:
            raise CoreError(ErrorCode.INVALID_RANGE, "Invalid locator bounds or fields")


@dataclass(frozen=True)
class Source:
    source_id: str
    title: str
    current_version_id: str
    created_at: str


@dataclass(frozen=True)
class SourceVersion:
    source_id: str
    version_id: str
    sequence: int
    sha256: str
    size: int
    original_name: str
    created_at: str


@dataclass(frozen=True)
class ImportResult:
    source: Source
    version: SourceVersion
    changed: bool


@dataclass(frozen=True)
class Task:
    task_id: str
    title: str
    content: str
    status: str
    due: str | None
    due_kind: str | None
    tags: list[str]
    revision: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class EvidenceRef:
    source_id: str
    version_id: str
    sha256: str
    locator: Locator
    method: str = "original"
    artifact_id: str | None = None

    def __post_init__(self):
        if not isinstance(self.locator, Locator):
            raise CoreError(ErrorCode.INVALID_ARGUMENT, "Evidence requires a typed locator")
        if self.locator.kind == "text" and self.locator.artifact_id != self.artifact_id:
            raise CoreError(ErrorCode.INVALID_ARGUMENT, "Evidence and text locator must use the same artifact")
        require_id(self.source_id, "src")
        require_id(self.version_id, "ver")
        if not isinstance(self.sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise CoreError(ErrorCode.INVALID_ARGUMENT, "Invalid SHA-256")
        if self.method != "original" and self.artifact_id is None:
            raise CoreError(ErrorCode.INVALID_ARGUMENT, "Derived evidence requires artifact ID")
        if self.artifact_id is not None:
            require_id(self.artifact_id, "artifact")


@dataclass(frozen=True)
class Result:
    data: Any
    evidence: list[EvidenceRef] = field(default_factory=list)
    coverage: str | None = None
    warnings: list[str] = field(default_factory=list)
    next_cursor: str | None = None
    contract_version: str = CONTRACT_VERSION

    def __post_init__(self):
        if self.coverage is not None and (not isinstance(self.coverage, str) or self.coverage not in {"not_processed", "partial", "success", "failed", "unsupported"}):
            raise CoreError(ErrorCode.INVALID_ARGUMENT, "Unknown coverage state")

    def to_dict(self) -> dict:
        return jsonable(self)
