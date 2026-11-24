import React, { useEffect, useRef, useState } from 'react';
import {
  App,
  applyDocumentTheme,
  applyHostStyleVariables,
} from '@modelcontextprotocol/ext-apps';
import { callAppTool, extractEnvelope } from './api';
import { LibraryTab } from './LibraryTab';
import { RootsTab } from './RootsTab';
import { ReviewsTab, TasksTab } from './TasksTab';
import type { OverviewData, TabKey } from './types';

export function WorkRagApp() {
  const appRef = useRef<App | null>(null);
  const [connected, setConnected] = useState(false);
  const [connectError, setConnectError] = useState<string | null>(null);
  const [tab, setTab] = useState<TabKey>('roots');
  const [overview, setOverview] = useState<OverviewData | null>(null);

  const refreshOverview = async () => {
    const app = appRef.current;
    if (!app) return;
    try {
      const res = await callAppTool<OverviewData>(app, 'app_overview');
      setOverview(res.data);
    } catch {
      // keep existing overview on transient error
    }
  };

  useEffect(() => {
    const app = new App({ name: 'Work-RAG', version: '0.4.0' }, {});
    appRef.current = app;

    app.ontoolresult = (result) => {
      try {
        const env = extractEnvelope<OverviewData>(result);
        if (env.data) {
          setOverview(env.data);
          if (env.data.tab) {
            setTab(env.data.tab);
          }
        }
      } catch {
        // ignore non-overview tool results
      }
    };

    app.onhostcontextchanged = (ctx) => {
      if (ctx.theme) applyDocumentTheme(ctx.theme);
      if (ctx.styles?.variables) applyHostStyleVariables(ctx.styles.variables);
    };

    void (async () => {
      try {
        await app.connect();
        const ctx = app.getHostContext();
        if (ctx?.theme) applyDocumentTheme(ctx.theme);
        if (ctx?.styles?.variables) applyHostStyleVariables(ctx.styles.variables);
        setConnected(true);
        await refreshOverview();
      } catch (err) {
        setConnectError(err instanceof Error ? err.message : String(err));
      }
    })();
  }, []);

  if (connectError) {
    return (
      <div className="app-shell" style={{ padding: 16 }}>
        <div className="banner error">无法连接到 Work-RAG MCP 服务：{connectError}</div>
      </div>
    );
  }

  if (!connected || !appRef.current) {
    return (
      <div className="app-shell" style={{ padding: 16 }}>
        <div className="muted">正在连接 Work-RAG 本地库…</div>
      </div>
    );
  }

  const app = appRef.current;

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="header-top">
          <div className="brand">
            <span>Work-RAG</span>
            <span className="muted">本地资料库 · 零模型直连</span>
          </div>
          <button type="button" onClick={() => void refreshOverview()}>刷新</button>
        </div>
        {overview && (
          <>
            <div className="data-dir-pill" title={overview.data_dir}>
              数据目录：{overview.data_dir}
            </div>
            <div className="stats-row">
              <span className="stat-chip">源目录 {overview.counts.enabled_roots}/{overview.counts.roots}</span>
              <span className="stat-chip">资料 {overview.counts.sources}</span>
              <span className="stat-chip">未完成事项 {overview.counts.unfinished_tasks}</span>
              <span className="stat-chip">复盘 {overview.counts.reviews}</span>
            </div>
          </>
        )}
      </header>

      <nav className="tab-bar">
        <button type="button" className={`tab-btn ${tab === 'roots' ? 'active' : ''}`} onClick={() => setTab('roots')}>
          源目录配置
        </button>
        <button type="button" className={`tab-btn ${tab === 'library' ? 'active' : ''}`} onClick={() => setTab('library')}>
          资料库
        </button>
        <button type="button" className={`tab-btn ${tab === 'tasks' ? 'active' : ''}`} onClick={() => setTab('tasks')}>
          事项
        </button>
        <button type="button" className={`tab-btn ${tab === 'reviews' ? 'active' : ''}`} onClick={() => setTab('reviews')}>
          复盘
        </button>
      </nav>

      <main className="app-body">
        {tab === 'roots' && <RootsTab app={app} onChanged={() => void refreshOverview()} />}
        {tab === 'library' && <LibraryTab app={app} onChanged={() => void refreshOverview()} />}
        {tab === 'tasks' && <TasksTab app={app} onChanged={() => void refreshOverview()} />}
        {tab === 'reviews' && <ReviewsTab app={app} />}
      </main>
    </div>
  );
}
