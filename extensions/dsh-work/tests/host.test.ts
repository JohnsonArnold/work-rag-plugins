import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { Binding, BridgeExecutor, BridgeRequest, BridgeResponse, FrozenOperation, MutationAck, WorkMutation } from '../src/shared/contracts.ts';
import { bindingKey, parseResponse, WorkServiceError } from '../src/shared/schema.ts';
import { atomicWrite, readJSON } from '../src/host/atomic-store.ts';
import { BridgeRunner } from '../src/host/bridge-runner.ts';
import { OperationLedger } from '../src/host/operation-ledger.ts';
import { WorkService } from '../src/host/work-service.ts';
import { DraftStore } from '../src/host/draft-store.ts';
import { DraftService } from '../src/host/draft-service.ts';

class FakeRunner implements BridgeExecutor {
  binding: Binding = { library_id: 'library_test', restore_generation: 'generation_1' };
  tasks = new Map<string, { task_id: string; title: string; content: string; revision: number; status: string }>();
  replay = new Map<string, MutationAck>();
  writes: BridgeRequest[] = []; loseAck = false;
  onWrite?: () => Promise<void>;
  modelCalls = 0;
  model() { this.modelCalls++; throw new Error('MODEL TRAP'); }
  async run(request: BridgeRequest): Promise<BridgeResponse> {
    const ok = (data: unknown): BridgeResponse => ({ protocol_version: '1', call_id: request.call_id, binding: this.binding, ok: true, result: { contract_version: '1.0', data, warnings: [], next_cursor: null } });
    const fail = (code: string): BridgeResponse => ({ protocol_version: '1', call_id: request.call_id, ok: false, error: { layer: 'core', code, message: code, details: { actual_revision: 7 } } });
    if (request.operation === 'task.list') return ok([...this.tasks.values()]);
    if (request.operation === 'task.get') return this.tasks.has(String(request.params.task_id)) ? ok(this.tasks.get(String(request.params.task_id))) : fail('not_found');
    if (request.operation === 'task.logs') return ok([]);
    this.writes.push(structuredClone(request));
    if (JSON.stringify(request.binding) !== JSON.stringify(this.binding)) return fail('binding_mismatch');
    const requestId = String(request.params.request_id);
    if (this.replay.has(requestId)) return ok(this.replay.get(requestId));
    let ack: MutationAck;
    if (request.operation === 'task.create') {
      const task_id = `task_${this.tasks.size + 1}`;
      this.tasks.set(task_id, { task_id, title: String(request.params.title), content: String(request.params.content), revision: 1, status: 'pending' });
      ack = { task_id, revision: 1, status: 'pending' };
    } else {
      const task = this.tasks.get(String(request.params.task_id));
      if (!task) return fail('not_found');
      if (task.revision !== request.params.expected_revision) return fail('revision_conflict');
      if (request.operation === 'task.update') Object.assign(task, request.params.changes);
      task.revision++;
      ack = { task_id: task.task_id, revision: task.revision, status: task.status as MutationAck['status'], ...(request.operation === 'task.append_log' ? { log_id: `log_${task.revision}` } : {}) };
    }
    this.replay.set(requestId, ack);
    await this.onWrite?.();
    if (this.loseAck) { this.loseAck = false; throw new WorkServiceError('timeout', 'lost acknowledgement'); }
    return ok(ack);
  }
}
async function setup() {
  const root = await mkdtemp(join(tmpdir(), 'dsh-work-test-'));
  const runner = new FakeRunner(); const ledger = new OperationLedger(join(root, 'receipts'));
  const work = new WorkService(runner, ledger); const store = new DraftStore(join(root, 'drafts'));
  const drafts = new DraftService(work, store); return { root, runner, ledger, work, store, drafts };
}
const create: WorkMutation = { kind: 'create', title: '中文事项', content: '正文\n第二行' };

