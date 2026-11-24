"""Use cases shared by CLI and the local MCP server."""
from pathlib import Path

from .contracts import Result
from .journal import Journal
from .knowledge import Knowledge, checked_limit
from .roots import RootService
from .sources import SourceService
from .storage import Store
from .tasks import TaskService


class Application:
    def __init__(self, data_dir: str | Path):
        self.store = Store(data_dir)
        self.sources = SourceService(self.store)
        self.tasks = TaskService(self.store)
        self.journal = Journal(self.store)
        self.knowledge = Knowledge(self.store, self.sources)
        self.roots = RootService(self.store, self.sources, self.knowledge)

    def show_source(self, source_id, version_id=None, *, local_path=False):
        version = self.sources.version(source_id, version_id)
        value = {"source": self.sources.get(source_id), "version": version}
        if local_path:
            value["snapshot_path"] = str(self.sources.snapshot_path(source_id, version.version_id))
        return Result(value, coverage="not_processed")

    def list_tasks(self, *, status=None, view=None, query=None, limit=100, offset=0):
        checked_limit(limit, 200)
        return Result(self.tasks.list(status=status, view=view, query=query, limit=limit, offset=offset),
                      warnings=["列表使用 offset/limit；并发写入期间不保证跨页快照一致。"])
