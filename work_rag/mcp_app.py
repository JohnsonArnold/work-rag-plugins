"""Antigravity MCP App resource and app-only tools (opt-in via --enable-app)."""
from __future__ import annotations

from functools import wraps
import os
from pathlib import Path
import sqlite3
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from .contracts import CONTRACT_VERSION, CoreError, Result, canonical_json
from .roots import DEFAULT_SUFFIXES, browse_directories
from .storage import SCHEMA_VERSION

APP_URI = "ui://work-rag/panel.html"
APP_MIME_TYPE = "text/html;profile=mcp-app"

PANE_META: dict[str, Any] = {
    "ui": {"resourceUri": APP_URI},
    "com.google.antigravity/ui": {
        "entrypoints": [
            {"type": "aux_pane", "title": "Work-RAG"},
            {"type": "global", "title": "Work-RAG"},
        ]
    },
}

APP_ONLY_META: dict[str, Any] = {
    "ui": {"visibility": ["app"]},
}

_FALLBACK_HTML = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>Work-RAG</title></head>
<body style="font-family:system-ui,sans-serif;padding:16px">
  <h2>Work-RAG 面板尚未构建</h2>
  <p>请先运行前端构建脚本生成 <code>work_rag/assets/panel.html</code>，或设置 <code>WORK_RAG_APP_HTML</code>。</p>
