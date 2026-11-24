"""Tests for Antigravity MCP App isolation and end-to-end tool/resource round-trips."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from work_rag.application import Application
from work_rag.mcp_app import APP_MIME_TYPE, APP_URI


class McpAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.library = self.base / "library"
        self.docs = self.base / "docs"
        self.docs.mkdir()
        (self.docs / "memo.md").write_text("# 项目计划\n完成多目录同步面板。\n", encoding="utf-8")

    def _params(self, *, enable_app: bool) -> StdioServerParameters:
        project = Path(__file__).resolve().parents[1]
        env = {**os.environ}
        pythonpath = [str(project)]
        if env.get("PYTHONPATH"):
            pythonpath.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(pythonpath)
        args = ["-m", "work_rag", "--data-dir", str(self.library), "mcp"]
        if enable_app:
            args.append("--enable-app")
        return StdioServerParameters(command=sys.executable, args=args, cwd=str(self.base), env=env)

    def test_standard_mcp_does_not_expose_app_tools_or_ui_resource(self):
        async def check():
            async with stdio_client(self._params(enable_app=False)) as (receive, send):
                async with ClientSession(receive, send) as session:
                    await session.initialize()
                    tools = {t.name for t in (await session.list_tools()).tools}
                    self.assertIn("kb_catalog", tools)
                    self.assertNotIn("work_rag_panel", tools)
                    self.assertFalse(any(name.startswith("app_") for name in tools))
                    resources = await session.list_resources()
                    self.assertEqual([str(r.uri) for r in resources.resources], [])

        asyncio.run(check())

    def test_enable_app_exposes_ui_resource_entrypoints_and_app_tools(self):
        async def check():
            async with stdio_client(self._params(enable_app=True)) as (receive, send):
                async with ClientSession(receive, send) as session:
                    await session.initialize()
                    tools = {t.name: t for t in (await session.list_tools()).tools}
                    self.assertIn("work_rag_panel", tools)
                    panel_meta = tools["work_rag_panel"].meta
                    self.assertEqual(panel_meta["ui"]["resourceUri"], APP_URI)
                    entrypoints = panel_meta["com.google.antigravity/ui"]["entrypoints"]
                    self.assertEqual([e["type"] for e in entrypoints], ["aux_pane", "global"])

                    # App-only tools must carry visibility=["app"].
                    for name in ("app_overview", "app_root_add", "app_root_scan", "app_root_sync", "app_task_create"):
                        self.assertIn(name, tools)
                        self.assertEqual(tools[name].meta["ui"]["visibility"], ["app"])

                    # UI resource is readable with the required MCP App MIME type.
                    resources = {str(r.uri): r for r in (await session.list_resources()).resources}
                    self.assertIn(APP_URI, resources)
                    self.assertEqual(resources[APP_URI].mimeType, APP_MIME_TYPE)
                    content = await session.read_resource(APP_URI)
                    self.assertTrue(content.contents[0].text.strip().startswith("<!doctype html>") or "<html" in content.contents[0].text.lower())

                    # Empty-arg entrypoint launch works.
                    launched = await session.call_tool("work_rag_panel", {})
                    self.assertFalse(launched.isError, launched.content)
                    self.assertEqual(launched.structuredContent["data"]["tab"], "roots")

                    # Add root, scan, sync via app-only tools; then standard kb_search finds it!
                    added = await session.call_tool("app_root_add", {"path": str(self.docs), "name": "工作文档"})
                    self.assertFalse(added.isError, added.content)
                    root_id = added.structuredContent["data"]["root_id"]

                    scanned = await session.call_tool("app_root_scan", {"root_id": root_id})
                    self.assertEqual(scanned.structuredContent["data"]["counts"]["new"], 1)

                    synced = await session.call_tool("app_root_sync", {"root_id": root_id, "auto_index": True})
                    self.assertFalse(synced.isError, synced.content)
                    self.assertEqual(synced.structuredContent["data"]["imported"], 1)

                    searched = await session.call_tool("kb_search", {"query": "多目录同步"})
                    self.assertFalse(searched.isError, searched.content)
                    self.assertEqual(len(searched.structuredContent["data"]["hits"]), 1)

        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
