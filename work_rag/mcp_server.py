"""Local stdio adapter for the shared application."""
from functools import wraps
import sqlite3
from typing import Any

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from .contracts import CONTRACT_VERSION, CoreError, Result, canonical_json
from .electricity import analyze, total_periods


def server(app):
    mcp = FastMCP("work-rag")

    def tool(*, read_only=True, idempotent=True):
        def register(function):
            @wraps(function)
            def wrapped(*args, **kwargs):
                try:
                    result = function(*args, **kwargs)
                    return result.to_dict() if isinstance(result, Result) else result
                except CoreError as error:
                    raise ToolError(canonical_json({"contract_version": CONTRACT_VERSION,
                                                    "error": error.to_dict()})) from None
                except (OSError, sqlite3.Error):
                    raise ToolError("Local library storage error") from None

            mcp.tool(annotations=ToolAnnotations(readOnlyHint=read_only, destructiveHint=False,
                                                 idempotentHint=idempotent, openWorldHint=False))(wrapped)
            return wrapped
        return register

    @tool()
    def kb_catalog(query: str = "", suffix: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Find current sources by title, filename or extension."""
        return Result(app.sources.catalog(query, suffix, limit))

    @tool()
    def kb_version(source_id: str, version_id: str | None = None) -> dict[str, Any]:
        """Find the selected or current immutable source version."""
        return app.show_source(source_id, version_id)

    @tool()
    def kb_status(source_id: str, version_id: str | None = None) -> dict[str, Any]:
        """Check which ranges have been parsed and indexed and any recorded processing failures."""
        return app.knowledge.status(source_id, version_id)

    @tool()
    def kb_render_attachment(source_id: str, name: str, version_id: str | None = None) -> Image:
        """Return a raster image attached to a Markdown or DOCX version."""
        image, _ = app.knowledge.render_attachment(source_id, name, version_id)
        return Image(data=image, format="png")

    @tool()
    def kb_describe(source_id: str, version_id: str | None = None) -> dict[str, Any]:
        """Inspect workbook sheets, PDF pages, image dimensions or text sections."""
        return app.knowledge.describe(source_id, version_id)

    @tool()
    def kb_read_sheet(source_id: str, sheet: str, cell_range: str, version_id: str | None = None,
                      limit: int = 100, cursor: str | None = None) -> dict[str, Any]:
        """Read Excel cells with formats, formulas, caches and pinned evidence."""
        return app.knowledge.read_sheet(source_id, sheet, cell_range, version_id, limit, cursor)

    @tool()
    def kb_read_text(source_id: str, version_id: str | None = None, page: int | None = None,
                     start: int = 0, limit: int = 4000) -> dict[str, Any]:
        """Read text by offset, using a page for PDF documents."""
        return app.knowledge.read_text(source_id, version_id, page, start, limit)

    @tool()
    def kb_search(query: str, limit: int = 30, cursor: str | None = None) -> dict[str, Any]:
        """Search indexed text and workbook values in current versions."""
        return app.knowledge.search(query, limit, cursor)

    @tool()
    def kb_calculate(source_id: str, sheet: str, cell_range: str, operation: str,
                     version_id: str | None = None, unit: str | None = None) -> dict[str, Any]:
        """Compute sum, difference, count or average over a fixed set of cells."""
        return app.knowledge.calculate(source_id, sheet, cell_range, operation, version_id, unit)

    @tool()
    def kb_electricity(source_id: str, sheet: str, row: int, category: str | None = None,
                       version_id: str | None = None, year: int | None = None) -> dict[str, Any]:
        """Check one electricity row and period using the workbook's known layout."""
        return analyze(app.knowledge, source_id, sheet, row, category, version_id, year)

    @tool()
    def kb_electricity_total(source_id: str, sheet: str, rows: list[int], category: str | None = None,
                             version_id: str | None = None, year: int | None = None) -> dict[str, Any]:
        """Total non-overlapping periods within one supported sheet and category."""
        return total_periods(app.knowledge, source_id, sheet, rows, category, version_id, year)

    @tool()
    def kb_resolve(reference: dict) -> dict[str, Any]:
        """Resolve an evidence citation against its immutable source version."""
        return app.knowledge.resolve(reference)

    @tool()
    def kb_ocr(source_id: str, page: int | None = None, version_id: str | None = None,
               rect: tuple[float, float, float, float] | None = None) -> dict[str, Any]:
        """Run local OCR on an image or an explicitly selected PDF page or region."""
        return app.knowledge.ocr(source_id, page, version_id, rect)

    @tool()
    def kb_render(source_id: str, page: int | None = None, version_id: str | None = None,
                  rect: tuple[float, float, float, float] | None = None) -> Image:
        """Return actual PNG image content for an image or PDF page."""
        image, _ = app.knowledge.render(source_id, page, version_id, rect)
        return Image(data=image, format="png")

    @tool(read_only=False)
    def task_create(title: str, content: str, request_id: str, due: str | None = None,
                    tags: list[str] | None = None) -> dict[str, Any]:
        """Create one task. Reuse the same request ID only on retry."""
        return Result(app.tasks.create(title, content, due=due, tags=tags, request_id=request_id))

    @tool()
    def task_get(task_id: str) -> dict[str, Any]:
        """Read one task by its stable ID."""
        return Result(app.tasks.get(task_id))

    @tool()
    def task_list(view: str | None = None, query: str | None = None, status: str | None = None,
                  limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """Query tasks by view, status or literal text."""
        return app.list_tasks(view=view, query=query, status=status, limit=limit, offset=offset)

    @tool(read_only=False)
    def task_update(task_id: str, expected_revision: int, changes: dict, request_id: str) -> dict[str, Any]:
        """Update title, content, due, tags or status with revision and retry protection."""
        return Result(app.tasks.update(task_id, expected_revision, request_id=request_id, **changes))

    @tool(read_only=False)
    def task_append_log(task_id: str, content: str, expected_revision: int, request_id: str,
                        corrects: str | None = None) -> dict[str, Any]:
        """Append a follow-up or correction without editing historical logs."""
        return Result(app.journal.append(task_id, content, expected_revision,
                                         corrects=corrects, request_id=request_id))

    @tool()
    def task_logs(task_id: str, after: int = 0, limit: int = 100) -> dict[str, Any]:
        """List follow-up entries after a task revision."""
        return Result(app.journal.logs(task_id, after=after, limit=limit))

    @tool(read_only=False)
    def review_create(task_ids: list[str], sections: dict, request_id: str) -> dict[str, Any]:
        """Save a review snapshot of only the explicitly selected tasks."""
        return Result(app.journal.create_review(task_ids, sections, request_id=request_id))

    @tool()
    def review_get(review_id: str) -> dict[str, Any]:
        """Read a saved review and historical snapshots."""
        return Result(app.journal.review(review_id))

    @tool()
    def review_list(limit: int = 100) -> dict[str, Any]:
        """List saved reviews."""
        return Result(app.journal.reviews(limit))

    return mcp


def run(app):
    server(app).run(transport="stdio")
