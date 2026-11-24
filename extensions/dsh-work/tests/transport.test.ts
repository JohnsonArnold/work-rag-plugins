import test from 'node:test';
import assert from 'node:assert/strict';
import { sessionApi } from '../src/client/api-transport.ts';
import type { RemoteHost } from '../src/client/api-transport.ts';
test('transport unwraps DSH RemoteResult before business result and forwards scope/signal', async () => {
  const signal = new AbortController().signal;
  const api = sessionApi({ query: async (...args: unknown[]) => {
    assert.deepEqual(args, ['session-A', { kind: 'list' }, signal]);
    return { ok: true, value: { ok: true, data: { data: [], binding: { library_id: 'lib', restore_generation: 'gen' } } } };
  } } as RemoteHost, 'session-A');
  assert.deepEqual(await api.query({ kind: 'list' }, signal), { data: [], binding: { library_id: 'lib', restore_generation: 'gen' } });
});
test('transport retains gateway and business conflict metadata', async () => {
  for (const reply of [
    { ok: false, error: { code: 'transport', message: 'offline' } },
    { ok: true, value: { ok: false, error: { code: 'draft_conflict', message: 'conflict', layer: 'host', details: { actual_draft_revision: 3 } } } },
  ]) {
    const api = sessionApi({ drafts: async () => reply } as RemoteHost, 'session-A');
    await assert.rejects(api.drafts(), (e: any) => e.code === (reply.ok ? reply.value?.error.code : reply.error?.code));
  }
});
test('transport abort before dispatch never invokes host', async () => {
  const controller = new AbortController(); controller.abort(); let calls = 0;
  const api = sessionApi({ query: async () => { calls++; throw Error('unexpected'); } } as RemoteHost, 'session-A');
  await assert.rejects(api.query({ kind: 'list' }, controller.signal), { name: 'AbortError' });
  assert.equal(calls, 0);
});
