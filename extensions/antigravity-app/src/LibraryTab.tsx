import React, { useEffect, useState } from 'react';
import type { App } from '@modelcontextprotocol/ext-apps';
import { callAppTool } from './api';
import type { CatalogItem, SourceStatusData, SourceVersion } from './types';

interface LibraryTabProps {
  app: App;
  onChanged: () => void;
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(2)} MB`;
}

export function LibraryTab({ app, onChanged }: LibraryTabProps) {
  const [query, setQuery] = useState('');
  const [suffix, setSuffix] = useState('');
  const [includeArchived, setIncludeArchived] = useState(false);
  const [items, setItems] = useState<CatalogItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [selected, setSelected] = useState<CatalogItem | null>(null);
  const [versions, setVersions] = useState<SourceVersion[]>([]);
  const [statusData, setStatusData] = useState<SourceStatusData | null>(null);
  const [renameText, setRenameText] = useState('');

  const loadCatalog = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await callAppTool<CatalogItem[]>(app, 'app_sources_catalog', {
        query: query.trim(),
        suffix: suffix || undefined,
        limit: 100,
        include_archived: includeArchived,
      });
      setItems(res.data);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadCatalog();
  }, [includeArchived, suffix]);

  const inspectSource = async (item: CatalogItem) => {
    setSelected(item);
    setRenameText(item.title);
    setError(null);
    try {
      const [verRes, statusRes] = await Promise.all([
        callAppTool<SourceVersion[]>(app, 'app_source_versions', { source_id: item.source_id }),
        callAppTool<SourceStatusData>(app, 'app_source_status', { source_id: item.source_id }),
      ]);
      setVersions(verRes.data);
      setStatusData(statusRes.data);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const handleIndex = async (sourceId: string) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const res = await callAppTool<Record<string, unknown>>(app, 'app_source_index', { source_id: sourceId });
      setNotice(`索引完成（覆盖度：${res.coverage ?? 'ok'}）`);
      if (selected && selected.source_id === sourceId) {
        await inspectSource(selected);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const handleRename = async () => {
    if (!selected || !renameText.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await callAppTool(app, 'app_source_edit', {
        source_id: selected.source_id,
        title: renameText.trim(),
      });
      setNotice('已更新资料标题');
      await loadCatalog();
      setSelected({ ...selected, title: renameText.trim() });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const handleToggleArchive = async (item: CatalogItem) => {
    setBusy(true);
    setError(null);
    try {
      const nextArchived = !Boolean(item.archived);
      await callAppTool(app, 'app_source_edit', {
        source_id: item.source_id,
        archived: nextArchived,
      });
      setNotice(nextArchived ? '已归档该资料' : '已恢复显示该资料');
      await loadCatalog();
      onChanged();
      if (selected?.source_id === item.source_id) {
        setSelected({ ...selected, archived: nextArchived });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="col">
      {error && <div className="banner error">{error}</div>}
      {notice && <div className="banner success">{notice}</div>}

      <form
        className="card"
        onSubmit={(e) => {
          e.preventDefault();
          void loadCatalog();
        }}
      >
        <div className="row">
          <div className="row-grow">
            <input
              type="search"
              placeholder="按标题或文件名筛选资料…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
          <select value={suffix} onChange={(e) => setSuffix(e.target.value)} style={{ width: 'auto' }}>
            <option value="">全部类型</option>
            <option value=".xlsx">.xlsx</option>
            <option value=".pdf">.pdf</option>
            <option value=".docx">.docx</option>
            <option value=".md">.md</option>
            <option value=".txt">.txt</option>
            <option value=".png">.png</option>
            <option value=".jpg">.jpg</option>
          </select>
          <button type="submit" className="primary">查找</button>
        </div>
        <label className="row" style={{ fontSize: 12 }}>
          <input
            type="checkbox"
            checked={includeArchived}
            onChange={(e) => setIncludeArchived(e.target.checked)}
          />
          显示已归档资料
        </label>
      </form>

      <div className="card">
        <div className="card-title">
          <span>资料列表 ({items.length})</span>
        </div>
        {loading && <div className="muted">加载中…</div>}
        {!loading && items.length === 0 && <div className="muted">暂无匹配的资料。</div>}
        <div className="item-list">
          {items.map((item) => (
            <div key={item.source_id} className="item-card">
              <div className="row" style={{ justifyContent: 'space-between' }}>
                <div className="row">
                  <strong>{item.title}</strong>
                  {Boolean(item.archived) && <span className="badge">已归档</span>}
                </div>
                <div className="row">
                  <button type="button" onClick={() => void inspectSource(item)}>详情</button>
                  <button type="button" onClick={() => void handleIndex(item.source_id)} disabled={busy}>
                    建索引
                  </button>
                </div>
              </div>
              <div className="muted">
                {item.original_name} · {formatBytes(item.size)} · <span className="mono">{item.source_id}</span>
              </div>
            </div>
          ))}
        </div>
      </div>

      {selected && (
        <div className="card">
          <div className="card-title">
            <span>资料详情</span>
            <button type="button" onClick={() => setSelected(null)}>关闭</button>
          </div>
          <div className="mono">source_id: {selected.source_id}</div>
          <div className="row">
            <div className="row-grow">
              <input type="text" value={renameText} onChange={(e) => setRenameText(e.target.value)} />
            </div>
            <button type="button" onClick={() => void handleRename()} disabled={busy}>保存标题</button>
            <button type="button" onClick={() => void handleToggleArchive(selected)} disabled={busy}>
              {Boolean(selected.archived) ? '取消归档' : '归档'}
            </button>
          </div>
          {statusData && (
            <div className="col">
              <strong>当前版本解析与索引状态</strong>
              {statusData.artifacts.length === 0 ? (
                <div className="muted">尚未解析（点击「建索引」后可被全文检索）。</div>
              ) : (
                statusData.artifacts.map((art) => (
                  <div key={art.artifact_id} className="muted">
                    · {art.scope} ({art.backend}) — 状态: <strong>{art.state}</strong>，索引条目: {art.indexed_entries}
                  </div>
                ))
              )}
              {statusData.failures.map((f, idx) => (
                <div key={idx} className="banner error">
                  失败范围 {f.scope}: {f.error?.message ?? JSON.stringify(f.error)}
                </div>
              ))}
            </div>
          )}
          <div className="col">
            <strong>历史版本 ({versions.length})</strong>
            {versions.map((ver) => (
              <div key={ver.version_id} className="mono">
                v{ver.sequence} · {ver.original_name} · {formatBytes(ver.size)} · {ver.created_at.slice(0, 19).replace('T', ' ')}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