test('same command concurrently across service instances is one frozen request and bounded receipt', async () => {
  const { runner, ledger, work } = await setup(); const work2 = new WorkService(runner, new OperationLedger(ledger.directory));
  const [a,b] = await Promise.all([work.submit('session', { ...create, content: 'x'.repeat(500_000) }, 'cmd1'), work2.submit('session', create, 'cmd1')]);
  assert.equal(a.operation_id, b.operation_id); assert.equal(a.state, 'SUCCEEDED'); assert.equal(runner.tasks.size, 1); assert.equal(runner.writes.length, 1);
  assert.ok(JSON.stringify(a.result).length < 256); assert.equal('request' in a, false); assert.equal(runner.modelCalls, 0);
  const replay = await work.submit('session', { kind: 'nonsense' } as unknown as WorkMutation, 'cmd1');
  assert.deepEqual(replay.result, a.result);
  await assert.rejects(work.operation('other-session', a.operation_id), /not found/);
});

test('lost ack retry replays frozen IDs BEFORE current revision/state checks', async () => {
  const { runner, work, ledger } = await setup();
  const created = await work.submit('s', create, 'create'); runner.loseAck = true;
  const lost = await work.submit('s', { kind: 'follow', task_id: created.task_id!, expected_revision: 1, content: '已联系' }, 'follow');
  assert.equal(lost.state, 'UNCERTAIN'); const task = runner.tasks.get(created.task_id!)!; task.revision = 7; task.status = 'cancelled';
  const replaySubmit = await work.submit('s', { kind: 'set_status', task_id: 'wrong', status: 'completed' }, 'follow');
  assert.equal(replaySubmit.state, 'UNCERTAIN'); assert.equal(runner.writes.length, 2);
  const recovered = await work.retry('s', lost.operation_id);
  assert.equal(recovered.state, 'SUCCEEDED'); assert.equal(recovered.result?.revision, 2); assert.equal(task.revision, 7);
  assert.deepEqual(runner.writes[1].params, runner.writes[2].params);
  const frozen = await ledger.get('s', lost.operation_id); assert.equal(frozen.input.kind, 'follow'); assert.equal(runner.modelCalls, 0);
});

test('binding discovery uses an empty metadata page even when first old task exceeds read bound', async () => {
  const { work, runner } = await setup(); const originalRun = runner.run.bind(runner);
  const oldTask = { task_id: 'legacy_huge', title: 'giant from another client', content: 'x'.repeat(9 * 1024 * 1024), revision: 1, status: 'pending' };
  runner.tasks.set(oldTask.task_id, oldTask);
  let emptyPages = 0;
  runner.run = async request => {
    if (request.operation === 'task.list') {
      if (request.params.offset === Number.MAX_SAFE_INTEGER) { emptyPages++; return { protocol_version: '1', call_id: request.call_id, binding: runner.binding, ok: true, result: { contract_version: '1.0', data: [], warnings: [], next_cursor: null } }; }
      return { protocol_version: '1', call_id: request.call_id, binding: runner.binding, ok: false, error: { layer: 'bridge', code: 'response_too_large', message: 'legacy giant task exceeds read limit' } };
    }
    return originalRun(request);
  };
  await assert.rejects(work.query('s', { kind: 'list', limit: 1 }), /legacy giant task/);
  const op = await work.submit('s', create, 'bounded-new-create');
  assert.equal(op.state, 'SUCCEEDED'); assert.ok(emptyPages >= 2); assert.ok(JSON.stringify(op.result).length < 256);
  const updated = await work.submit('s', { kind: 'set_status', task_id: oldTask.task_id, expected_revision: 1, status: 'completed' }, 'bounded-old-update');
  assert.equal(updated.state, 'SUCCEEDED'); assert.ok(JSON.stringify(updated.result).length < 256);
});

test('conflict remains business rejection and is never auto-rebased', async () => {
  const { runner, work } = await setup(); const created = await work.submit('s', create, 'c');
  const rejected = await work.submit('s', { kind: 'set_status', task_id: created.task_id!, expected_revision: 20, status: 'completed' }, 'bad-rev');
  assert.equal(rejected.state, 'REJECTED'); assert.equal(rejected.error?.code, 'revision_conflict'); assert.equal(rejected.error?.details?.actual_revision, 7);
  await work.retry('s', rejected.operation_id); assert.equal(runner.writes.length, 2);
});

