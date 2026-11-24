import type { Binding, DraftDTO, DraftKind, DraftSave, OperationDTO, TaskDTO, TaskLogDTO, TaskStatus } from '../shared/contracts.ts';
import type { WorkApi } from './work-api.ts';
import { workErrorText } from './work-api.ts';
export interface StorageLike { getItem(key: string): string | null; setItem(key: string, value: string): void }
export type PendingSave = { input: DraftSave; version: number };
export type LocalDraft = {
  key: string; kind: DraftKind; task_id?: string; draft_id?: string; revision: number; base_task_revision?: number;
  payload: Record<string, unknown>; version: number; dirty: boolean; pending?: PendingSave;
  server?: DraftDTO; conflict?: DraftDTO; taskConflict?: TaskDTO; error?: string; submitting?: boolean; operation?: OperationDTO;
  pendingSubmission?: { draft_id: string; revision: number; version: number };
};
export type WorkState = {
  binding?: Binding; availableBinding?: Binding; archivedDrafts?: { binding: Binding; drafts: Record<string, LocalDraft> }[];
  historicalDrafts: DraftDTO[];
  drafts: Record<string, LocalDraft>; tasks: TaskDTO[]; selected?: TaskDTO; logs: TaskLogDTO[];
  view: 'unfinished' | 'completed' | 'all' | 'cancelled'; query: string; offset: number; limit: number;
  activeKey: string; online: boolean; loading: boolean; error?: string; notice?: string; warnings: unknown[];
  operations: OperationDTO[]; logsAfter: number; logsHasMore: boolean; storageError?: string;
};
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;
const sameBinding = (a?: Binding, b?: Binding) => a?.library_id === b?.library_id && a?.restore_generation === b?.restore_generation;
const id = () => `browser_${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}_${Math.random().toString(16).slice(2)}`}`;
const rejectedDraftSubmission = new Set(['invalid_input', 'draft_conflict', 'draft_not_found', 'binding_mismatch']);
export const draftKey = (kind: DraftKind, taskId?: string) => kind === 'create' ? 'create' : `${kind}:${taskId}`;

/** Framework-independent controller: observable state + persisted frozen save requests. */
export class WorkStore {
  private state: WorkState;
  private listeners = new Set<() => void>();
  private saving = new Map<string, Promise<void>>();
  private listGeneration = 0; private detailGeneration = 0; private refreshGeneration = 0;
  private disposed = false;
  readonly storageKey: string;
  readonly sessionId: string;
  readonly api: WorkApi;
  private storage?: StorageLike;
  constructor(sessionId: string, api: WorkApi, storage?: StorageLike) {
    this.sessionId = sessionId; this.api = api; this.storage = storage;
    const sessionKey = `${encodeURIComponent(sessionId)}:v1`;
    this.storageKey = `work-rag.work:${sessionKey}`;
    this.state = { drafts: {}, historicalDrafts: [], tasks: [], logs: [], view: 'unfinished', query: '', offset: 0, limit: 20, activeKey: 'create', online: true, loading: false, warnings: [], operations: [], logsAfter: 0, logsHasMore: false };
    if (!storage) this.state.storageError = '浏览器本地存储不可用；未同步内容仅在当前页面内存中，请勿关闭页面。';
    try {
      const raw = storage?.getItem(this.storageKey) ?? storage?.getItem(`it-rag.work:${sessionKey}`);
      if (raw) { const saved = JSON.parse(raw); if (saved.sessionId === sessionId && saved.drafts && typeof saved.drafts === 'object') { this.state.drafts = saved.drafts; this.state.binding = saved.binding; this.state.archivedDrafts = saved.archivedDrafts; for (const d of Object.values(this.state.drafts)) d.submitting = false; } }
    } catch { this.state.storageError = '本地草稿缓存不可读；当前编辑仍保留在内存。'; }
  }
  getSnapshot = (): WorkState => this.state;
  subscribe = (listener: () => void): (() => void) => { this.listeners.add(listener); return () => this.listeners.delete(listener); };
  private emit(persist = true): void {
    if (persist) try { this.storage?.setItem(this.storageKey, JSON.stringify({ sessionId: this.sessionId, binding: this.state.binding, drafts: this.state.drafts, archivedDrafts: this.state.archivedDrafts })); }
    catch { this.state.storageError = '浏览器存储不可用；请勿关闭尚未同步的草稿。'; }
    this.state = { ...this.state, drafts: { ...this.state.drafts } };
    if (!this.disposed) this.listeners.forEach(listener => listener());
  }
  resume(): void { this.disposed = false; }
  dispose(): void { this.disposed = true; this.listGeneration++; this.detailGeneration++; this.refreshGeneration++; this.listeners.clear(); }
  setOnline(online: boolean): void { this.state.online = online; this.emit(false); }
  ensureDraft(kind: DraftKind, task?: TaskDTO): LocalDraft {
    if (kind === 'create') task = undefined;
    const key = draftKey(kind, task?.task_id); let draft = this.state.drafts[key];
    if (!draft) {
      draft = { key, kind, ...(task ? { task_id: task.task_id, base_task_revision: task.revision } : {}), revision: 0, payload: kind === 'follow' ? { content: '' } : task ? { title: task.title, content: task.content, due: task.due ?? null, tags: task.tags ?? [] } : { title: '', content: '', due: null, tags: [] }, version: 0, dirty: false };
      this.state.drafts[key] = draft;
    } else if (task && !draft.dirty && !draft.pending && !draft.pendingSubmission && (draft.server?.deleted || !draft.draft_id)) {
      draft.base_task_revision = task.revision;
      if (kind === 'edit') draft.payload = { title: task.title, content: task.content, due: task.due ?? null, tags: task.tags ?? [] };
    }
    return draft;
  }
  activate(kind: DraftKind, task = this.state.selected): void { if (kind !== 'create' && !task) return; this.state.activeKey = this.ensureDraft(kind, task).key; this.emit(); }
  edit(key: string, patch: Record<string, unknown>): void {
    const draft = this.state.drafts[key]; if (!draft) return;
    draft.payload = { ...draft.payload, ...patch }; draft.version++; draft.dirty = true; draft.error = undefined; this.emit();
  }
  async loadList(): Promise<void> {
    const generation = ++this.listGeneration; const { view, query, limit, offset } = this.state;
    this.state.loading = true; this.emit(false);
    try {
      const reply = await this.api.query({ kind: 'list', ...(view === 'unfinished' ? { view } : view === 'all' ? {} : { status: view }), query, limit, offset });
      if (this.disposed || generation !== this.listGeneration) return;
      this.acceptBinding(reply.binding); this.state.tasks = reply.data as TaskDTO[]; this.state.warnings = reply.warnings; this.state.error = undefined;
    } catch (error) { if (generation === this.listGeneration && !this.disposed) this.state.error = `列表读取失败：${workErrorText(error)}`; }
    finally { if (generation === this.listGeneration && !this.disposed) { this.state.loading = false; this.emit(); } }
  }
  setFilter(patch: Partial<Pick<WorkState, 'view' | 'query' | 'offset'>>): void { Object.assign(this.state, patch); if (patch.query !== undefined || patch.view !== undefined) this.state.offset = 0; this.emit(false); void this.loadList(); }
  async select(taskId: string): Promise<void> {
    const generation = ++this.detailGeneration; this.state.selected = undefined; this.state.logs = []; this.state.logsAfter = 0; this.emit(false);
    try {
      const reply = await this.api.query({ kind: 'get', task_id: taskId });
      if (this.disposed || generation !== this.detailGeneration) return;
      this.acceptBinding(reply.binding); this.state.selected = reply.data as TaskDTO; this.state.error = undefined; this.emit();
      await this.loadLogs(false, generation);
    } catch (error) { if (generation === this.detailGeneration && !this.disposed) { this.state.error = `详情读取失败：${workErrorText(error)}`; this.emit(false); } }
  }
  async loadLogs(append = true, generation = this.detailGeneration): Promise<void> {
    const taskId = this.state.selected?.task_id; if (!taskId) return;
    const after = append ? this.state.logsAfter : 0;
    try {
      const reply = await this.api.query({ kind: 'logs', task_id: taskId, after, limit: 50 });
      if (this.disposed || generation !== this.detailGeneration || taskId !== this.state.selected?.task_id) return;
      if (!sameBinding(reply.binding, this.state.binding)) { this.acceptBinding(reply.binding); this.emit(); return; }
      const rows = reply.data as TaskLogDTO[];
      this.state.logs = append ? [...this.state.logs, ...rows.filter(row => !this.state.logs.some(old => old.log_id === row.log_id))] : rows;
      this.state.logsAfter = rows.length ? rows[rows.length - 1]!.task_revision : after; this.state.logsHasMore = rows.length === 50; this.emit(false);
    } catch (error) { if (generation === this.detailGeneration && !this.disposed) { this.state.error = `跟进读取失败：${workErrorText(error)}`; this.emit(false); } }
  }
  private acceptBinding(binding: Binding): void {
    if (this.state.binding && !sameBinding(this.state.binding, binding)) {
      // Never replay a cached request against a restored/replaced library.
      for (const draft of Object.values(this.state.drafts)) { draft.error = '资料库代次已变更；旧稿保留，请复制内容为当前库新稿。'; }
      this.state.availableBinding = binding;
      this.state.notice = '资料库已切换：旧草稿不会自动补传。';
      return;
    }
    this.state.binding = binding;
  }
  adoptCurrentLibrary(): void {
    if (!this.state.availableBinding || !this.state.binding) return;
    this.state.archivedDrafts = [...(this.state.archivedDrafts ?? []), { binding: clone(this.state.binding), drafts: clone(this.state.drafts) }];
    this.state.binding = this.state.availableBinding; this.state.availableBinding = undefined;
    this.detailGeneration++;
    this.state.drafts = {}; this.state.activeKey = 'create'; this.state.selected = undefined; this.state.logs = [];
    this.state.notice = '已切换当前资料库；旧稿在下方只读保留，不自动重放。';
    this.activate('create'); void this.reconnect();
  }
  private applyRemote(remote: DraftDTO): void {
    if (!sameBinding(remote.binding, this.state.binding)) return;
    const key = remote.key; const local = this.state.drafts[key];
    if (local?.pending || local?.pendingSubmission) return; // Lost ACK must replay its frozen request before checking current CAS.
    if (local?.dirty) { if (remote.draft_revision !== local.revision || !sameBinding(remote.binding, this.state.binding)) local.conflict = clone(remote); return; }
    if (!sameBinding(remote.binding, this.state.binding)) return;
    if (remote.deleted) {
      this.state.drafts[key] = { key, kind: remote.kind, task_id: remote.task_id, draft_id: remote.draft_id, revision: remote.draft_revision, base_task_revision: remote.base_task_revision, payload: remote.kind === 'follow' ? { content: '' } : { title: '', content: '', due: null, tags: [] }, version: (local?.version ?? 0) + 1, dirty: false, server: clone(remote) };
      return;
    }
    this.state.drafts[key] = { key, kind: remote.kind, task_id: remote.task_id, draft_id: remote.draft_id, revision: remote.draft_revision, base_task_revision: remote.base_task_revision, payload: clone(remote.payload), version: (local?.version ?? 0) + 1, dirty: false, server: clone(remote) };
  }
  async reconnect(): Promise<void> {
    const generation = ++this.refreshGeneration;
    await this.loadList();
    if (this.disposed || generation !== this.refreshGeneration || !this.state.online || !this.state.binding || this.state.notice?.startsWith('资料库已切换')) return;
    try {
      // Replay before pulling a newer remote revision, including requests persisted before browser restart.
      for (const draft of Object.values(this.state.drafts)) {
        if (draft.pendingSubmission) await this.submitDraft(draft.key);
        else if (draft.pending) await this.save(draft.key);
      }
      const remote = await this.api.drafts();
      if (this.disposed || generation !== this.refreshGeneration) return;
      this.state.historicalDrafts = remote.filter(draft => !draft.deleted && !sameBinding(draft.binding, this.state.binding));
      for (const draft of remote) this.applyRemote(draft);
      this.emit();
      for (const draft of Object.values(this.state.drafts)) if (draft.dirty && !draft.conflict && !draft.error) await this.save(draft.key);
    } catch (error) { if (!this.disposed && generation === this.refreshGeneration) { this.state.error = `草稿同步失败：${workErrorText(error)}`; this.emit(); } }
  }
  save(key: string): Promise<void> {
    const existing = this.saving.get(key); if (existing) return existing;
    const promise = this.saveOnce(key).finally(() => { this.saving.delete(key); }); this.saving.set(key, promise); return promise;
  }
  private async saveOnce(key: string): Promise<void> {
    const draft = this.state.drafts[key];
    if (!draft || (!draft.dirty && !draft.pending) || draft.conflict || draft.submitting || draft.pendingSubmission || !this.state.online || !this.state.binding || this.state.notice?.startsWith('资料库已切换')) return;
    if (!draft.pending) {
      // Host drafts accept UI-only fields and project formal fields on submission.
      // Preserve raw tag input (including a trailing separator) across save ACKs.
      const payload = clone(draft.payload);
      draft.pending = { version: draft.version, input: { kind: draft.kind, key: draft.key, ...(draft.draft_id ? { draft_id: draft.draft_id } : {}), ...(draft.task_id ? { task_id: draft.task_id } : {}), expected_draft_revision: draft.revision, mutation_id: id(), payload, binding: this.state.binding, ...(draft.base_task_revision === undefined ? {} : { base_task_revision: draft.base_task_revision }) } };
    }
    const pending = draft.pending; draft.error = undefined; this.emit();
    try {
      const remote = await this.api.saveDraft(clone({ ...pending.input, binding: pending.input.binding ?? this.state.binding }));
      if (this.disposed || this.state.drafts[key] !== draft) return;
      draft.draft_id = remote.draft_id; draft.revision = remote.draft_revision; draft.server = clone(remote); draft.pending = undefined;
      draft.dirty = draft.version !== pending.version;
      if (!draft.dirty) draft.payload = clone(remote.payload);
      this.emit();
    } catch (error) {
      if (this.disposed) return;
      draft.error = workErrorText(error); this.emit();
      // Keep frozen request on transport failure; if CAS failed retain both versions for explicit merge.
      try {
        const remote = (await this.api.drafts()).find(row => row.key === key && sameBinding(row.binding, this.state.binding));
        if (remote && remote.draft_revision !== pending.input.expected_draft_revision && /conflict|revision|修订|冲突/iu.test(draft.error)) { draft.pending = undefined; draft.conflict = clone(remote); this.emit(); }
      } catch { /* Offline: frozen payload remains durable for exact replay. */ }
    }
  }
  resolveConflict(key: string, choice: 'local' | 'server'): void {
    const draft = this.state.drafts[key]; const remote = draft?.conflict; if (!draft || !remote) return;
    if (!sameBinding(remote.binding, this.state.binding)) { draft.error = '不同资料库的草稿不能直接覆盖；请复制为新稿。'; this.emit(); return; }
    if (choice === 'server') { draft.dirty = false; draft.pending = undefined; draft.conflict = undefined; this.applyRemote(remote); }
    else { draft.revision = remote.draft_revision; draft.draft_id = remote.draft_id; draft.server = clone(remote); draft.pending = undefined; draft.conflict = undefined; draft.error = undefined; draft.dirty = true; draft.version++; }
    this.emit();
  }
  async compareTask(key: string): Promise<void> {
    const draft = this.state.drafts[key]; if (!draft?.task_id || draft.pendingSubmission) return;
    try {
      const reply = await this.api.query({ kind: 'get', task_id: draft.task_id });
      if (!sameBinding(reply.binding, this.state.binding)) { this.acceptBinding(reply.binding); this.emit(); return; }
      if (this.state.drafts[key] === draft) { draft.taskConflict = reply.data as TaskDTO; this.emit(); }
    } catch (error) { draft.error = workErrorText(error); this.emit(); }
  }
  resolveTaskConflict(key: string, choice: 'local' | 'server'): void {
    const draft = this.state.drafts[key]; const task = draft?.taskConflict;
    if (!draft || !task || draft.pendingSubmission || draft.pending) return;
    if (choice === 'server' && draft.kind === 'edit') draft.payload = { title: task.title, content: task.content, due: task.due ?? null, tags: task.tags ?? [] };
    draft.base_task_revision = task.revision; draft.taskConflict = undefined; draft.error = undefined; draft.dirty = true; draft.version++; this.emit();
  }
  async discard(key: string): Promise<void> {
    const draft = this.state.drafts[key]; if (!draft) return;
    if (draft.pending || draft.submitting || draft.pendingSubmission) { draft.error = '请先确认正在同步或提交的操作，避免丢稿。'; this.emit(); return; }
    const version = draft.version;
    try {
      const remote = draft.draft_id ? await this.api.discardDraft(draft.draft_id, draft.revision, id()) : undefined;
      if (this.state.drafts[key] === draft) {
        if (remote) { draft.revision = remote.draft_revision; draft.server = clone(remote); }
        if (draft.version === version) {
          draft.dirty = false;
          if (remote) this.applyRemote(remote); else delete this.state.drafts[key];
        }
      }
      this.emit();
    } catch (error) { draft.error = workErrorText(error); this.emit(); }
  }
  async submitDraft(key: string): Promise<void> {
    let draft = this.state.drafts[key];
    if (!draft || draft.submitting || draft.conflict || !this.state.online || this.state.availableBinding) return;
    if (!draft.pendingSubmission) {
      await this.save(key); draft = this.state.drafts[key];
      // If typing raced the save, save the new version before freezing submission.
      if (draft?.dirty && !draft.pending && !draft.error) await this.save(key);
      if (!draft || draft.dirty || draft.pending || draft.conflict || !draft.draft_id || draft.server?.deleted) return;
      draft.pendingSubmission = { draft_id: draft.draft_id, revision: draft.revision, version: draft.version };
    }
    const { version, revision, draft_id } = draft.pendingSubmission;
    draft.submitting = true; this.emit();
    try {
      const operation = await this.api.submitDraft(draft_id, revision);
      if (this.disposed) return;
      draft.operation = operation; this.state.operations = [operation, ...this.state.operations.filter(old => old.operation_id !== operation.operation_id)];
      if (operation.state === 'SUCCEEDED') {
        draft.pendingSubmission = undefined;
        this.state.notice = `已保存 ${operation.result?.task_id ?? ''} r${operation.result?.revision ?? ''}；operation ${operation.operation_id}`;
        // Tombstone advances the shared CAS revision; do not reset it to zero.
        let remote: DraftDTO | undefined;
        try { remote = (await this.api.drafts()).find(row => row.draft_id === draft.draft_id); }
        catch (error) { this.state.notice += `；共享草稿刷新失败（提交已成功）：${workErrorText(error)}`; }
        if (remote) { draft.revision = remote.draft_revision; draft.server = clone(remote); }
        else { draft.error = '提交已成功，草稿清理修订尚未确认。请刷新后继续同步。'; }
        if (draft.version === version) {
          draft.dirty = false;
          if (remote) this.applyRemote(remote);
          else { draft.payload = draft.kind === 'follow' ? { content: '' } : { title: '', content: '', due: null, tags: [] }; draft.version++; }
        } else { draft.pending = undefined; draft.dirty = true; draft.error = undefined; }
        if (draft.kind !== 'create' && operation.result) this.state.drafts[key]!.base_task_revision = operation.result.revision;
        await this.loadList();
        const selectedId = this.state.selected?.task_id;
        if (selectedId && selectedId === operation.result?.task_id) await this.select(selectedId);
      } else {
        if (operation.state === 'REJECTED') draft.pendingSubmission = undefined;
        draft.error = operation.error ? `${operation.error.code}: ${operation.error.message}${operation.error.details ? ' ' + JSON.stringify(operation.error.details) : ''}` : `提交结果 ${operation.state}；请查询/恢复原 operation ${operation.operation_id}`;
      }
    } catch (error) {
      const failure = error as { layer?: string; code?: string; details?: { draft?: DraftDTO } } | undefined;
      if (failure?.layer === 'host' && failure.code && rejectedDraftSubmission.has(failure.code)) {
        draft.pendingSubmission = undefined;
        draft.error = workErrorText(error);
        if (failure.code === 'draft_conflict' && failure.details?.draft) draft.conflict = failure.details.draft;
      } else {
        draft.error = `提交回执未确认：${workErrorText(error)}。可再次提交同一草稿修订确认，勿另建事项。`;
      }
    }
    finally { draft.submitting = false; this.emit(); }
  }
  async status(status: Exclude<TaskStatus, 'cancelled'>): Promise<void> {
    const { selected: task, binding, online, availableBinding } = this.state;
    if (!task || !binding || !online || availableBinding) return;
    // Persist a local submission token before transport, so a lost ACK never makes a new operation.
    const key = `status:${binding.library_id}:${binding.restore_generation}:${task.task_id}:${task.revision}:${status}`;
    const localKey = this.storageKey + ':' + key;
    let submissionId: string;
    try { if (!this.storage) throw new Error('Storage unavailable'); submissionId = this.storage.getItem(localKey) ?? id(); this.storage.setItem(localKey, submissionId); }
    catch { this.state.error = '无法持久保存提交编号；请恢复浏览器存储后再切换状态。'; this.emit(false); return; }
    try {
      const operation = await this.api.submit({ kind: 'set_status', task_id: task.task_id, expected_revision: task.revision, status }, submissionId, binding);
      this.state.operations = [operation, ...this.state.operations.filter(row => row.operation_id !== operation.operation_id)];
      this.state.notice = `状态操作 ${operation.state}：${operation.operation_id}${operation.error ? ' ' + operation.error.message : ''}`;
      if (operation.state === 'SUCCEEDED') { await this.loadList(); if (this.state.selected?.task_id === task.task_id) await this.select(task.task_id); }
      this.emit();
    } catch (error) { this.state.error = `状态提交待确认，重试会沿用原提交编号：${workErrorText(error)}`; this.emit(false); }
  }
  async loadOperations(): Promise<void> { try { this.state.operations = await this.api.operations(50, 0); this.emit(false); } catch (error) { this.state.error = workErrorText(error); this.emit(false); } }
  async retry(operationId: string): Promise<void> { try { const op = await this.api.retry(operationId); this.state.operations = [op, ...this.state.operations.filter(old => old.operation_id !== op.operation_id)]; this.state.notice = `${op.operation_id}: ${op.state}${op.error ? ' ' + op.error.message : ''}`; this.emit(); await this.loadList(); } catch (error) { this.state.error = workErrorText(error); this.emit(false); } }
}
