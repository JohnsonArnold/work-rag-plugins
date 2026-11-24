"""JSON management commands; no business rules or SQL live here."""
from __future__ import annotations

import argparse
from io import TextIOWrapper
import json
import os
from pathlib import Path
import sqlite3
import sys

from . import __version__
from .application import Application
from .backup import copy_library
from .electricity import analyze, total_periods
from .storage import SCHEMA_VERSION, Store
from .contracts import CONTRACT_VERSION, CoreError, ErrorCode, Result


def _json_input(value):
    try:
        return json.loads(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Expected JSON") from error


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="work-rag", description="个人资料库与事项核心")
    root.add_argument("--version", action="version", version=__version__)
    default_dir = ".work-rag-data"
    if not Path(default_dir).exists() and Path(".it-rag-data").is_dir():
        default_dir = ".it-rag-data"
    data_dir = os.environ.get("WORK_RAG_DATA_DIR") or os.environ.get("IT_RAG_DATA_DIR") or default_dir
    root.add_argument("--data-dir", default=data_dir, help="共享数据目录；所有命令/客户端须使用同一路径")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="初始化或迁移数据目录")
    commands.add_parser("status", help="检查数据库、已引用快照及中断遗留文件（不删除）")
    sources = commands.add_parser("source", help="指定文件与不可变版本").add_subparsers(dest="action", required=True)
    imp = sources.add_parser("import", help="指定导入文件及 Markdown 中的本地相对附件")
    imp.add_argument("path")
    imp.add_argument("--source-id")
    imp.add_argument("--origin-key")
    imp.add_argument("--title")
    imp.add_argument("--request-id")
    imp.add_argument("--expected-current-version")
    sources.add_parser("list")
    show = sources.add_parser("show", help="元数据、固定版本和本机快照路径；不是已解析正文")
    show.add_argument("source_id")
    show.add_argument("--version-id")
    versions = sources.add_parser("versions")
    versions.add_argument("source_id")
    tasks = commands.add_parser("task", help="事项基本操作").add_subparsers(dest="action", required=True)
    create = tasks.add_parser("create")
    create.add_argument("--title", required=True)
    create.add_argument("--content", required=True)
    create.add_argument("--due", help="YYYY-MM-DD 或有时区的 ISO datetime")
    create.add_argument("--tag", action="append", default=[])
    create.add_argument("--request-id")
    get = tasks.add_parser("get")
    get.add_argument("task_id")
    listing = tasks.add_parser("list")
    listing.add_argument("--status", choices=["pending", "in_progress", "completed", "cancelled"])
    listing.add_argument("--view", choices=["unfinished", "today", "overdue", "recent"])
    listing.add_argument("--query")
    listing.add_argument("--limit", type=int, default=100)
    listing.add_argument("--offset", type=int, default=0)
    update = tasks.add_parser("update")
    update.add_argument("task_id")
    update.add_argument("--expected-revision", required=True, type=int)
    update.add_argument("--title")
    update.add_argument("--content")
    update.add_argument("--status", choices=["pending", "in_progress", "completed", "cancelled"])
    due = update.add_mutually_exclusive_group()
    due.add_argument("--due")
    due.add_argument("--clear-due", action="store_true")
    tags = update.add_mutually_exclusive_group()
    tags.add_argument("--tag", action="append")
    tags.add_argument("--clear-tags", action="store_true")
    update.add_argument("--request-id")
    catalog = sources.add_parser("catalog")
    catalog.add_argument("--query", default="")
    catalog.add_argument("--suffix")
    catalog.add_argument("--limit", type=int, default=50)
    catalog.add_argument("--include-archived", action="store_true")
    edit = sources.add_parser("edit")
    edit.add_argument("source_id")
    edit.add_argument("--title")
    edit.add_argument("--archived", action=argparse.BooleanOptionalAction)
    logs = tasks.add_parser("logs")
    logs.add_argument("task_id")
    logs.add_argument("--after", type=int, default=0)
    logs.add_argument("--limit", type=int, default=100)
    append = tasks.add_parser("append-log")
    append.add_argument("task_id")
    append.add_argument("--content", required=True)
    append.add_argument("--expected-revision", required=True, type=int)
    append.add_argument("--corrects")
    append.add_argument("--request-id")
    reviews = commands.add_parser("review").add_subparsers(dest="action", required=True)
    new_review = reviews.add_parser("create")
    new_review.add_argument("task_ids", nargs="+")
    new_review.add_argument("--sections", required=True, type=_json_input)
    new_review.add_argument("--request-id")
    get_review = reviews.add_parser("get")
    get_review.add_argument("review_id")
    list_reviews = reviews.add_parser("list")
    list_reviews.add_argument("--limit", type=int, default=100)
    kb = commands.add_parser("kb").add_subparsers(dest="action", required=True)
    for name in ("describe", "status", "render-attachment", "read-sheet", "read-text", "calculate", "electricity", "electricity-total", "ocr", "render"):
        action = kb.add_parser(name)
        action.add_argument("source_id")
        action.add_argument("--version-id")
        if name == "render-attachment":
            action.add_argument("name")
        if name in ("read-sheet", "calculate", "electricity", "electricity-total"):
            action.add_argument("sheet")
        if name in ("read-sheet", "calculate"):
            action.add_argument("cell_range")
        if name in ("read-text", "ocr", "render"):
            action.add_argument("--page", type=int)
        if name in ("ocr", "render"):
            action.add_argument("--rect", nargs=4, type=float)
        if name == "read-sheet":
            action.add_argument("--limit", type=int, default=100)
            action.add_argument("--cursor")
        if name == "read-text":
            action.add_argument("--limit", type=int, default=4000)
            action.add_argument("--start", type=int, default=0)
        if name == "calculate":
            action.add_argument("operation", choices=["sum", "difference", "count", "average"])
            action.add_argument("--unit")
        if name in ("electricity", "electricity-total"):
            if name == "electricity":
                action.add_argument("row", type=int)
            else:
                action.add_argument("rows", type=int, nargs="+")
            action.add_argument("--category")
            action.add_argument("--year", type=int)
    search = kb.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=30)
    search.add_argument("--cursor")
    resolve = kb.add_parser("resolve")
    resolve.add_argument("reference", type=_json_input)
    index_cmd = kb.add_parser("index", help="按需解析并建立指定版本的全文检索索引")
    index_cmd.add_argument("source_id")
    index_cmd.add_argument("--version-id")
    index_cmd.add_argument("--ocr", action="store_true")
    roots = commands.add_parser("root", help="受管本地资料源目录配置与同步").add_subparsers(dest="action", required=True)
    root_add = roots.add_parser("add")
    root_add.add_argument("path")
    root_add.add_argument("--name")
    root_add.add_argument("--recursive", action=argparse.BooleanOptionalAction, default=True)
    root_add.add_argument("--suffix", action="append")
    root_add.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=True)
    root_list = roots.add_parser("list")
    root_list.add_argument("--enabled-only", action="store_true")
    root_get = roots.add_parser("get")
    root_get.add_argument("root_id")
    root_update = roots.add_parser("update")
    root_update.add_argument("root_id")
    root_update.add_argument("--name")
    root_update.add_argument("--path")
    root_update.add_argument("--recursive", action=argparse.BooleanOptionalAction, default=None)
    root_update.add_argument("--suffix", action="append")
    root_update.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=None)
    root_remove = roots.add_parser("remove")
    root_remove.add_argument("root_id")
    root_scan = roots.add_parser("scan")
    root_scan.add_argument("root_id", nargs="?")
    root_scan.add_argument("--include-unchanged", action="store_true")
    root_sync = roots.add_parser("sync")
    root_sync.add_argument("root_id", nargs="?")
    root_sync.add_argument("--no-index", action="store_true")
    root_sync.add_argument("--ocr", action="store_true")
    backup = commands.add_parser("backup")
    backup.add_argument("destination")
    restore = commands.add_parser("restore")
    restore.add_argument("backup")
    mcp_cmd = commands.add_parser("mcp", help="本地 MCP stdio 服务")
    mcp_cmd.add_argument("--enable-app", action="store_true", help="启用 Antigravity MCP App 交互面板与 UI 专用工具")
    return root