</body>
</html>"""


def load_panel_html() -> str:
    override = os.environ.get("WORK_RAG_APP_HTML")
    candidates = []
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(Path(__file__).resolve().parent / "assets" / "panel.html")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    return _FALLBACK_HTML


def _overview_payload(app) -> dict[str, Any]:
    roots = app.roots.list()
    sources = app.sources.catalog("", None, 200, include_archived=True)
    active_sources = sum(1 for item in sources if not item["archived"])
    unfinished_tasks = app.tasks.list(view="unfinished", limit=200)
    reviews = app.journal.reviews(limit=200)
    return {
        "data_dir": str(app.store.data_dir),
        "schema_version": SCHEMA_VERSION,
        "default_suffixes": list(DEFAULT_SUFFIXES),
        "counts": {
            "roots": len(roots),
            "enabled_roots": sum(1 for r in roots if r.enabled),
            "sources": active_sources,
            "archived_sources": len(sources) - active_sources,
            "unfinished_tasks": len(unfinished_tasks),
            "reviews": len(reviews),
        },
    }


def register_mcp_app(mcp, app) -> None:
    """Register the MCP App HTML resource, panel entrypoint, and app-only tools."""

    def app_tool(*, read_only=True, idempotent=True, meta=None, name=None, title=None):
        def register(function):
            @wraps(function)
            def wrapped(*args, **kwargs):
                try:
                    result = function(*args, **kwargs)
                    return result.to_dict() if isinstance(result, Result) else result
                except CoreError as error:
                    raise ToolError(canonical_json({
                        "contract_version": CONTRACT_VERSION,
                        "error": error.to_dict(),
                    })) from None
                except (OSError, sqlite3.Error):
                    raise ToolError("Local library storage error") from None

            mcp.tool(
                name=name,
                title=title,
                annotations=ToolAnnotations(
                    readOnlyHint=read_only,
                    destructiveHint=False,
                    idempotentHint=idempotent,
                    openWorldHint=False,
                ),
                meta=meta if meta is not None else APP_ONLY_META,
            )(wrapped)
            return wrapped
        return register

    @mcp.resource(
        APP_URI,
        name="Work-RAG Panel",
        title="Work-RAG 资料目录与事项面板",
        description="Interactive configuration panel for managed source directories, library, tasks, and reviews.",
        mime_type=APP_MIME_TYPE,
        meta={"ui": {"prefersBorder": False}},
    )
    def panel_html() -> str:
        return load_panel_html()

    @app_tool(name="work_rag_panel", title="Work-RAG", meta=PANE_META)
    def work_rag_panel(tab: str = "roots") -> dict[str, Any]:
        """Open the Work-RAG panel to manage source folders, library index, tasks, and reviews."""
        selected_tab = tab if tab in {"roots", "library", "tasks", "reviews"} else "roots"
        return Result({"tab": selected_tab, **_overview_payload(app)})

    # --- App-only overview & directory picker ---

    @app_tool()
    def app_overview() -> dict[str, Any]:
        """Return library path and summary counts for the Work-RAG UI."""
        return Result(_overview_payload(app))

    @app_tool()
    def app_browse_dirs(path: str | None = None) -> dict[str, Any]:
        """Browse local drives and immediate subdirectories for folder registration."""
        return Result(browse_directories(path))

    # --- App-only managed source roots ---

    @app_tool()
    def app_roots_list() -> dict[str, Any]:
        """List all registered local working directories."""
        return Result(app.roots.list())

    @app_tool(read_only=False, idempotent=False)
    def app_root_add(
        path: str,
        name: str | None = None,
        recursive: bool = True,
        suffixes: list[str] | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        """Register a local working directory for diff scanning and explicit sync."""
        return Result(app.roots.add(path, name=name, recursive=recursive, suffixes=suffixes, enabled=enabled))

    @app_tool(read_only=False)
    def app_root_update(
        root_id: str,
        name: str | None = None,
        path: str | None = None,
        recursive: bool | None = None,
        suffixes: list[str] | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        """Update managed directory settings."""
        return Result(app.roots.update(
            root_id, name=name, path=path, recursive=recursive, suffixes=suffixes, enabled=enabled
        ))

    @app_tool(read_only=False)
    def app_root_remove(root_id: str) -> dict[str, Any]:
        """Remove a directory registration without deleting imported source snapshots."""
        return Result(app.roots.remove(root_id))

    @app_tool()
    def app_root_scan(root_id: str | None = None, include_unchanged: bool = False) -> dict[str, Any]:
        """Scan one or all enabled directories and classify new, changed, and missing files."""
        return Result(app.roots.scan(root_id=root_id, include_unchanged=include_unchanged))

    @app_tool(read_only=False, idempotent=False)
    def app_root_sync(
        root_id: str | None = None,
        paths: list[str] | None = None,
        auto_index: bool = True,
        ocr: bool = False,
    ) -> dict[str, Any]:
        """Import new and changed files from managed directories into immutable snapshots."""
        return Result(app.roots.sync(root_id=root_id, paths=paths, auto_index=auto_index, ocr=ocr))

    # --- App-only library catalog & indexing ---

    @app_tool()
    def app_sources_catalog(
        query: str = "",
        suffix: str | None = None,
        limit: int = 100,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        """List library sources with archive filter for the UI."""
        return Result(app.sources.catalog(query, suffix, limit, include_archived=include_archived))

    @app_tool()
    def app_source_versions(source_id: str) -> dict[str, Any]:
        """List immutable historical versions of a source."""
        return Result(app.sources.versions(source_id))

    @app_tool(read_only=False)
    def app_source_edit(
        source_id: str,
        title: str | None = None,
        archived: bool | None = None,
    ) -> dict[str, Any]:
        """Rename or archive/unarchive a source."""
        return Result(app.sources.edit(source_id, title=title, archived=archived))

    @app_tool()
    def app_source_status(source_id: str, version_id: str | None = None) -> dict[str, Any]:
        """Inspect parsed artifacts, indexed entries, and processing failures for a source."""
        return app.knowledge.status(source_id, version_id)

    @app_tool(read_only=False)
    def app_source_index(
        source_id: str,
        version_id: str | None = None,
        ocr: bool = False,
    ) -> dict[str, Any]:
        """Parse and index a source version on demand."""
        return app.knowledge.index_version(source_id, version_id, ocr=ocr)

    # --- App-only direct task & review operations (no model call) ---

    @app_tool()
    def app_tasks_list(
        view: str | None = None,
        status: str | None = None,
        query: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List tasks directly for the UI."""
        return app.list_tasks(status=status, view=view, query=query, limit=limit, offset=offset)

    @app_tool()
    def app_task_get(task_id: str) -> dict[str, Any]:
        """Read one task directly for the UI."""
        return Result(app.tasks.get(task_id))

    @app_tool(read_only=False)
    def app_task_create(
        title: str,
        content: str,
        request_id: str,
        due: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a task directly from the UI."""
        return Result(app.tasks.create(title, content, due=due, tags=tags, request_id=request_id))

    @app_tool(read_only=False)
    def app_task_update(
        task_id: str,
        expected_revision: int,
        changes: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        """Update a task directly from the UI with optimistic locking."""
        return Result(app.tasks.update(task_id, expected_revision, request_id=request_id, **changes))

    @app_tool(read_only=False)
    def app_task_append_log(
        task_id: str,
        content: str,
        expected_revision: int,
        request_id: str,
        corrects: str | None = None,
    ) -> dict[str, Any]:
        """Append a follow-up log directly from the UI."""
        return Result(app.journal.append(
            task_id, content, expected_revision, corrects=corrects, request_id=request_id
        ))

    @app_tool()
    def app_task_logs(task_id: str, after: int = 0, limit: int = 100) -> dict[str, Any]:
        """List follow-up logs of a task directly for the UI."""
        return Result(app.journal.logs(task_id, after=after, limit=limit))

    @app_tool()
    def app_reviews_list(limit: int = 100) -> dict[str, Any]:
        """List saved reviews directly for the UI."""
        return Result(app.journal.reviews(limit))

    @app_tool()
    def app_review_get(review_id: str) -> dict[str, Any]:
        """Read a saved review snapshot directly for the UI."""
        return Result(app.journal.review(review_id))
