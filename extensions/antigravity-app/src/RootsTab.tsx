import React, { useEffect, useState } from 'react';
import type { App } from '@modelcontextprotocol/ext-apps';
import { callAppTool } from './api';
import { FolderPickerModal } from './FolderPickerModal';
import type { ScanReport, SourceRoot, SyncReport } from './types';

const DEFAULT_SUFFIX_CHOICES = [
  '.xlsx', '.pdf', '.docx', '.md', '.txt', '.png', '.jpg', '.jpeg', '.webp', '.tif', '.tiff',
];

interface RootsTabProps {
  app: App;
  onChanged: () => void;
}

export function RootsTab({ app, onChanged }: RootsTabProps) {
  const [roots, setRoots] = useState<SourceRoot[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Add form state
  const [newPath, setNewPath] = useState('');
  const [newName, setNewName] = useState('');
  const [newRecursive, setNewRecursive] = useState(true);
  const [newSuffixes, setNewSuffixes] = useState<string[]>(DEFAULT_SUFFIX_CHOICES);
  const [customSuffix, setCustomSuffix] = useState('');
  const [pickerOpen, setPickerOpen] = useState(false);

  // Edit state
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState('');
  const [editPath, setEditPath] = useState('');
  const [editRecursive, setEditRecursive] = useState(true);
  const [editSuffixesText, setEditSuffixesText] = useState('');

  // Scan & Sync state
  const [autoIndex, setAutoIndex] = useState(true);
  const [scanReport, setScanReport] = useState<ScanReport | null>(null);
  const [scanTargetRoot, setScanTargetRoot] = useState<string | null>(null);
  const [selectedPaths, setSelectedPaths] = useState<Set<string>>(new Set());
  const [syncReport, setSyncReport] = useState<SyncReport | null>(null);

  const loadRoots = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await callAppTool<SourceRoot[]>(app, 'app_roots_list');
      setRoots(res.data);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadRoots();
  }, []);

  const toggleSuffix = (suffix: string) => {
    setNewSuffixes((prev) =>
      prev.includes(suffix) ? prev.filter((item) => item !== suffix) : [...prev, suffix],
    );
  };

  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newPath.trim()) {
      setError('请输入或浏览选择一个本地目录绝对路径');
      return;
    }
    const extra = customSuffix
      .split(/[,，\s]+/u)
      .map((s) => s.trim())
      .filter(Boolean);
    const suffixes = Array.from(new Set([...newSuffixes, ...extra]));
    if (suffixes.length === 0) {
      setError('请至少选择一个文件后缀');
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const res = await callAppTool<SourceRoot>(app, 'app_root_add', {
        path: newPath.trim(),
        name: newName.trim() || undefined,
        recursive: newRecursive,
        suffixes,
      });
      setNewPath('');
      setNewName('');
      setCustomSuffix('');
      setNotice(`已添加目录「${res.data.name}」（${res.data.path}）`);
      await loadRoots();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const startEdit = (root: SourceRoot) => {
    setEditingId(root.root_id);
    setEditName(root.name);
    setEditPath(root.path);
    setEditRecursive(root.recursive);
    setEditSuffixesText(root.suffixes.join(', '));
  };

  const saveEdit = async (rootId: string) => {
    const suffixes = editSuffixesText
      .split(/[,，\s]+/u)
      .map((s) => s.trim())
      .filter(Boolean);
    setBusy(true);
    setError(null);
    try {
      await callAppTool<SourceRoot>(app, 'app_root_update', {
        root_id: rootId,
        name: editName.trim(),
        path: editPath.trim(),
        recursive: editRecursive,
        suffixes,
      });
      setEditingId(null);
      setNotice('目录配置已更新');
      await loadRoots();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const toggleEnabled = async (root: SourceRoot) => {
    setBusy(true);
    setError(null);
    try {
      await callAppTool<SourceRoot>(app, 'app_root_update', {
        root_id: root.root_id,
        enabled: !root.enabled,
      });
      await loadRoots();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const handleRemove = async (root: SourceRoot) => {
    setBusy(true);
    setError(null);
    try {
      await callAppTool(app, 'app_root_remove', { root_id: root.root_id });
      setNotice(`已移除目录登记「${root.name}」（已导入资料快照完整保留）`);
      if (scanTargetRoot === root.root_id) {
        setScanReport(null);
      }
      await loadRoots();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const handleScan = async (rootId: string | null = null) => {
    setBusy(true);
    setError(null);
    setSyncReport(null);
    try {
      const res = await callAppTool<ScanReport>(app, 'app_root_scan', {
        root_id: rootId ?? undefined,
      });
      setScanReport(res.data);
      setScanTargetRoot(rootId);
      const actionable = res.data.items
        .filter((item) => item.status === 'new' || item.status === 'changed')
        .map((item) => item.path);
      setSelectedPaths(new Set(actionable));
      setNotice(
        `扫描完成：新增 ${res.data.counts.new}，已变更 ${res.data.counts.changed}，未变更 ${res.data.counts.unchanged}，原文件已移走 ${res.data.counts.missing}`,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const handleSync = async (rootId: string | null = null, paths?: string[]) => {
    setBusy(true);
    setError(null);
    try {
      const res = await callAppTool<SyncReport>(app, 'app_root_sync', {
        root_id: rootId ?? undefined,
        paths,
        auto_index: autoIndex,
      });
      setSyncReport(res.data);
      setScanReport(null);
      setNotice(
        `同步完成：新增导入 ${res.data.imported}，更新版本 ${res.data.updated}，已建索引 ${res.data.indexed}，跳过 ${res.data.skipped}，失败 ${res.data.errors}`,
      );
      await loadRoots();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const toggleSelectPath = (filePath: string) => {
    setSelectedPaths((prev) => {
      const next = new Set(prev);
      if (next.has(filePath)) next.delete(filePath);
      else next.add(filePath);
      return next;
    });
  };

  const actionableItems = scanReport?.items.filter((i) => i.status === 'new' || i.status === 'changed') ?? [];
  const missingItems = scanReport?.items.filter((i) => i.status === 'missing') ?? [];

  return (
    <div className="col">
      {error && <div className="banner error">{error}</div>}
      {notice && <div className="banner success">{notice}</div>}

      <form className="card" onSubmit={(e) => void handleAdd(e)}>
        <div className="card-title">
          <span>添加本地工作目录</span>
          <span className="muted">同步后 Antigravity 与 ChatGPT 自动共享</span>
        </div>
        <div className="row">
          <div className="row-grow">
            <input
              type="text"
              placeholder="输入绝对路径，例如 D:\工作资料\电费台账"
              value={newPath}
              onChange={(e) => setNewPath(e.target.value)}
            />
          </div>
          <button type="button" onClick={() => setPickerOpen(true)} disabled={busy}>
            浏览目录…
          </button>
        </div>
        <div className="row">
          <div className="row-grow">
            <input
              type="text"
              placeholder="显示名称（可选，默认取目录名）"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
            />
          </div>
          <label className="row" style={{ fontSize: 12 }}>
            <input
              type="checkbox"
              checked={newRecursive}
              onChange={(e) => setNewRecursive(e.target.checked)}
            />
            递归扫描子目录
          </label>
        </div>
        <div className="col">
          <span className="muted">包含文件类型：</span>
          <div className="suffix-grid">
            {DEFAULT_SUFFIX_CHOICES.map((suffix) => (
              <button
                key={suffix}
                type="button"
                className={`suffix-pill ${newSuffixes.includes(suffix) ? 'selected' : ''}`}
                onClick={() => toggleSuffix(suffix)}
              >
                {suffix}
              </button>
            ))}
          </div>
          <div className="row">
            <div className="row-grow">
              <input
                type="text"
                placeholder="额外后缀（可选，逗号分隔，如 .csv, .json）"
                value={customSuffix}
                onChange={(e) => setCustomSuffix(e.target.value)}
              />
            </div>
            <button type="submit" className="primary" disabled={busy}>
              + 添加目录
            </button>
          </div>
        </div>
      </form>

      <div className="card">
        <div className="card-title">
          <span>已登记目录 ({roots.length})</span>
          <div className="row">
            <label className="row" style={{ fontSize: 12 }}>
              <input
                type="checkbox"
                checked={autoIndex}
                onChange={(e) => setAutoIndex(e.target.checked)}
              />
              同步后自动建索引
            </label>
            <button type="button" onClick={() => void handleScan(null)} disabled={busy || roots.length === 0}>
              扫描全部差异
            </button>
            <button
              type="button"
              className="primary"
              onClick={() => void handleSync(null)}
              disabled={busy || roots.length === 0}
            >
              一键同步全部
            </button>
          </div>
        </div>

        {loading && <div className="muted">加载目录列表中…</div>}
        {!loading && roots.length === 0 && (
          <div className="muted">尚未添加任何工作目录。请在上方输入路径或点击「浏览目录…」手动添加。</div>
        )}

        <div className="item-list">
          {roots.map((root) => (
            <div key={root.root_id} className="item-card">
              {editingId === root.root_id ? (
                <div className="col">
                  <input type="text" value={editName} onChange={(e) => setEditName(e.target.value)} placeholder="名称" />
                  <input type="text" value={editPath} onChange={(e) => setEditPath(e.target.value)} placeholder="绝对路径" />
                  <input
                    type="text"
                    value={editSuffixesText}
                    onChange={(e) => setEditSuffixesText(e.target.value)}
                    placeholder="后缀列表，逗号分隔"
                  />
                  <label className="row" style={{ fontSize: 12 }}>
                    <input
                      type="checkbox"
                      checked={editRecursive}
                      onChange={(e) => setEditRecursive(e.target.checked)}
                    />
                    递归子目录
                  </label>
                  <div className="row">
                    <button type="button" className="primary" onClick={() => void saveEdit(root.root_id)} disabled={busy}>
                      保存
                    </button>
                    <button type="button" onClick={() => setEditingId(null)}>取消</button>
                  </div>
                </div>
              ) : (
                <>
                  <div className="row" style={{ justifyContent: 'space-between' }}>
                    <div className="row">
                      <strong>{root.name}</strong>
                      <span className={`badge ${root.enabled ? 'success' : ''}`}>
                        {root.enabled ? '已启用' : '已停用'}
                      </span>
                      <span className="badge">{root.recursive ? '含子目录' : '仅当前层'}</span>
                    </div>
                    <div className="row">
                      <button type="button" onClick={() => void handleScan(root.root_id)} disabled={busy}>
                        扫描差异
                      </button>
                      <button type="button" onClick={() => void handleSync(root.root_id)} disabled={busy}>
                        同步
                      </button>
                      <button type="button" onClick={() => startEdit(root)} disabled={busy}>
                        编辑
                      </button>
                      <button type="button" onClick={() => void toggleEnabled(root)} disabled={busy}>
                        {root.enabled ? '停用' : '启用'}
                      </button>
                      <button type="button" className="danger" onClick={() => void handleRemove(root)} disabled={busy}>
                        移除
                      </button>
                    </div>
                  </div>
                  <div className="mono">{root.path}</div>
                  <div className="muted">
                    后缀：{root.suffixes.join(' ')} · 上次同步：{root.last_synced_at ? root.last_synced_at.slice(0, 19).replace('T', ' ') : '尚未同步'}
                  </div>
                </>
              )}
            </div>
          ))}
        </div>
      </div>

      {scanReport && (
        <div className="card">
          <div className="card-title">
            <span>差异预览</span>
            <div className="row">
              <span className="badge new">新增 {scanReport.counts.new}</span>
              <span className="badge changed">变更 {scanReport.counts.changed}</span>
              <span className="badge">未变 {scanReport.counts.unchanged}</span>
              {scanReport.counts.missing > 0 && (
                <span className="badge missing">原文件已移走 {scanReport.counts.missing}</span>
              )}
            </div>
          </div>

          {actionableItems.length === 0 ? (
            <div className="muted">没有需要新增或更新的文件。</div>
          ) : (
            <>
              <div className="row">
                <button
                  type="button"
                  onClick={() => setSelectedPaths(new Set(actionableItems.map((i) => i.path)))}
                >
                  全选 ({actionableItems.length})
                </button>
                <button type="button" onClick={() => setSelectedPaths(new Set())}>全不选</button>
                <button
                  type="button"
                  className="primary"
                  disabled={busy || selectedPaths.size === 0}
                  onClick={() => void handleSync(scanTargetRoot, Array.from(selectedPaths))}
                >
                  同步已选文件 ({selectedPaths.size})
                </button>
              </div>
              <div className="item-list">
                {actionableItems.map((item) => (
                  <label key={item.path} className="item-card" style={{ cursor: 'pointer' }}>
                    <div className="row">
                      <input
                        type="checkbox"
                        checked={selectedPaths.has(item.path)}
                        onChange={() => toggleSelectPath(item.path)}
                      />
                      <span className={`badge ${item.status}`}>
                        {item.status === 'new' ? '新增' : '已变更'}
                      </span>
                      <strong>{item.name}</strong>
                      <span className="muted">({item.root_name})</span>
                    </div>
                    <div className="mono">{item.relative_path}</div>
                  </label>
                ))}
              </div>
            </>
          )}

          {missingItems.length > 0 && (
            <div className="col" style={{ marginTop: 6 }}>
              <span className="muted">原文件已从磁盘移走（库内历史快照仍安全保留，不会自动删除）：</span>
              {missingItems.map((item) => (
                <div key={item.source_id ?? item.path} className="mono">
                  · [{item.root_name}] {item.relative_path} ({item.source_id})
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {syncReport && syncReport.results.length > 0 && (
        <div className="card">
          <div className="card-title">最近一次同步结果</div>
          <div className="item-list">
            {syncReport.results.slice(0, 30).map((item, idx) => (
              <div key={`${item.path}-${idx}`} className="row" style={{ justifyContent: 'space-between' }}>
                <span className="mono">{item.relative_path}</span>
                <span className={`badge ${item.action === 'error' ? 'missing' : 'success'}`}>
                  {item.action === 'imported'
                    ? `已导入 v${item.sequence}`
                    : item.action === 'updated'
                      ? `已更新 v${item.sequence}`
                      : item.action === 'error'
                        ? `失败: ${item.error?.message ?? ''}`
                        : item.action}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {pickerOpen && (
        <FolderPickerModal
          app={app}
          initialPath={newPath}
          onSelect={(picked) => setNewPath(picked)}
          onClose={() => setPickerOpen(false)}
        />
      )}
    </div>
  );
}