def execute(args: argparse.Namespace, app: Application) -> Result:
    if args.command == "init":
        return Result({"data_dir": str(app.store.data_dir), "schema_version": SCHEMA_VERSION})
    if args.command == "status":
        return Result(app.store.recovery_report(), warnings=["快照可用、已解析和已索引是独立状态。"])
    if args.command == "source":
        if args.action == "import":
            return Result(app.sources.import_file(args.path, source_id=args.source_id, origin_key=args.origin_key,
                          title=args.title, request_id=args.request_id, expected_current_version=args.expected_current_version),
                          coverage="not_processed", warnings=["保存指定文件快照及支持的 Markdown 附件；正文按需解析。"])
        if args.action == "list":
            return Result(app.sources.list())
        if args.action == "versions":
            return Result(app.sources.versions(args.source_id))
        if args.action == "catalog":
            return Result(app.sources.catalog(args.query, args.suffix, args.limit, include_archived=args.include_archived))
        if args.action == "edit":
            return Result(app.sources.edit(args.source_id, title=args.title, archived=args.archived))
        return app.show_source(args.source_id, args.version_id, local_path=True)
    if args.command == "backup":
        return Result(copy_library(app.store, args.destination))
    if args.command == "review":
        if args.action == "create":
            return Result(app.journal.create_review(args.task_ids, args.sections, request_id=args.request_id))
        if args.action == "get":
            return Result(app.journal.review(args.review_id))
        return Result(app.journal.reviews(args.limit))
    if args.command == "kb":
        values = {key: value for key, value in vars(args).items() if key not in {"command", "action", "data_dir"}}
        if args.action == "index":
            return app.knowledge.index_version(**values)
        if args.action == "electricity":
            return analyze(app.knowledge, **values)
        if args.action == "electricity-total":
            return total_periods(app.knowledge, **values)
        if args.action in ("render", "render-attachment"):
            import base64
            render = app.knowledge.render if args.action == "render" else app.knowledge.render_attachment
            image, result = render(**values)
            return Result({**result.data, "png_base64": base64.b64encode(image).decode()},
                          evidence=result.evidence, coverage=result.coverage)
        handler = getattr(app.knowledge, args.action.replace("-", "_"))
        return handler(**values)
    if args.command == "root":
        if args.action == "add":
            return Result(app.roots.add(args.path, name=args.name, recursive=args.recursive,
                                        suffixes=args.suffix, enabled=args.enabled))
        if args.action == "list":
            return Result(app.roots.list(enabled_only=args.enabled_only))
        if args.action == "get":
            return Result(app.roots.get(args.root_id))
        if args.action == "update":
            return Result(app.roots.update(args.root_id, name=args.name, path=args.path,
                                           recursive=args.recursive, suffixes=args.suffix, enabled=args.enabled))
        if args.action == "remove":
            return Result(app.roots.remove(args.root_id))
        if args.action == "scan":
            return Result(app.roots.scan(args.root_id, include_unchanged=args.include_unchanged))
        return Result(app.roots.sync(args.root_id, auto_index=not args.no_index, ocr=args.ocr))
    if args.action == "append-log":
        return Result(app.journal.append(args.task_id, args.content, args.expected_revision,
                                         corrects=args.corrects, request_id=args.request_id))
    if args.action == "logs":
        return Result(app.journal.logs(args.task_id, after=args.after, limit=args.limit))
    if args.action == "create":
        return Result(app.tasks.create(args.title, args.content, due=args.due, tags=args.tag, request_id=args.request_id))
    if args.action == "get":
        return Result(app.tasks.get(args.task_id))
    if args.action == "list":
        return app.list_tasks(status=args.status, view=args.view, query=args.query, limit=args.limit, offset=args.offset)
    changes = {key: getattr(args, key) for key in ("title", "content", "status", "due") if getattr(args, key) is not None}
    if args.clear_due:
        changes["due"] = None
    if args.tag is not None or args.clear_tags:
        changes["tags"] = [] if args.clear_tags else args.tag
    return Result(app.tasks.update(args.task_id, args.expected_revision, request_id=args.request_id, **changes))


