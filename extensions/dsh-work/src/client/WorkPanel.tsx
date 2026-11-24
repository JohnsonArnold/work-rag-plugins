import React, { useState } from 'react';
import type { WorkApi } from './work-api.ts';
import type { LocalDraft } from './work-store.ts';
import { useWorkController } from './use-work-controller.ts';
export interface WorkPanelProps { sessionId: string; api: WorkApi }
const text = (value: unknown) => typeof value === 'string' ? value : '';
const labels = { pending: '待处理', in_progress: '处理中', completed: '已完成', cancelled: '已取消' };
const block: React.CSSProperties = { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', margin: '6px 0' };
const inputStyle: React.CSSProperties = { width: '100%', boxSizing: 'border-box', padding: '6px', border: '1px solid #8886', borderRadius: 4, background: 'transparent', color: 'inherit' };
function SyncLabel({ draft, online }: { draft: LocalDraft; online: boolean }) {
  return <span role="status">{draft.conflict ? '有其他设备修改' : draft.pendingSubmission ? '正式提交待确认（保留原提交编号）' : draft.pending ? '同步中 / 待确认' : draft.dirty ? online ? '未同步' : '离线未同步' : '已同步'}{draft.draft_id ? ` · 草稿 r${draft.revision}` : ' · 本地草稿'}</span>;
}
export function WorkPanel({ sessionId, api }: WorkPanelProps) {
  const { state, store } = useWorkController(sessionId, api);
  const [search, setSearch] = useState('');
  const draft = state.drafts[state.activeKey];
  const selected = state.selected;
  const contentLabel = draft?.kind === 'follow' ? '跟进内容' : '内容';
  return <section aria-label="事项" style={{ padding: 12, display: 'flex', flexDirection: 'column', gap: 10, fontSize: 13, overflowY: 'auto', height: '100%', boxSizing: 'border-box' }}>
    <header><strong>事项</strong> <small>直接操作本地事项库 · 不调用模型</small></header>
    <nav style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}><button onClick={() => store.activate('create')}>新建事项</button><button onClick={() => void store.reconnect()}>刷新 / 同步</button><button onClick={() => void store.loadOperations()}>操作回执</button></nav>
    {!state.online && <p role="status">离线：输入保存在此设备，联网后补传。</p>}
    {state.storageError && <p role="alert">{state.storageError}</p>}
    {state.error && <p role="alert" style={block}>{state.error}</p>}
    {state.notice && <p role="status" style={block}>{state.notice}</p>}
    {state.availableBinding && <button onClick={() => store.adoptCurrentLibrary()}>只读保留旧稿，切换当前资料库</button>}
    {!!state.archivedDrafts?.length && <details><summary>旧资料库草稿（只读，可复制；不自动提交）</summary>{state.archivedDrafts.map((archive, index) => <section key={index}><small>{archive.binding.library_id} / {archive.binding.restore_generation}</small>{Object.values(archive.drafts).map(row => <div key={row.key}><strong>{row.key}</strong><pre style={block}>{JSON.stringify(row.payload, null, 2)}</pre>{row.pendingSubmission && <small>原提交待确认：{row.pendingSubmission.draft_id} r{row.pendingSubmission.revision}</small>}</div>)}</section>)}</details>}
    {state.historicalDrafts.length > 0 && <details><summary>旧资料库共享草稿</summary>{state.historicalDrafts.map(row => <section key={row.draft_id}><strong>{row.key}</strong><small> · 草稿 r{row.draft_revision}</small><pre style={block}>{JSON.stringify(row.payload, null, 2)}</pre></section>)}</details>}
    <form onSubmit={event => { event.preventDefault(); store.setFilter({ query: search }); }} style={{ display: 'flex', gap: 6 }}>
      <select aria-label="事项筛选" value={state.view} onChange={event => store.setFilter({ view: event.target.value as typeof state.view })}><option value="unfinished">未完成</option><option value="completed">已完成</option><option value="all">全部</option><option value="cancelled">已取消</option></select>
      <input aria-label="关键词" placeholder="关键词" value={search} onChange={event => setSearch(event.target.value)} style={{ ...inputStyle, minWidth: 0 }} /><button type="submit">查找</button>
    </form>
    <div aria-busy={state.loading}>
      {state.loading && <p>加载中…</p>}
      {!state.loading && !state.tasks.length && !state.error && <p>此页没有事项。</p>}
      <ul style={{ paddingLeft: 18 }}>{state.tasks.map(task => <li key={task.task_id}><button style={{ textAlign: 'left', maxWidth: '100%', overflowWrap: 'anywhere' }} onClick={() => void store.select(task.task_id)}>{task.title} · {labels[task.status]}</button><small style={{ display: 'block', overflowWrap: 'anywhere' }}>{task.task_id} · r{task.revision}</small></li>)}</ul>
      <button disabled={state.offset === 0} onClick={() => store.setFilter({ offset: Math.max(0, state.offset - state.limit) })}>上一页</button> <span>{state.tasks.length ? `${state.offset + 1}–${state.offset + state.tasks.length}` : '0 条'}</span> <button disabled={state.tasks.length < state.limit} onClick={() => store.setFilter({ offset: state.offset + state.limit })}>下一页</button>
      {state.warnings.map((warning, index) => <p key={index} style={block}>{typeof warning === 'string' ? warning : JSON.stringify(warning)}</p>)}
    </div>
    {selected && <article aria-label="事项详情" style={{ borderTop: '1px solid #8885', paddingTop: 10 }}>
      <h3 style={block}>{selected.title}</h3><p style={block}>{selected.content}</p><small style={block}>{selected.task_id} · {labels[selected.status]} · r{selected.revision}<br />截止：{selected.due ?? '无'} · 标签：{selected.tags?.join('、') ?? '无'}</small>
      <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap' }}><button onClick={() => store.activate('edit')}>编辑事项</button><button onClick={() => store.activate('follow')}>追加跟进</button>{selected.status !== 'cancelled' && <><button disabled={selected.status === 'in_progress'} onClick={() => void store.status('in_progress')}>开始</button><button disabled={selected.status === 'completed'} onClick={() => void store.status('completed')}>完成</button><button disabled={selected.status === 'pending'} onClick={() => void store.status('pending')}>重开</button></>}</div>
      <h4>跟进记录 <small>详情与日志分别读取</small></h4>{state.logs.map(log => <div key={log.log_id}><small>{log.log_id} · r{log.task_revision}</small><p style={block}>{log.content}</p></div>)}{state.logsHasMore && <button onClick={() => void store.loadLogs()}>更多跟进</button>}
    </article>}
    {draft && <form aria-label="事项草稿" onSubmit={event => { event.preventDefault(); void store.submitDraft(draft.key); }} style={{ display: 'flex', flexDirection: 'column', gap: 7, borderTop: '1px solid #8885', paddingTop: 10 }}>
      <strong>{draft.kind === 'create' ? '新建事项' : draft.kind === 'edit' ? '编辑事项' : '追加跟进'}</strong>
      {draft.task_id && <small style={block}>{draft.task_id} · 基于事项 r{draft.base_task_revision}</small>}
      <SyncLabel draft={draft} online={state.online} />
      {draft.kind !== 'follow' && <label>标题<input style={inputStyle} value={text(draft.payload.title)} onChange={event => store.edit(draft.key, { title: event.target.value })} /></label>}
      <label>{contentLabel}<textarea aria-label={contentLabel} rows={6} style={inputStyle} value={text(draft.payload.content)} onChange={event => store.edit(draft.key, { content: event.target.value })} /></label>
      {draft.kind !== 'follow' && <><label>截止（可空，日期或带时区 ISO 时间）<input style={inputStyle} placeholder="2026-11-30 或 2026-11-30T09:00:00+08:00" value={text(draft.payload.due)} onChange={event => store.edit(draft.key, { due: event.target.value || null })} /></label><label>标签（逗号分隔）<input style={inputStyle} value={text(draft.payload.tags_text) || (Array.isArray(draft.payload.tags) ? draft.payload.tags.join(', ') : '')} onChange={event => store.edit(draft.key, { tags_text: event.target.value, tags: event.target.value.split(/[,，]/u).map(value => value.trim()).filter(Boolean) })} /></label></>}
      {draft.error && <p role="alert" style={block}>{draft.error}</p>}
      {draft.error && draft.kind !== 'create' && !draft.pendingSubmission && <button type="button" onClick={() => void store.compareTask(draft.key)}>读取当前事项，比较修订冲突</button>}
      {draft.taskConflict && <div role="alert"><strong>当前事项 r{draft.taskConflict.revision}（本地草稿仍保留在上方）</strong><p style={block}>{draft.taskConflict.title}</p><p style={block}>{draft.taskConflict.content}</p><button type="button" onClick={() => store.resolveTaskConflict(draft.key, 'local')}>保留本地内容，明确采用当前事项修订</button>{draft.kind === 'edit' && <button type="button" onClick={() => store.resolveTaskConflict(draft.key, 'server')}>使用当前事项内容作为编辑稿</button>}</div>}
      {draft.conflict && <div role="alert"><strong>同一草稿已被其他设备修改，两份均已保留。</strong><p>本地：</p><pre style={block}>{JSON.stringify(draft.payload, null, 2)}</pre><p>共享稿{draft.conflict.deleted ? '（已处理/丢弃）' : ''}：</p><pre style={block}>{JSON.stringify(draft.conflict.payload, null, 2)}</pre><button type="button" onClick={() => store.resolveConflict(draft.key, 'server')}>使用共享稿</button> <button type="button" onClick={() => store.resolveConflict(draft.key, 'local')}>保留本地并基于新修订保存</button><p>也可先在上方编辑框手工合并，再保留本地。</p></div>}
      <div style={{ display: 'flex', gap: 6 }}><button type="button" disabled={!state.online || !!draft.conflict || draft.submitting} onClick={() => void store.save(draft.key)}>同步草稿</button><button type="submit" disabled={!state.online || !!draft.conflict || draft.submitting}>{draft.submitting ? '提交中…' : draft.pendingSubmission ? '确认原提交' : '正式提交'}</button><button type="button" disabled={!!draft.pending || !!draft.pendingSubmission || draft.submitting} onClick={() => void store.discard(draft.key)}>丢弃草稿</button></div>
      <small>半成品也自动保存，正式提交时才校验。跟进不会自动切换状态。</small>
    </form>}
    {Object.values(state.drafts).filter(row => row.key !== state.activeKey && (row.dirty || row.draft_id && !row.server?.deleted)).map(row => <button key={row.key} onClick={() => store.activate(row.kind, row.task_id ? { task_id: row.task_id, title: '', content: '', status: 'pending', revision: row.base_task_revision ?? 1 } : undefined)}>接续草稿：{row.key}{row.dirty ? '（未同步）' : ''}</button>)}
    {!!state.operations.length && <section aria-label="操作回执"><h4>最近操作</h4>{state.operations.map(operation => <div key={operation.operation_id} style={{ borderTop: '1px solid #8883', padding: '5px 0' }}><small style={block}>{operation.operation_id} · {operation.state}</small>{operation.result && <p style={block}>{operation.result.task_id} · r{operation.result.revision}{operation.result.log_id ? ' · ' + operation.result.log_id : ''}</p>}{operation.error && <p role="alert" style={block}>{operation.error.code}: {operation.error.message}{operation.error.details ? '\n' + JSON.stringify(operation.error.details) : ''}</p>}{operation.retryable && <button onClick={() => void store.retry(operation.operation_id)}>确认 / 恢复原提交</button>}</div>)}</section>}
  </section>;
}
export default WorkPanel;