test('restore generations partition operations and block old uncertain writes', async () => {
  const { runner, work, ledger } = await setup(); runner.loseAck = true;
  const old = await work.submit('s', create, 'old'); runner.binding = { ...runner.binding, restore_generation: 'generation_2' };
  const retried = await work.retry('s', old.operation_id); assert.equal(retried.error?.code, 'binding_mismatch'); assert.equal(retried.retryable, false); assert.equal(runner.writes.length, 1);
  const again = await work.submit('s', create, 'old'); assert.equal(again.operation_id, old.operation_id);
  const fresh = await work.submit('s', create, 'fresh'); assert.equal(fresh.binding.restore_generation, 'generation_2');
  const raw = JSON.parse(await readFile(ledger.path('s'), 'utf8')); assert.equal(Object.keys(raw.partitions).length, 2);
});

test('stale browser binding rejects first-time draft save and status change after restore', async () => {
  const { work, drafts, runner } = await setup();
  const oldBinding = runner.binding;
  const saved = { kind: 'create', expected_draft_revision: 0, mutation_id: 'offline', payload: { title: '旧库未上传草稿' }, binding: oldBinding } as const;
  runner.binding = { ...oldBinding, restore_generation: 'generation_2' };

  await assert.rejects(drafts.saveDraft('s', saved), { code: 'binding_mismatch' });
  await assert.rejects(work.submit('s', { kind: 'set_status', task_id: 'kept-in-backup', expected_revision: 1, status: 'completed' }, 'stale-status', undefined, oldBinding), { code: 'binding_mismatch' });
  assert.deepEqual(await drafts.drafts('s'), []);
  assert.deepEqual(await work.operations('s'), []);
});

test('restart RUNNING receipt becomes UNCERTAIN without launching a write', async () => {
  const { ledger, runner } = await setup(); const now = new Date().toISOString();
  const op: FrozenOperation = { operation_id: 'op_interrupted', submission_id: 'cmd', source: 'command', state: 'RUNNING', retryable: true, binding: runner.binding, created_at: now, updated_at: now, input: create, request: { protocol_version: '1', call_id: 'old', operation: 'task.create', binding: runner.binding, params: { title: 'a', content: 'b', request_id: 'stable' } } };
  await atomicWrite(ledger.path('s'), { version: 1, partitions: { [bindingKey(runner.binding)]: { [op.operation_id]: op } } });
  const work = new WorkService(runner, ledger); const recovered = await work.operation('s', op.operation_id);
  assert.equal(recovered.state, 'UNCERTAIN'); assert.equal(runner.writes.length, 0);
  assert.equal((await work.retry('s', op.operation_id)).state, 'SUCCEEDED');
});

test('draft CAS permits incomplete values, mutation replay precedes CAS and tombstone blocks stale resurrection', async () => {
  const { drafts } = await setup(); const input = { kind: 'create' as const, mutation_id: 'm1', expected_draft_revision: 0, payload: { title: '', due: '2026-' } };
  const first = await drafts.saveDraft('s', input); assert.equal(first.draft_revision, 1);
  const second = await drafts.saveDraft('s', { ...input, draft_id: first.draft_id, mutation_id: 'm2', expected_draft_revision: 1, payload: { title: 'new' } });
  assert.deepEqual(await drafts.saveDraft('s', input), first);
  await assert.rejects(drafts.saveDraft('s', { ...input, mutation_id: 'other' }), /another device/);
  const discarded = await drafts.discardDraft('s', first.draft_id, second.draft_revision, 'delete'); assert.equal(discarded.deleted, true);
  assert.deepEqual(await drafts.discardDraft('s', first.draft_id, second.draft_revision, 'delete'), discarded);
  await assert.rejects(drafts.saveDraft('s', { ...input, mutation_id: 'stale', draft_id: first.draft_id, expected_draft_revision: 2 }), /another device/);
  await assert.rejects(drafts.submitDraft('s', first.draft_id, 2), /another device/);
  await assert.rejects(drafts.saveDraft('s', { ...input, payload: { different: true } }), /different draft mutation/);
});

