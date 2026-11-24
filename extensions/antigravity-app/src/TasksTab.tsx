import React, { useEffect, useState } from 'react';
import type { App } from '@modelcontextprotocol/ext-apps';
import { callAppTool, makeRequestId } from './api';
import type { ReviewDetail, ReviewSummary, TaskDTO, TaskLogDTO, TaskStatus } from './types';

const STATUS_LABELS: Record<TaskStatus, string> = {
  pending: '待处理',
  in_progress: '处理中',
  completed: '已完成',
  cancelled: '已取消',
};

const SECTION_LABELS: Record<string, string> = {
  background: '背景',
  process: '过程',
  result: '结果',
  improvement: '改进',
};

const KIND_LABELS: Record<string, string> = {
  user_statement: '用户陈述',
  source_excerpt: '资料摘录',
  ai_inference: 'AI 推断',
};

interface TasksTabProps {
  app: App;
  onChanged: () => void;
}

export function TasksTab({ app, onChanged }: TasksTabProps) {
  const [filter, setFilter] = useState<'unfinished' | 'today' | 'overdue' | 'recent' | 'completed' | 'all'>('unfinished');
  const [query, setQuery] = useState('');
  const [tasks, setTasks] = useState<TaskDTO[]>([]);
  const [selected, setSelected] = useState<TaskDTO | null>(null);
  const [logs, setLogs] = useState<TaskLogDTO[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Create task
  const [showCreate, setShowCreate] = useState(false);
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [due, setDue] = useState('');
  const [tagsText, setTagsText] = useState('');

  // Follow-up & edit
  const [followText, setFollowText] = useState('');
  const [editing, setEditing] = useState(false);
  const [editTitle, setEditTitle] = useState('');
  const [editContent, setEditContent] = useState('');
  const [editDue, setEditDue] = useState('');
  const [editTags, setEditTags] = useState('');

  const loadTasks = async () => {
    setLoading(true);
    setError(null);
    try {
      const args: Record<string, unknown> = { query: query.trim() || undefined, limit: 100 };
      if (filter === 'completed') args.status = 'completed';
      else if (filter !== 'all') args.view = filter;
      const res = await callAppTool<TaskDTO[]>(app, 'app_tasks_list', args);
      setTasks(res.data);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadTasks();
  }, [filter]);

  const selectTask = async (taskId: string) => {
    setError(null);
    setEditing(false);
    try {
      const [taskRes, logsRes] = await Promise.all([
        callAppTool<TaskDTO>(app, 'app_task_get', { task_id: taskId }),
        callAppTool<TaskLogDTO[]>(app, 'app_task_logs', { task_id: taskId }),
      ]);
      setSelected(taskRes.data);
      setLogs(logsRes.data);
      setEditTitle(taskRes.data.title);
      setEditContent(taskRes.data.content);
      setEditDue(taskRes.data.due ?? '');
      setEditTags(taskRes.data.tags.join(', '));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim() || !content.trim()) {
      setError('请填写标题和内容');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const tags = tagsText.split(/[,，]/u).map((s) => s.trim()).filter(Boolean);
      const res = await callAppTool<TaskDTO>(app, 'app_task_create', {
        title: title.trim(),
        content: content.trim(),
        due: due.trim() || undefined,
        tags,
        request_id: makeRequestId('create'),
      });
      setTitle('');
      setContent('');
      setDue('');
      setTagsText('');
      setShowCreate(false);
      setNotice(`已创建事项 ${res.data.task_id}`);
      await loadTasks();
      await selectTask(res.data.task_id);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const handleUpdate = async (changes: Record<string, unknown>, message: string) => {
    if (!selected) return;
    setBusy(true);
    setError(null);
    try {
      const res = await callAppTool<TaskDTO>(app, 'app_task_update', {
        task_id: selected.task_id,
        expected_revision: selected.revision,
        changes,
        request_id: makeRequestId('update'),
      });
      setSelected(res.data);
      setEditing(false);
      setNotice(message);
      await loadTasks();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const handleAppendLog = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selected || !followText.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await callAppTool(app, 'app_task_append_log', {
        task_id: selected.task_id,
        content: followText.trim(),
        expected_revision: selected.revision,
        request_id: makeRequestId('log'),
      });
      setFollowText('');
      setNotice('已追加跟进记录');
      await selectTask(selected.task_id);
      await loadTasks();
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

      <div className="card">
        <div className="row">
          <select value={filter} onChange={(e) => setFilter(e.target.value as typeof filter)} style={{ width: 'auto' }}>
            <option value="unfinished">未完成</option>
            <option value="today">今日到期</option>
            <option value="overdue">已逾期</option>
            <option value="recent">最近新增</option>
            <option value="completed">已完成</option>
            <option value="all">全部</option>
          </select>
          <div className="row-grow">
            <input
              type="search"
              placeholder="搜索标题、内容或标签…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') void loadTasks(); }}
            />
          </div>
          <button type="button" onClick={() => void loadTasks()}>查找</button>
          <button type="button" className="primary" onClick={() => setShowCreate((v) => !v)}>
            {showCreate ? '收起表单' : '+ 新建事项'}
          </button>
        </div>

        {showCreate && (
          <form className="col" style={{ marginTop: 8, paddingTop: 8, borderTop: '1px solid var(--border-secondary)' }} onSubmit={(e) => void handleCreate(e)}>
            <input type="text" placeholder="事项标题" value={title} onChange={(e) => setTitle(e.target.value)} />
            <textarea rows={3} placeholder="事项内容" value={content} onChange={(e) => setContent(e.target.value)} />
            <div className="row">
              <div className="row-grow">
                <input type="text" placeholder="截止日期（可选，如 2026-11-30）" value={due} onChange={(e) => setDue(e.target.value)} />
              </div>
              <div className="row-grow">
                <input type="text" placeholder="标签（逗号分隔）" value={tagsText} onChange={(e) => setTagsText(e.target.value)} />
              </div>
            </div>
            <div className="row">
              <button type="submit" className="primary" disabled={busy}>保存事项</button>
            </div>
          </form>
        )}
      </div>

      <div className="card">
        <div className="card-title">事项列表 ({tasks.length})</div>
        {loading && <div className="muted">加载中…</div>}
        {!loading && tasks.length === 0 && <div className="muted">此视图下没有事项。</div>}
        <div className="item-list">
          {tasks.map((task) => (
            <div
              key={task.task_id}
              className="item-card"
              style={{ cursor: 'pointer', borderColor: selected?.task_id === task.task_id ? 'var(--ring-primary)' : undefined }}
              onClick={() => void selectTask(task.task_id)}
            >
              <div className="row" style={{ justifyContent: 'space-between' }}>
                <strong>{task.title}</strong>
                <span className={`badge ${task.status === 'completed' ? 'success' : task.status === 'in_progress' ? 'new' : ''}`}>
                  {STATUS_LABELS[task.status]} · r{task.revision}
                </span>
              </div>
              <div className="muted">
                {task.due ? `截止: ${task.due}` : '无截止日期'}
                {task.tags.length > 0 ? ` · 标签: ${task.tags.join('、')}` : ''}
              </div>
            </div>
          ))}
        </div>
      </div>

      {selected && (
        <div className="card">
          <div className="card-title">
            <span>{selected.title}</span>
            <div className="row">
              <button type="button" onClick={() => setEditing((v) => !v)}>{editing ? '取消编辑' : '编辑'}</button>
              <button type="button" onClick={() => setSelected(null)}>关闭</button>
            </div>
          </div>
          <div className="mono">{selected.task_id} · {STATUS_LABELS[selected.status]} · r{selected.revision}</div>

          {editing ? (
            <div className="col">
              <input type="text" value={editTitle} onChange={(e) => setEditTitle(e.target.value)} />
              <textarea rows={4} value={editContent} onChange={(e) => setEditContent(e.target.value)} />
              <input type="text" value={editDue} onChange={(e) => setEditDue(e.target.value)} placeholder="截止日期（留空清除）" />
              <input type="text" value={editTags} onChange={(e) => setEditTags(e.target.value)} placeholder="标签（逗号分隔）" />
              <button
                type="button"
                className="primary"
                disabled={busy}
                onClick={() => void handleUpdate({
                  title: editTitle.trim(),
                  content: editContent.trim(),
                  due: editDue.trim() || null,
                  tags: editTags.split(/[,，]/u).map((s) => s.trim()).filter(Boolean),
                }, '已保存修改')}
              >
                保存修改
              </button>
            </div>
          ) : (
            <div style={{ whiteSpace: 'pre-wrap' }}>{selected.content}</div>
          )}

          <div className="row">
            {(['pending', 'in_progress', 'completed', 'cancelled'] as TaskStatus[]).map((st) => (
              <button
                key={st}
                type="button"
                disabled={busy || selected.status === st}
                onClick={() => void handleUpdate({ status: st }, `状态已切换为「${STATUS_LABELS[st]}」`)}
              >
                设为{STATUS_LABELS[st]}
              </button>
            ))}
          </div>

          <form className="col" style={{ marginTop: 6, paddingTop: 8, borderTop: '1px solid var(--border-secondary)' }} onSubmit={(e) => void handleAppendLog(e)}>
            <strong>跟进记录 ({logs.length})</strong>
            <div className="row">
              <div className="row-grow">
                <input
                  type="text"
                  placeholder="输入新的跟进进展（仅追加，不改写历史）…"
                  value={followText}
                  onChange={(e) => setFollowText(e.target.value)}
                />
              </div>
              <button type="submit" className="primary" disabled={busy || !followText.trim()}>追加跟进</button>
            </div>
            <div className="item-list">
              {logs.map((log) => (
                <div key={log.log_id} className="item-card">
                  <div className="mono">r{log.task_revision} · {log.created_at.slice(0, 19).replace('T', ' ')}</div>
                  <div style={{ whiteSpace: 'pre-wrap' }}>{log.content}</div>
                </div>
              ))}
            </div>
          </form>
        </div>
      )}
    </div>
  );
}

export function ReviewsTab({ app }: { app: App }) {
  const [reviews, setReviews] = useState<ReviewSummary[]>([]);
  const [detail, setDetail] = useState<ReviewDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = async () => {
      setLoading(true);
      try {
        const res = await callAppTool<ReviewSummary[]>(app, 'app_reviews_list', { limit: 100 });
        setReviews(res.data);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setLoading(false);
      }
    };
    void load();
  }, []);

  const openReview = async (reviewId: string) => {
    setError(null);
    try {
      const res = await callAppTool<ReviewDetail>(app, 'app_review_get', { review_id: reviewId });
      setDetail(res.data);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="col">
      {error && <div className="banner error">{error}</div>}
      <div className="card">
        <div className="card-title">已保存复盘 ({reviews.length})</div>
        {loading && <div className="muted">加载中…</div>}
        {!loading && reviews.length === 0 && (
          <div className="muted">暂无复盘记录。可在对话中让 AI 基于选定事项生成复盘并保存。</div>
        )}
        <div className="item-list">
          {reviews.map((r) => (
            <div key={r.review_id} className="item-card" style={{ cursor: 'pointer' }} onClick={() => void openReview(r.review_id)}>
              <div className="row" style={{ justifyContent: 'space-between' }}>
                <strong className="mono">{r.review_id}</strong>
                <span className="muted">{r.created_at.slice(0, 19).replace('T', ' ')}</span>
              </div>
            </div>
          ))}
        </div>
      </div>

      {detail && (
        <div className="card">
          <div className="card-title">
            <span>复盘快照</span>
            <button type="button" onClick={() => setDetail(null)}>关闭</button>
          </div>
          {Object.entries(detail.sections).map(([sec, entries]) => (
            <div key={sec} className="col">
              <strong>{SECTION_LABELS[sec] ?? sec}</strong>
              {entries.map((entry, idx) => (
                <div key={idx} className="item-card">
                  <span className={`badge ${entry.kind === 'source_excerpt' ? 'new' : entry.kind === 'ai_inference' ? 'changed' : 'success'}`}>
                    {KIND_LABELS[entry.kind] ?? entry.kind}
                  </span>
                  <div style={{ whiteSpace: 'pre-wrap' }}>{entry.text}</div>
                </div>
              ))}
            </div>
          ))}
          <div className="col" style={{ marginTop: 6 }}>
            <strong>固化事项快照 ({detail.snapshots.length})</strong>
            {detail.snapshots.map((snap) => (
              <div key={snap.task.task_id} className="item-card">
                <strong>{snap.task.title}</strong>
                <div className="mono">{snap.task.task_id} · r{snap.task.revision} · 日志 {snap.logs.length} 条</div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
