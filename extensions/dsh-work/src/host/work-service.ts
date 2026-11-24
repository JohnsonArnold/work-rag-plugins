import { randomUUID } from 'node:crypto';
import type { Binding, BridgeExecutor, BridgeRequest, CoreResult, FrozenOperation, OperationDTO, WorkMutation, WorkQuery, WorkReply } from '../shared/contracts.ts';
import { errorDTO, integer, parseAck, parseMutation, parseQuery, parseResponse, sameBinding, text, WorkServiceError } from '../shared/schema.ts';
import { serialized } from './atomic-store.ts';
import { OperationLedger, operationDTO } from './operation-ledger.ts';

function callId(): string { return `call_${randomUUID().replaceAll('-', '')}`; }
export class WorkService {
  readonly runner: BridgeExecutor;
  readonly ledger: OperationLedger;
  constructor(runner: BridgeExecutor, ledger: OperationLedger) { this.runner = runner; this.ledger = ledger; }
  async query(sessionId: string, input: WorkQuery, signal?: AbortSignal): Promise<WorkReply> {
    text(sessionId, 'sessionId'); const query = parseQuery(input); let params: Record<string, unknown>;
    if (query.kind === 'get') params = { task_id: query.task_id };
    else if (query.kind === 'logs') params = { task_id: query.task_id, after: query.after ?? 0, limit: query.limit ?? 50 };
    else { const { kind: _kind, ...rest } = query; params = { ...rest, limit: query.limit ?? 50, offset: query.offset ?? 0 }; }
    const request: BridgeRequest = { protocol_version: '1', call_id: callId(), operation: `task.${query.kind}`, params };
    const response = parseResponse(await this.runner.run(request, signal), request);
    if (!response.ok) throw new WorkServiceError(response.error.code, response.error.message, response.error.details, response.error.layer);
    return { ...response.result, binding: response.binding };
  }
  async binding(sessionId: string, signal?: AbortSignal): Promise<Binding> {
    // Metadata discovery must not load an arbitrary existing task: a task
    // created through another entrypoint can exceed the read stdout bound.
    // This valid, known-empty page still returns transaction-pinned binding.
    return (await this.query(sessionId, { kind: 'list', limit: 1, offset: Number.MAX_SAFE_INTEGER }, signal)).binding;
  }
  async prepare(sessionId: string, input: WorkMutation, submissionId: string, signal?: AbortSignal, options: { source?: 'command' | 'draft'; binding?: Binding } = {}): Promise<FrozenOperation> {
    text(sessionId, 'sessionId'); text(submissionId, 'submissionId');
    return serialized(`${this.ledger.path(sessionId)}.prepare`, async () => {
      // Submission replay is deliberately BEFORE current input validation,
      // revision reads, restore-generation discovery, and task state checks.
      const previous = await this.ledger.findSubmission(sessionId, submissionId);
      if (previous) return previous;
      const mutation = parseMutation(input);
      const binding = await this.binding(sessionId, signal);
      if (options.binding && !sameBinding(options.binding, binding)) throw new WorkServiceError('binding_mismatch', 'Submission belongs to a different library or restore generation', { expected: options.binding, actual: binding });
      if (mutation.kind !== 'create' && mutation.expected_revision === undefined) {
        const detail = await this.query(sessionId, { kind: 'get', task_id: mutation.task_id }, signal);
        if (!sameBinding(binding, detail.binding)) throw new WorkServiceError('binding_mismatch', 'Library changed while preparing operation');
        mutation.expected_revision = integer((detail.data as { revision: number }).revision, 'revision', 1);
      }
      const operationId = `op_${randomUUID().replaceAll('-', '')}`; const now = new Date().toISOString();
      const requestId = `dsh-work:${operationId}`; let operation: BridgeRequest['operation']; let params: Record<string, unknown>;
      if (mutation.kind === 'create') {
        const { kind: _kind, ...payload } = mutation; operation = 'task.create'; params = { ...payload, request_id: requestId };
      } else if (mutation.kind === 'follow') {
        operation = 'task.append_log'; params = { task_id: mutation.task_id, expected_revision: mutation.expected_revision, content: mutation.content, request_id: requestId };
      } else {
        operation = 'task.update'; params = { task_id: mutation.task_id, expected_revision: mutation.expected_revision, changes: mutation.kind === 'edit' ? mutation.changes : { status: mutation.status }, request_id: requestId };
      }
      return this.ledger.insert(sessionId, {
        operation_id: operationId, submission_id: submissionId, source: options.source ?? 'command', state: 'PREPARED',
        created_at: now, updated_at: now, binding, retryable: true, input: mutation,
        ...(mutation.kind !== 'create' ? { task_id: mutation.task_id, expected_revision: mutation.expected_revision } : {}),
        request: { protocol_version: '1', call_id: callId(), operation, binding, params },
      });
    });
  }
  async submit(sessionId: string, input: WorkMutation, submissionId: string, signal?: AbortSignal, binding?: Binding): Promise<OperationDTO> {
    const op = await this.prepare(sessionId, input, submissionId, signal, { binding });
    return this.executePrepared(sessionId, op.operation_id, signal);
  }
  async operation(sessionId: string, operationId: string): Promise<OperationDTO> { return operationDTO(await this.ledger.get(sessionId, operationId)); }
  async operations(sessionId: string, limit = 50, offset = 0): Promise<OperationDTO[]> {
    integer(limit, 'limit', 1, 200); integer(offset, 'offset'); return this.ledger.list(sessionId, limit, offset);
  }
  async retry(sessionId: string, operationId: string, signal?: AbortSignal): Promise<OperationDTO> { return this.executePrepared(sessionId, operationId, signal, true); }
  async executePrepared(sessionId: string, operationId: string, signal?: AbortSignal, retry = false): Promise<OperationDTO> {
    return serialized(`${this.ledger.path(sessionId)}.${operationId}.execute`, async () => {
      let op = await this.ledger.get(sessionId, operationId);
      if (op.state === 'SUCCEEDED' || op.state === 'REJECTED' || (op.state === 'UNCERTAIN' && !retry)) return operationDTO(op);
      try {
        const current = await this.binding(sessionId, signal);
        if (!sameBinding(op.binding, current)) {
          // Preserve the old outcome state; never send old frozen writes into a
          // restored library. A historical receipt remains viewable.
          return operationDTO(await this.ledger.change(sessionId, operationId, record => {
            record.retryable = false; record.updated_at = new Date().toISOString();
            record.error = { layer: 'host', code: 'binding_mismatch', message: 'Operation belongs to another library or restore generation', details: { expected: record.binding, actual: current } };
          }));
        }
        op = await this.ledger.change(sessionId, operationId, record => { record.state = 'RUNNING'; record.updated_at = new Date().toISOString(); delete record.error; });
        const request = { ...op.request, call_id: callId() };
        const response = parseResponse(await this.runner.run(request, signal), request);
        if (response.ok) {
          const ack = parseAck(response.result.data);
          op = await this.ledger.change(sessionId, operationId, record => {
            record.state = 'SUCCEEDED'; record.retryable = false; record.result = ack; record.task_id = ack.task_id;
            record.updated_at = new Date().toISOString(); delete record.error;
          });
        } else {
          op = await this.ledger.change(sessionId, operationId, record => {
            record.state = 'REJECTED'; record.retryable = false; record.error = response.error; record.updated_at = new Date().toISOString();
          });
        }
      } catch (error) {
        op = await this.ledger.change(sessionId, operationId, record => {
          record.state = 'UNCERTAIN'; record.retryable = true; record.error = errorDTO(error); record.updated_at = new Date().toISOString();
        });
      }
      return operationDTO(op);
    });
  }
}