test('same shared draft submitted concurrently from devices binds operation exactly once', async () => {
  const { drafts, store, work, runner } = await setup(); const device2 = new DraftService(work, new DraftStore(store.directory));
  const draft = await drafts.saveDraft('s', { kind: 'create', mutation_id: 'save', expected_draft_revision: 0, payload: { title: '中文', content: '正文' } });
  const [a,b] = await Promise.all([drafts.submitDraft('s', draft.draft_id, 1), device2.submitDraft('s', draft.draft_id, 1)]);
  assert.equal(a.operation_id, b.operation_id); assert.equal(runner.tasks.size, 1); assert.equal(runner.writes.length, 1);
  const tombstone = (await drafts.drafts('s'))[0]; assert.equal(tombstone.deleted, true); assert.equal(tombstone.draft_revision, 2);
  assert.equal((await device2.submitDraft('s', draft.draft_id, 1)).operation_id, a.operation_id);
});

test('draft binding is durable before bridge, and crash recovery uses frozen payload', async () => {
  const { drafts, store, work, runner, ledger } = await setup();
  const draft = await drafts.saveDraft('s', { kind: 'create', mutation_id: 'save', expected_draft_revision: 0, payload: { title: 'original', content: 'frozen' } });
  const originalExecute = work.executePrepared;
  work.executePrepared = async () => { throw new Error('simulated host crash before launch'); };
  await assert.rejects(drafts.submitDraft('s', draft.draft_id, 1), /simulated host crash/);
  const persisted = (await drafts.drafts('s'))[0]; assert.ok(persisted.operation_id); assert.equal(runner.writes.length, 0);
  const frozen = await ledger.get('s', persisted.operation_id!); assert.equal(frozen.state, 'PREPARED'); assert.equal(frozen.request.params.content, 'frozen');
  work.executePrepared = originalExecute;
  const restarted = new DraftService(new WorkService(runner, new OperationLedger(ledger.directory)), new DraftStore(store.directory));
  assert.equal((await restarted.submitDraft('s', draft.draft_id, 1)).state, 'SUCCEEDED'); assert.equal(runner.tasks.size, 1);
});

test('submission success does not clear edits synchronized during the bridge call', async () => {
  const { drafts, runner } = await setup();
  const draft = await drafts.saveDraft('s', { kind: 'create', mutation_id: 'save', expected_draft_revision: 0, payload: { title: 'first', content: 'first' } });
  runner.onWrite = async () => { await drafts.saveDraft('s', { kind: 'create', draft_id: draft.draft_id, mutation_id: 'new-input', expected_draft_revision: 1, payload: { title: 'second', content: 'second' } }); };
  const op = await drafts.submitDraft('s', draft.draft_id, 1); assert.equal(op.state, 'SUCCEEDED');
  const current = (await drafts.drafts('s'))[0]; assert.equal(current.deleted, undefined); assert.equal(current.draft_revision, 2); assert.equal(current.payload.title, 'second');
});

test('old generation draft cannot submit to restored library and remains viewable', async () => {
  const { drafts, runner } = await setup(); const draft = await drafts.saveDraft('s', { kind: 'create', mutation_id: 'save', expected_draft_revision: 0, payload: { title: 'a', content: 'b' } });
  runner.binding = { ...runner.binding, restore_generation: 'restored' };
  await assert.rejects(drafts.submitDraft('s', draft.draft_id, 1), /different library or restore generation/); assert.equal(runner.writes.length, 0);
  const fresh = await drafts.saveDraft('s', { kind: 'create', mutation_id: 'newgen', expected_draft_revision: 0, payload: {} }); assert.notEqual(fresh.draft_id, draft.draft_id);
  assert.equal((await drafts.drafts('s')).length, 2);
});

