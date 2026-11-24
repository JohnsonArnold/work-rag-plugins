import React, { useEffect, useState } from 'react';
import type { App } from '@modelcontextprotocol/ext-apps';
import { callAppTool } from './api';
import type { BrowseResult } from './types';

interface FolderPickerModalProps {
  app: App;
  initialPath?: string;
  onSelect: (path: string) => void;
  onClose: () => void;
}

export function FolderPickerModal({ app, initialPath, onSelect, onClose }: FolderPickerModalProps) {
  const [data, setData] = useState<BrowseResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async (targetPath?: string | null) => {
    setLoading(true);
    setError(null);
    try {
      const res = await callAppTool<BrowseResult>(app, 'app_browse_dirs', {
        path: targetPath ?? undefined,
      });
      setData(res.data);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      if (targetPath) {
        // Fallback to drive list if initial path doesn't exist.
        try {
          const fallback = await callAppTool<BrowseResult>(app, 'app_browse_dirs', {});
          setData(fallback.data);
        } catch {
          // ignore secondary failure
        }
      }
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load(initialPath?.trim() || null);
  }, []);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <strong>选择本地目录</strong>
          <button type="button" onClick={onClose}>关闭</button>
        </div>
        <div className="modal-body">
          <div className="row" style={{ marginBottom: 6 }}>
            <button type="button" onClick={() => void load(null)} disabled={loading}>磁盘列表</button>
            <button
              type="button"
              onClick={() => void load(data?.parent ?? null)}
              disabled={loading || !data?.current}
            >
              上级目录
            </button>
            <span className="mono row-grow">{data?.current ?? '请选择磁盘或快捷目录'}</span>
          </div>
          {error && <div className="banner error">{error}</div>}
          {loading && <div className="muted">读取目录中…</div>}
          {!loading && data && data.entries.length === 0 && (
            <div className="muted">当前目录下没有子文件夹。</div>
          )}
          {!loading && data?.entries.map((entry) => (
            <button
              key={entry.path}
              type="button"
              className="dir-entry"
              onClick={() => void load(entry.path)}
            >
              <span>📁 {entry.name}</span>
              <span className="muted">{entry.kind === 'drive' ? '磁盘' : entry.kind === 'shortcut' ? '快捷' : '进入'}</span>
            </button>
          ))}
        </div>
        <div className="modal-footer">
          <button type="button" onClick={onClose}>取消</button>
          <button
            type="button"
            className="primary"
            disabled={!data?.current}
            onClick={() => {
              if (data?.current) {
                onSelect(data.current);
                onClose();
              }
            }}
          >
            选择当前目录
          </button>
        </div>
      </div>
    </div>
  );
}
