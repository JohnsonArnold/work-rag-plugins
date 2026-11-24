import test from 'node:test';
import assert from 'node:assert/strict';
import { WorkStore } from '../src/client/work-store.ts';
import type { StorageLike } from '../src/client/work-store.ts';
import type { WorkApi } from '../src/client/work-api.ts';
import type { Binding, DraftDTO, DraftSave, OperationDTO, TaskDTO, WorkReply } from '../src/shared/contracts.ts';
const binding: Binding = { library_id: 'library_test', restore_generation: 'gen1' };
const taskId = 'task_' + 'b'.repeat(32);
const task: TaskDTO = { task_id: taskId, title: '标题', content: '内容', revision: 3, status: 'pending' };
const operation: OperationDTO = { operation_id: 'op_test', submission_id: 'draft_test', source: 'draft', state: 'SUCCEEDED', binding, result: { task_id: taskId, revision: 4 }, created_at: '', updated_at: '', retryable: false };
const copy = <T>(value: T): T => JSON.parse(JSON.stringify(value));
const reply = (data: unknown): WorkReply => ({ contract_version: '1.0', data, warnings: [], next_cursor: null, binding });
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(r => { resolve = r; }); return { promise, resolve }; }
class MemoryStorage implements StorageLike { rows = new Map<string, string>(); getItem(key: string) { return this.rows.get(key) ?? null; } setItem(key: string, value: string) { this.rows.set(key, value); } }
function fixture() {
  const storage = new MemoryStorage(); const saves: DraftSave[] = []; let rows: DraftDTO[] = [];
  const api: WorkApi = {
    query: async input => reply(input.kind === 'list' ? [task] : input.kind === 'get' ? task : []),
    async submit() { return operation; }, async operation() { return operation; }, async operations() { return [operation]; }, async retry() { return operation; },
    async drafts() { return copy(rows); },
    async saveDraft(input) { saves.push(copy(input)); const existing = rows.find(row => row.key === input.key); const result: DraftDTO = { draft_id: existing?.draft_id ?? 'draft_test', key: input.key!, kind: input.kind, binding, draft_revision: input.expected_draft_revision + 1, payload: copy(input.payload), updated_at: '', ...(input.task_id ? { task_id: input.task_id, base_task_revision: input.base_task_revision } : {}) }; rows = [result]; return copy(result); },
    async discardDraft(id, revision) { const result = { ...rows.find(row => row.draft_id === id)!, deleted: true, draft_revision: revision + 1 }; rows = [result]; return copy(result); },
    async submitDraft(id, revision) { rows = rows.map(row => row.draft_id === id ? { ...row, draft_revision: revision + 1, deleted: true } : row); return operation; },
  };
  const store = new WorkStore('session_test', api, storage);
  return { store, api, storage, saves, setRows(value: DraftDTO[]) { rows = value; }, getRows: () => rows };
}
test('incomplete drafts persist locally, session isolated, formal fields retained', async () => {
  const { store, api, storage, saves } = fixture(); await store.reconnect(); store.activate('create');
  store.edit('create', { content: '半成品\n<script>alert(1)</script>', tags_text: '行政，', tags: ['行政'] });
  assert.equal(new WorkStore('other', api, storage).getSnapshot().drafts.create, undefined);
  assert.equal(new WorkStore('session_test', api, storage).getSnapshot().drafts.create!.payload.content, '半成品\n<script>alert(1)</script>');
  await store.save('create'); assert.equal(saves.length, 1); assert.equal(saves[0]!.payload.title, ''); assert.equal(saves[0]!.payload.due, null); assert.equal(saves[0]!.payload.tags_text, '行政，');
  assert.equal(store.getSnapshot().drafts.create!.dirty, false);
});
test('drafts saved before the rename carry forward under the new cache key', () => {
  const { store, api, storage } = fixture();
  store.activate('create');
  store.edit('create', { title: '升级前的草稿' });
  storage.setItem('it-rag.work:session_test:v1', storage.getItem(store.storageKey)!);
  storage.rows.delete(store.storageKey);

  const upgraded = new WorkStore('session_test', api, storage);
  assert.equal(upgraded.getSnapshot().drafts.create?.payload.title, '升级前的草稿');
  upgraded.edit('create', { title: '升级后的编辑' });
  assert.equal(new WorkStore('session_test', api, storage).getSnapshot().drafts.create?.payload.title, '升级后的编辑');
});
test('typing during in-flight save is never overwritten; next save uses ACK revision', async () => {
  const { store, api, saves } = fixture(); await store.reconnect(); store.activate('create'); store.edit('create', { title: '甲' });
  const save = api.saveDraft; const wait = deferred<DraftDTO>(); api.saveDraft = async input => { const row = await save(input); await wait.promise; return row; };
  const pending = store.save('create'); store.edit('create', { title: '甲乙' }); wait.resolve({} as DraftDTO); await pending;
  assert.equal(store.getSnapshot().drafts.create!.payload.title, '甲乙'); assert.equal(store.getSnapshot().drafts.create!.dirty, true);
  api.saveDraft = save; await store.save('create'); assert.equal(saves[1]!.expected_draft_revision, 1); assert.equal(saves[1]!.payload.title, '甲乙');
});
test('lost save ACK persists frozen payload+mutation id across restart, before newer typing', async () => {
  const { store, api, storage, saves } = fixture(); await store.reconnect(); store.activate('create'); store.edit('create', { title: '旧' });
  const save = api.saveDraft; let first = true; api.saveDraft = async input => { const row = await save(input); if (first) { first = false; throw new Error('transport disconnected'); } return row; };
  await store.save('create'); store.edit('create', { title: '旧+新输入' });
  const frozen = store.getSnapshot().drafts.create!.pending!.input;
  const restored = new WorkStore('session_test', api, storage); await restored.reconnect();
  assert.deepEqual(saves[1], frozen); assert.equal(saves[2]!.payload.title, '旧+新输入'); assert.equal(saves[2]!.expected_draft_revision, 1); assert.equal(restored.getSnapshot().drafts.create!.payload.title, '旧+新输入');
});
test('same-draft remote conflict preserves both versions and explicit merge CAS', async () => {
  const { store, setRows, saves } = fixture(); await store.reconnect(); store.activate('create'); store.edit('create', { title: '本地' });
  setRows([{ draft_id: 'draft_test', kind: 'create', key: 'create', binding, payload: { title: '另一设备' }, draft_revision: 2, updated_at: '' }]);
  await store.reconnect(); const draft = store.getSnapshot().drafts.create!;
  assert.equal(draft.payload.title, '本地'); assert.equal(draft.conflict?.payload.title, '另一设备'); assert.equal(saves.length, 0);
  store.edit('create', { content: '已合并另一设备内容' }); store.resolveConflict('create', 'local'); await store.save('create'); assert.equal(saves[0]!.expected_draft_revision, 2); assert.equal(saves[0]!.payload.title, '本地');
});
test('offline reconnect uploads unsynced local text; restore generation never uploads old cache', async () => {
  const { store, api, storage, saves } = fixture(); await store.reconnect(); store.activate('create'); store.setOnline(false); store.edit('create', { title: '离线输入' }); await store.save('create'); assert.equal(saves.length, 0);
  store.setOnline(true); await store.reconnect(); assert.equal(saves.length, 1);
  store.edit('create', { title: '旧库输入' }); const changed = new WorkStore('session_test', api, storage); api.query = async () => ({ ...reply([]), binding: { ...binding, restore_generation: 'gen2' } }); await changed.reconnect(); await changed.save('create'); assert.equal(saves.length, 1); assert.equal(changed.getSnapshot().drafts.create!.payload.title, '旧库输入'); assert.match(changed.getSnapshot().notice!, /不会自动补传/);
});
test('latest list and detail generations win against out-of-order responses', async () => {
  const { store, api } = fixture(); const first = deferred<WorkReply>(); let count = 0;
  api.query = async () => ++count === 1 ? first.promise : reply([{ ...task, title: '最新' }]);
  const a = store.loadList(); await store.loadList(); first.resolve(reply([{ ...task, title: '过时' }])); await a; assert.equal(store.getSnapshot().tasks[0]!.title, '最新');
  const old = deferred<WorkReply>(); api.query = async input => input.kind === 'get' ? input.task_id === 'old' ? old.promise : reply({ ...task, task_id: 'new' }) : reply([]);
  const selecting = store.select('old'); await store.select('new'); old.resolve(reply({ ...task, task_id: 'old' })); await selecting; assert.equal(store.getSnapshot().selected?.task_id, 'new');
});
test('lost submit ACK with newer typing replays same frozen draft revision on restart', async () => {
  const { store, api, storage } = fixture(); await store.reconnect(); store.activate('create'); store.edit('create', { title: '已提交标题', content: '正文' }); await store.save('create');
  const submit = api.submitDraft; const calls: number[] = []; let first = true;
  api.submitDraft = async (id, revision) => { calls.push(revision); const result = await submit(id, revision); if (first) { first = false; throw new Error('ACK lost'); } return result; };
  await store.submitDraft('create'); store.edit('create', { title: '提交后的新输入' }); await store.save('create');
  assert.equal(store.getSnapshot().drafts.create!.pendingSubmission!.revision, 1);
  const restored = new WorkStore('session_test', api, storage); await restored.reconnect();
  assert.deepEqual(calls, [1, 1]); assert.equal(restored.getSnapshot().drafts.create!.payload.title, '提交后的新输入'); assert.equal(restored.getSnapshot().drafts.create!.pendingSubmission, undefined);
});
test('submit success clears only submitted version, tombstone revision reused for next create', async () => {
  const { store, api, saves } = fixture(); await store.reconnect(); store.activate('create'); store.edit('create', { title: '甲', content: '内容' }); await store.submitDraft('create');
  assert.equal(store.getSnapshot().drafts.create!.payload.title, ''); assert.equal(store.getSnapshot().drafts.create!.revision, 2);
  api.submitDraft = async () => assert.fail('A cleared draft must not be submitted again');
  await store.submitDraft('create');
  assert.equal(store.getSnapshot().drafts.create!.pendingSubmission, undefined);
  store.edit('create', { title: '乙' }); await store.save('create'); assert.equal(saves[1]!.expected_draft_revision, 2);
});
test('definitive draft validation failure permits correction and submission', async () => {
  const { store, api, saves } = fixture();
  await store.reconnect();
  store.activate('create');
  store.edit('create', { content: '先保存半成品' });
  const submit = api.submitDraft;
  api.submitDraft = async () => { throw Object.assign(new Error('title must be a nonempty string'), { layer: 'host', code: 'invalid_input' }); };
  await store.submitDraft('create');
  const rejected = store.getSnapshot().drafts.create!;
  assert.match(rejected.error!, /title/);
  assert.equal(rejected.pendingSubmission, undefined);
  assert.equal(rejected.payload.content, '先保存半成品');
  assert.deepEqual(saves[0]!.binding, binding);

  api.submitDraft = submit;
  store.edit('create', { title: '后补标题' });
  await store.submitDraft('create');
  assert.equal(store.getSnapshot().operations[0]?.state, 'SUCCEEDED');
  assert.equal(saves[1]!.payload.title, '后补标题');
});
test('discard with intervening typing preserves new local content', async () => {
  const { store, api } = fixture(); await store.reconnect(); store.activate('create'); store.edit('create', { title: '旧' }); await store.save('create');
  const discard = api.discardDraft; const wait = deferred<DraftDTO>(); api.discardDraft = async (id, revision, mutation) => { const result = await discard(id, revision, mutation); await wait.promise; return result; };
  const removing = store.discard('create'); store.edit('create', { title: '保留' }); wait.resolve({} as DraftDTO); await removing;
  assert.equal(store.getSnapshot().drafts.create!.payload.title, '保留'); assert.equal(store.getSnapshot().drafts.create!.dirty, true); assert.equal(store.getSnapshot().drafts.create!.revision, 2);
});
test('edit/follow drafts use task revision; no task copied into a fresh create', async () => {
  const { store } = fixture(); await store.reconnect(); await store.select(taskId); store.activate('edit'); let draft = store.getSnapshot().drafts['edit:' + taskId]!; assert.equal(draft.base_task_revision, 3); assert.equal(draft.payload.title, '标题');
  store.activate('follow'); draft = store.getSnapshot().drafts['follow:' + taskId]!; assert.deepEqual(draft.payload, { content: '' });
  store.activate('create'); assert.equal(store.getSnapshot().drafts.create!.task_id, undefined); assert.equal(store.getSnapshot().drafts.create!.payload.title, '');
});
test('explicit library switch archives old local payload and starts fresh current drafts', async () => {
  const { store, api, storage } = fixture(); await store.reconnect(); store.activate('create'); store.edit('create', { title: '旧库珍贵内容' });
  api.query = async () => ({ ...reply([]), binding: { ...binding, restore_generation: 'gen2' } }); await store.reconnect();
  store.adoptCurrentLibrary(); assert.equal(store.getSnapshot().binding?.restore_generation, 'gen2'); assert.equal(store.getSnapshot().drafts.create!.payload.title, '');
  assert.equal(store.getSnapshot().archivedDrafts?.[0]?.drafts.create!.payload.title, '旧库珍贵内容');
  assert.equal(new WorkStore('session_test', api, storage).getSnapshot().archivedDrafts?.[0]?.drafts.create!.payload.title, '旧库珍贵内容');
});
test('fresh browser can read shared drafts from the old library without applying them to the current form', async () => {
  const { store, api, setRows, saves } = fixture();
  api.query = async () => ({ ...reply([]), binding: { ...binding, restore_generation: 'gen2' } });
  setRows([{ draft_id: 'draft_old', kind: 'create', key: 'create', binding, payload: { title: '旧库共享稿' }, draft_revision: 1, updated_at: '' }]);
  await store.reconnect();
  store.activate('create');
  assert.equal(store.getSnapshot().historicalDrafts[0]?.payload.title, '旧库共享稿');
  assert.equal(store.getSnapshot().drafts.create!.payload.title, '');
  assert.equal(saves.length, 0);
});
test('task revision conflict keeps local content until explicit comparison/rebase', async () => {
  const { store, api } = fixture(); await store.reconnect(); await store.select(taskId); store.activate('edit'); const key = 'edit:' + taskId; store.edit(key, { content: '本地尚未保存' });
  api.submitDraft = async () => ({ ...operation, state: 'REJECTED', result: undefined, error: { layer: 'core', code: 'conflict', message: 'changed', details: { actual_revision: 8 } } });
  await store.submitDraft(key); assert.equal(store.getSnapshot().drafts[key]!.payload.content, '本地尚未保存'); assert.equal(store.getSnapshot().drafts[key]!.base_task_revision, 3); assert.match(store.getSnapshot().drafts[key]!.error!, /actual_revision/);
  api.query = async () => reply({ ...task, revision: 8, content: '远端正式内容' }); await store.compareTask(key); assert.equal(store.getSnapshot().drafts[key]!.taskConflict!.content, '远端正式内容');
  store.resolveTaskConflict(key, 'local'); assert.equal(store.getSnapshot().drafts[key]!.base_task_revision, 8); assert.equal(store.getSnapshot().drafts[key]!.payload.content, '本地尚未保存'); assert.equal(store.getSnapshot().drafts[key]!.dirty, true);
});
test('submission in-flight preserves later typing and refreshes follow base revision', async () => {
  const { store, api } = fixture(); await store.reconnect(); await store.select(taskId); store.activate('follow'); const key = 'follow:' + taskId; store.edit(key, { content: '已提交跟进' }); await store.save(key);
  const wait = deferred<OperationDTO>(); const started = deferred<boolean>(); const submit = api.submitDraft; api.submitDraft = async (id, revision) => { started.resolve(true); await wait.promise; return submit(id, revision); };
  const submitting = store.submitDraft(key); await started.promise; store.edit(key, { content: '下一次跟进' }); wait.resolve(operation); await submitting;
  assert.equal(store.getSnapshot().drafts[key]!.payload.content, '下一次跟进'); assert.equal(store.getSnapshot().drafts[key]!.dirty, true); assert.equal(store.getSnapshot().drafts[key]!.base_task_revision, 4);
});