test('lost draft acknowledgement is shared across devices and retry cleanup survives refresh', async () => {
  const { drafts, store, work, runner } = await setup();
  const draft = await drafts.saveDraft('s', { kind: 'create', mutation_id: 'save', expected_draft_revision: 0, payload: { title: 'a', content: 'b' } });
  runner.loseAck = true;
  const uncertain = await drafts.submitDraft('s', draft.draft_id, 1); assert.equal(uncertain.state, 'UNCERTAIN');
  const device2 = new DraftService(work, new DraftStore(store.directory));
  const repeated = await device2.submitDraft('s', draft.draft_id, 1); assert.equal(repeated.operation_id, uncertain.operation_id); assert.equal(runner.writes.length, 1);
  assert.equal((await device2.drafts('s'))[0].deleted, undefined);
  assert.equal((await work.retry('s', uncertain.operation_id)).state, 'SUCCEEDED');
  assert.equal((await device2.drafts('s'))[0].deleted, true); assert.equal(runner.tasks.size, 1);
});

test('edit/follow draft submission freezes loaded task revision, rejects incomplete submit but saves input', async () => {
  const { drafts, work, runner } = await setup();
  const created = await work.submit('s', create, 'created');
  const first = await drafts.saveDraft('s', { kind: 'follow', task_id: created.task_id!, expected_draft_revision: 0, mutation_id: 'follow-draft', payload: { content: '跟进' } });
  await assert.rejects(drafts.submitDraft('s', first.draft_id, 1), /base_task_revision/);
  const loaded = await drafts.saveDraft('s', { kind: 'follow', task_id: created.task_id!, draft_id: first.draft_id, expected_draft_revision: 1, mutation_id: 'base', base_task_revision: 1, payload: { content: '跟进' } });
  runner.tasks.get(created.task_id!)!.revision = 2;
  const rejected = await drafts.submitDraft('s', loaded.draft_id, 2); assert.equal(rejected.state, 'REJECTED'); assert.equal(rejected.expected_revision, 1);
  const current = (await drafts.drafts('s'))[0]; assert.equal(current.deleted, undefined); assert.equal(current.payload.content, '跟进');
});

test('protocol validator rejects call/version/binding mismatch and unbounded write ack', () => {
  const request: BridgeRequest = { protocol_version: '1', call_id: 'one', operation: 'task.create', binding: { library_id: 'a', restore_generation: '1' }, params: {} };
  const response: BridgeResponse = { protocol_version: '1', call_id: 'one', ok: true, binding: request.binding!, result: { contract_version: '1.0', data: { task_id: 'task', revision: 1 }, warnings: [], next_cursor: null } };
  assert.doesNotThrow(() => parseResponse(response, request));
  assert.throws(() => parseResponse({ ...response, call_id: 'two' }, request), /call_id/);
  assert.throws(() => parseResponse({ ...response, binding: { ...request.binding, restore_generation: '2' } }, request), /binding mismatch/);
  assert.throws(() => parseResponse({ ...response, result: { ...response.result, data: { task_id: 'task', revision: 1, content: 'x'.repeat(1000) } } }, request), /unbounded/);
});

test('atomic store publishes private complete JSON and ignores orphan temporary files', async () => {
  const root = await mkdtemp(join(tmpdir(), 'dsh-work-atomic-')); const path = join(root, 'nested', 'state.json');
  await atomicWrite(path, { state: 'complete' }); assert.deepEqual(await readJSON(path, () => ({})), { state: 'complete' });
  assert.equal((await stat(path)).mode & 0o777, 0o600);
});

test('runner rejects unsafe config, oversize stdin and already-aborted calls before spawning', async () => {
  assert.throws(() => new BridgeRunner({ projectRoot: 'relative', dataDir: '/tmp/test' }), /projectRoot must be absolute/);
  assert.throws(() => new BridgeRunner({ projectRoot: '/tmp', dataDir: 'relative' }), /dataDir must be absolute/);
  assert.throws(() => new BridgeRunner({ projectRoot: '/tmp', dataDir: '/tmp/test', bridgeTimeoutMs: 1 }), /5000/);
  const runner = new BridgeRunner({ projectRoot: '/tmp', dataDir: '/tmp/test' }); const request: BridgeRequest = { protocol_version: '1', call_id: 'call', operation: 'task.list', params: {} };
  await assert.rejects(runner.run({ ...request, params: { huge: 'x'.repeat(1024 * 1024) } }), /1 MiB/);
  await assert.rejects(runner.run(request, AbortSignal.abort()), /before launch/);
});
