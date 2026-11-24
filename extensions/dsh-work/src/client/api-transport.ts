import type { WorkApi } from './work-api.ts';
interface Failure { code?: string; message: string; details?: unknown; layer?: string }
type HostReply = { ok: true; data: unknown } | { ok: false; error: Failure };
type RemoteReply = { ok: true; value: HostReply } | { ok: false; error: Failure };
export type RemoteHost = Record<string, (sessionId: string, input: unknown, signal?: AbortSignal) => Promise<RemoteReply>>;
export function sessionApi(remote: RemoteHost, sessionId: string): WorkApi {
  const call = async (method: string, input: unknown, signal?: AbortSignal): Promise<any> => {
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    const wire = await remote[method](sessionId, input, signal);
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    if (!wire.ok) throw Object.assign(new Error(wire.error.message), wire.error);
    const reply = wire.value;
    if (!reply.ok) throw Object.assign(new Error(reply.error.message), reply.error);
    return reply.data;
  };
  return {
    query: (input, signal) => call('query', input, signal),
    submit: (mutation, submission_id, binding, signal) => call('submit', { mutation, submission_id, binding }, signal),
    operation: operation_id => call('operation', { operation_id }),
    operations: (limit = 50, offset = 0) => call('operations', { limit, offset }),
    retry: (operation_id, signal) => call('retry', { operation_id }, signal),
    drafts: () => call('drafts', {}),
    saveDraft: input => call('saveDraft', input),
    discardDraft: (draft_id, expected_draft_revision, mutation_id) => call('discardDraft', { draft_id, expected_draft_revision, mutation_id }),
    submitDraft: (draft_id, expected_draft_revision, signal) => call('submitDraft', { draft_id, expected_draft_revision }, signal),
  };
}
