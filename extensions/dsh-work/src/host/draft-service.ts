import { randomUUID } from 'node:crypto';
import type { DraftDTO, DraftSave, OperationDTO, WorkMutation } from '../shared/contracts.ts';
import { bindingKey, canonical, integer, object, parseBinding, sameBinding, text, WorkServiceError } from '../shared/schema.ts';
import { allDrafts, draftDTO, DraftStore, findDraft } from './draft-store.ts';
import type { DraftRecord } from './draft-store.ts';
import { WorkService } from './work-service.ts';

export function draftKey(kind: string, taskId?: string): string {
  if (kind === 'create') return 'create';
  if (kind !== 'edit' && kind !== 'follow') throw new WorkServiceError('invalid_input', 'Unknown draft kind');
  return `${kind}:${text(taskId, 'task_id')}`;
}
function conflict(draft?: DraftRecord): never {
  throw new WorkServiceError('draft_conflict', 'Shared draft was changed on another device', draft ? { actual_draft_revision: draft.draft_revision, draft: draftDTO(draft) } : { actual_draft_revision: 0 });
}
export class DraftService {
  readonly work: WorkService;
  readonly store: DraftStore;
  constructor(work: WorkService, store: DraftStore) { this.work = work; this.store = store; }
  async drafts(sessionId: string): Promise<DraftDTO[]> {
    return this.store.transaction(sessionId, async data => {
      // A user may confirm an uncertain draft through WorkService.retry. Also
      // finish cleanup on the next draft read after a crash following success.
      for (const draft of allDrafts(data)) {
        const operationId = draft.submissions[String(draft.draft_revision)];
        if (!draft.deleted && operationId) {
          const op = await this.work.operation(sessionId, operationId);
          if (op.state === 'SUCCEEDED') {
            draft.deleted = true; draft.draft_revision++; draft.operation_id = operationId; draft.updated_at = new Date().toISOString();
          }
        }
      }
      return allDrafts(data).map(draftDTO);
    });
  }
  async saveDraft(sessionId: string, input: DraftSave): Promise<DraftDTO> {
    text(input.mutation_id, 'mutation_id');
    return this.store.transaction(sessionId, async data => {
      const { binding: expectedBinding, ...mutation } = input;
      // Keep stored mutation fingerprints compatible with requests saved before browser binding was added.
      const fingerprint = canonical({ action: 'save', input: mutation }); const receipt = data.receipts[input.mutation_id];
      // Replay before CAS, generation discovery, or current draft validation.
      if (receipt) {
        if (receipt.fingerprint !== fingerprint || expectedBinding && !sameBinding(parseBinding(expectedBinding), receipt.result.binding)) throw new WorkServiceError('mutation_id_reused', 'mutation_id was used for a different draft mutation');
        return receipt.result;
      }
      integer(input.expected_draft_revision, 'expected_draft_revision');
      if (input.base_task_revision !== undefined) integer(input.base_task_revision, 'base_task_revision', 1);
      const key = draftKey(input.kind, input.task_id);
      if (input.key !== undefined && input.key !== key) throw new WorkServiceError('invalid_input', 'Draft key must match kind and task_id');
      const payload = object(input.payload, 'payload');
      // Incomplete strings, dates and fields are intentionally not validated
      // as a formal task here. Only portable JSON is persisted.
      let frozenPayload: Record<string, unknown>;
      try { frozenPayload = JSON.parse(JSON.stringify(payload)); } catch { throw new WorkServiceError('invalid_input', 'Draft payload must be JSON'); }
      const binding = await this.work.binding(sessionId);
      if (expectedBinding && !sameBinding(parseBinding(expectedBinding), binding)) {
        throw new WorkServiceError('binding_mismatch', 'Draft belongs to a different library or restore generation', { expected: expectedBinding, actual: binding });
      }
      const partition = data.partitions[bindingKey(binding)] ??= {};
      const current = input.draft_id ? findDraft(data, input.draft_id) : Object.values(partition).find(d => d.key === key);
      if (input.draft_id && !current) throw new WorkServiceError('draft_not_found', 'Draft not found in this session');
      if (current && (!sameBinding(current.binding, binding) || current.key !== key)) throw new WorkServiceError('binding_mismatch', 'Draft belongs to a different key or restore generation');
      if ((current?.draft_revision ?? 0) !== input.expected_draft_revision) conflict(current);
      const draft: DraftRecord = {
        draft_id: current?.draft_id ?? `draft_${randomUUID().replaceAll('-', '')}`, kind: input.kind, key, binding,
        ...(input.task_id ? { task_id: input.task_id } : {}), draft_revision: input.expected_draft_revision + 1,
        ...(input.base_task_revision !== undefined ? { base_task_revision: input.base_task_revision } : {}),
        payload: frozenPayload, updated_at: new Date().toISOString(), submissions: current?.submissions ?? {},
      };
      partition[draft.draft_id] = draft;
      const result = draftDTO(draft); data.receipts[input.mutation_id] = { fingerprint, result }; return result;
    });
  }
  async discardDraft(sessionId: string, draftId: string, expectedRevision: number, mutationId = `discard:${draftId}:${expectedRevision}`): Promise<DraftDTO> {
    text(mutationId, 'mutation_id');
    return this.store.transaction(sessionId, async data => {
      const fingerprint = canonical({ action: 'discard', draftId, expectedRevision }); const receipt = data.receipts[mutationId];
      if (receipt) {
        if (receipt.fingerprint !== fingerprint) throw new WorkServiceError('mutation_id_reused', 'mutation_id was used for a different draft mutation');
        return receipt.result;
      }
      const draft = findDraft(data, draftId);
      if (!draft) throw new WorkServiceError('draft_not_found', 'Draft not found in this session');
      integer(expectedRevision, 'expected_draft_revision');
      if (draft.draft_revision !== expectedRevision || draft.deleted) conflict(draft);
      const binding = await this.work.binding(sessionId);
      if (!sameBinding(binding, draft.binding)) throw new WorkServiceError('binding_mismatch', 'Cannot discard an old restore-generation draft');
      draft.deleted = true; draft.draft_revision++; draft.updated_at = new Date().toISOString();
      const result = draftDTO(draft); data.receipts[mutationId] = { fingerprint, result }; return result;
    });
  }
  async submitDraft(sessionId: string, draftId: string, expectedRevision: number, signal?: AbortSignal): Promise<OperationDTO> {
    const operationId = await this.store.transaction(sessionId, async data => {
      const draft = findDraft(data, draftId);
      if (!draft) throw new WorkServiceError('draft_not_found', 'Draft not found in this session');
      // A completed/submitting older revision always resolves its original
      // operation even after deletion, later edits, or a library restore.
      const previous = draft.submissions[String(expectedRevision)];
      if (previous) return previous;
      integer(expectedRevision, 'expected_draft_revision', 1);
      if (draft.deleted || draft.draft_revision !== expectedRevision) conflict(draft);
      let input: WorkMutation;
      // Draft payload also holds unfinished/UI-only fields (e.g. tags_text).
      // Project only formal core fields, never spread that arbitrary payload
      // into the bridge request.
      const fields = (payload: Record<string, unknown>, names: string[]) => Object.fromEntries(names.filter(name => Object.hasOwn(payload, name)).map(name => [name, payload[name]]));
      if (draft.kind === 'create') input = { ...fields(draft.payload, ['title', 'content', 'due', 'tags']), kind: 'create' } as WorkMutation;
      else {
        const revision = integer(draft.base_task_revision, 'base_task_revision', 1);
        if (draft.kind === 'follow') input = { kind: 'follow', task_id: text(draft.task_id, 'task_id'), expected_revision: revision, content: text(draft.payload.content, 'content') };
        else input = { kind: 'edit', task_id: text(draft.task_id, 'task_id'), expected_revision: revision, changes: fields(object(draft.payload.changes ?? draft.payload, 'changes'), ['title', 'content', 'due', 'tags', 'status']) };
      }
      const op = await this.work.prepare(sessionId, input, `draft:${draftId}:${expectedRevision}`, signal, { source: 'draft', binding: draft.binding });
      // PREPARED already durably contains frozen params and request_id. This
      // draft binding is fsynced BEFORE any bridge mutation starts. A crash
      // between the two files recovers the same deterministic submission_id.
      draft.submissions[String(expectedRevision)] = op.operation_id;
      draft.operation_id = op.operation_id; return op.operation_id;
    });
    const operation = await this.work.executePrepared(sessionId, operationId, signal);
    if (operation.state === 'SUCCEEDED') await this.store.transaction(sessionId, data => {
      const draft = findDraft(data, draftId);
      if (draft && draft.draft_revision === expectedRevision && !draft.deleted) {
        draft.deleted = true; draft.draft_revision++; draft.operation_id = operationId; draft.updated_at = new Date().toISOString();
      }
    });
    return operation;
  }
}