def main(argv: list[str] | None = None) -> int:
    if os.name == "nt":
        # Windows pipes may default to a legacy code page. Keep Chinese CLI
        # output and startup errors readable when a host captures the streams.
        for stream in (sys.stdout, sys.stderr):
            if isinstance(stream, TextIOWrapper):
                stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        if args.command == "mcp":
            from .mcp_server import run
            enable_app = bool(args.enable_app or os.environ.get("WORK_RAG_MCP_APP") == "1")
            run(Application(args.data_dir), enable_app=enable_app)
            return 0
        if args.command == "restore":
            from pathlib import Path
            source = Path(args.backup).expanduser()
            if not (source / "core.sqlite3").is_file():
                raise CoreError(ErrorCode.NOT_FOUND, "Backup database does not exist")
            result = Result(copy_library(Store(source), args.data_dir))
        else:
            result = execute(args, Application(args.data_dir))
        print(json.dumps(result.to_dict(), ensure_ascii=False, allow_nan=False))
        return 0
    except CoreError as error:
        print(json.dumps({"contract_version": CONTRACT_VERSION, "error": error.to_dict()}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as error:
        code = ErrorCode.CONFLICT if isinstance(error, sqlite3.OperationalError) and "locked" in str(error) else ErrorCode.INTEGRITY
        print(json.dumps({"contract_version": CONTRACT_VERSION, "error": {"code": code.value, "message": str(error), "details": {}}}, ensure_ascii=False), file=sys.stderr)
        return 3
