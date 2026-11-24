"""One request per short process: local task operations, independent of MCP.

Wire protocol 1 wraps core contract 1.0. Writes intentionally return only a
bounded receipt, including when the core returns a historical replay result.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .application import Application
from .contracts import CoreError, ErrorCode, Result, canonical_json, jsonable
from .storage import library_lock

PROTOCOL_VERSION = "1"
MAX_STDIN_BYTES = 1024 * 1024
MAX_STDOUT_BYTES = 8 * 1024 * 1024
_WRITES = frozenset({"task.create", "task.update", "task.append_log"})
_FIELDS = {
    "task.create": ({"title", "content", "request_id"}, {"due", "tags"}),
    "task.get": ({"task_id"}, set()),
    "task.list": (set(), {"status", "view", "query", "limit", "offset"}),
    "task.update": ({"task_id", "expected_revision", "changes", "request_id"}, set()),
    "task.append_log": ({"task_id", "content", "expected_revision", "request_id"}, {"corrects"}),
    "task.logs": ({"task_id"}, {"after", "limit"}),
}


class BridgeError(Exception):
    def __init__(self, layer, code, message, **details):
        super().__init__(message)
        self.layer, self.code, self.details = layer, code, details


def _invalid(message, **details):
    raise BridgeError("validation", "invalid_argument", message, **details)


def _object(value, required, optional, field):
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        _invalid(f"Invalid {field} fields", field=field)


def _string(value, field, *, maximum=None, nullable=False):
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value.strip() or (maximum is not None and len(value) > maximum):
        _invalid(f"{field} must be a non-empty string" + (f" of at most {maximum} characters" if maximum else ""), field=field)


def _integer(value, field, minimum=0, maximum=2**63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        _invalid(f"{field} must be an integer between {minimum} and {maximum}", field=field)


def _task_fields(params):
    for name in ("title", "content", "status"):
        if name in params:
            _string(params[name], name)
    if "due" in params:
        _string(params["due"], "due", nullable=True)
    if "tags" in params:
        if not isinstance(params["tags"], list):
            _invalid("tags must be a list", field="tags")
        for tag in params["tags"]:
            _string(tag, "tags")


def _validate(request):
    _object(request, {"protocol_version", "call_id", "operation", "params"}, {"binding"}, "request")
    if request["protocol_version"] != PROTOCOL_VERSION:
        raise BridgeError("protocol", "unsupported", "Unsupported local bridge protocol")
    _string(request["call_id"], "call_id", maximum=200)
    operation = request["operation"]
    if not isinstance(operation, str) or operation not in _FIELDS:
        raise BridgeError("protocol", "unsupported", "Unsupported local bridge operation")
    required, optional = _FIELDS[operation]
    params = request["params"]
    _object(params, required, optional, "params")
    if operation in _WRITES and "binding" not in request:
        _invalid("Writes require a library binding", field="binding")
    if "binding" in request:
        _object(request["binding"], {"library_id", "restore_generation"}, set(), "binding")
        for field, value in request["binding"].items():
            _string(value, field, maximum=200)
    if "request_id" in params:
        _string(params["request_id"], "request_id", maximum=200)
    for field in ("task_id", "corrects"):
        if field in params:
            _string(params[field], field, nullable=field == "corrects")
    if "expected_revision" in params:
        _integer(params["expected_revision"], "expected_revision", 1)
    if "limit" in params:
        _integer(params["limit"], "limit", 1, 200)
    for field in ("offset", "after"):
        if field in params:
            _integer(params[field], field)
    if operation == "task.list":
        for field in ("status", "view", "query"):
            if field in params and params[field] is not None and not isinstance(params[field], str):
                _invalid(f"{field} must be a string", field=field)
    if operation == "task.update":
        changes = params["changes"]
        _object(changes, set(), {"title", "content", "status", "due", "tags"}, "changes")
        if not changes:
            _invalid("Provide at least one task change", field="changes")
        _task_fields(changes)
    elif operation != "task.list":
        _task_fields(params)
    # JSON escape sequences can encode lone surrogates even in valid UTF-8 input.
    try:
        canonical_json(request).encode("utf-8")
    except (UnicodeError, ValueError, RecursionError) as error:
        raise BridgeError("protocol", "invalid_argument", "Request is not valid UTF-8 JSON") from error
    return operation, params


def _receipt(value):
    value = jsonable(value)
    result = {"task_id": value["task_id"], "revision": value.get("revision", value.get("task_revision"))}
    for field in ("status", "log_id"):
        if field in value:
            result[field] = value[field]
    return result


def _result(data):
    result = data if isinstance(data, Result) else Result(data)
    return {"contract_version": result.contract_version, "data": jsonable(result.data),
            "warnings": result.warnings, "next_cursor": result.next_cursor}


def _error(call_id, error, binding=None):
    if isinstance(error, CoreError):
        value = {"layer": "core", **error.to_dict()}
    elif isinstance(error, BridgeError):
        value = {"layer": error.layer, "code": error.code, "message": str(error), "details": error.details}
    else:
        value = {"layer": "bridge", "code": "internal_error", "message": "Local bridge failed", "details": {}}
    response = {"protocol_version": PROTOCOL_VERSION, "call_id": call_id, "ok": False, "error": value}
    if binding is not None:
        response["binding"] = binding
    return response


def _bounded(response):
    if len(canonical_json(response).encode("utf-8")) + 1 > MAX_STDOUT_BYTES:
        return _error(response["call_id"], BridgeError(
            "bridge", "response_too_large", "Read result exceeds stdout limit; request a smaller page",
            maximum_bytes=MAX_STDOUT_BYTES), response.get("binding"))
    return response


def _call_id(request):
    value = request.get("call_id", "") if isinstance(request, dict) else ""
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        return ""
    try:
        value.encode("utf-8")
    except UnicodeError:
        return ""
    return value


def dispatch(app: Application, request: dict) -> dict:
    """Validate, pin the library generation, invoke core, and wrap its result."""
    call_id = _call_id(request)
    binding = None
    try:
        operation, params = _validate(request)
        with app.store.bound_to(request.get("binding")) as binding:
            if operation == "task.create":
                result = _receipt(app.tasks.create(**params))
            elif operation == "task.update":
                args = {key: value for key, value in params.items() if key != "changes"}
                result = _receipt(app.tasks.update(**args, **params["changes"]))
            elif operation == "task.append_log":
                result = _receipt(app.journal.append(**params))
            elif operation == "task.get":
                result = app.tasks.get(**params)
            elif operation == "task.list":
                result = app.list_tasks(**{"limit": 50, **params})
            else:
                result = app.journal.logs(**{"limit": 50, **params})
            return _bounded({"protocol_version": PROTOCOL_VERSION, "call_id": call_id,
                             "binding": binding, "ok": True, "result": _result(result)})
    except (CoreError, BridgeError) as error:
        return _error(call_id, error, binding)
    except (OSError, sqlite3.Error) as error:
        code = "conflict" if isinstance(error, sqlite3.OperationalError) and "locked" in str(error) else "integrity_error"
        return _error(call_id, BridgeError("bridge", code, "Library storage operation failed"), binding)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _parse(raw):
    if len(raw) > MAX_STDIN_BYTES:
        raise BridgeError("protocol", "input_too_large", "Request exceeds stdin limit", maximum_bytes=MAX_STDIN_BYTES)
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON number")))
    except (UnicodeError, ValueError, RecursionError) as error:
        raise BridgeError("protocol", "invalid_argument", "Expected one UTF-8 JSON object followed by EOF") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    if not args.data_dir.is_absolute():
        parser.error("--data-dir must be absolute")
    call_id = ""
    try:
        request = _parse(sys.stdin.buffer.read(MAX_STDIN_BYTES + 1))
        call_id = _call_id(request)
        _validate(request)  # Invalid requests must not initialize a library.
        with library_lock(args.data_dir):
            response = dispatch(Application(args.data_dir), request)
    except (CoreError, BridgeError) as error:
        response = _error(call_id, error)
    except Exception as error:
        # Always give a complete error envelope when the process is still able
        # to respond. Do not expose exception paths or payloads on stdout.
        print(f"Local bridge failure: {type(error).__name__}", file=sys.stderr)
        response = _error(call_id, error)
    sys.stdout.buffer.write((canonical_json(_bounded(response)) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
