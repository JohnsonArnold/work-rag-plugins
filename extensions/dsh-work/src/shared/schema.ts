import type { Binding, BridgeRequest, BridgeResponse, MutationAck, WorkMutation, WorkQuery } from './contracts.ts';

export class WorkServiceError extends Error {
  readonly layer: string;
  readonly code: string;
  readonly details: Record<string, unknown>;
  constructor(code: string, message: string, details: Record<string, unknown> = {}, layer = 'host') {
    super(message); this.name = 'WorkServiceError'; this.code = code; this.layer = layer; this.details = details;
  }
}
export function object(value: unknown, name = 'input'): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new WorkServiceError('invalid_input', `${name} must be an object`);
  return value as Record<string, unknown>;
}
export function text(value: unknown, name: string, allowEmpty = false): string {
  if (typeof value !== 'string' || (!allowEmpty && !value.trim())) throw new WorkServiceError('invalid_input', `${name} must be a nonempty string`);
  return value;
}
export function integer(value: unknown, name: string, min = 0, max = Number.MAX_SAFE_INTEGER): number {
  if (!Number.isSafeInteger(value) || Number(value) < min || Number(value) > max) throw new WorkServiceError('invalid_input', `${name} must be an integer from ${min} to ${max}`);
  return value as number;
}
export function parseBinding(value: unknown): Binding {
  const v = object(value, 'binding');
  return { library_id: text(v.library_id, 'library_id'), restore_generation: text(v.restore_generation, 'restore_generation') };
}
export function sameBinding(a: Binding, b: Binding): boolean { return a.library_id === b.library_id && a.restore_generation === b.restore_generation; }
export function bindingKey(binding: Binding): string { return JSON.stringify([binding.library_id, binding.restore_generation]); }
function status(value: unknown, allowCancelled = true): void {
  if (!(allowCancelled ? ['pending', 'in_progress', 'completed', 'cancelled'] : ['pending', 'in_progress', 'completed']).includes(String(value))) throw new WorkServiceError('invalid_input', 'Invalid status');
}
function optionalFields(v: Record<string, unknown>): void {
  if ('due' in v && v.due !== null) text(v.due, 'due', true);
  if ('tags' in v && (!Array.isArray(v.tags) || !v.tags.every(t => typeof t === 'string'))) throw new WorkServiceError('invalid_input', 'tags must be strings');
}
export function parseQuery(value: unknown): WorkQuery {
  const v = object(value);
  if (v.kind === 'get' || v.kind === 'logs') text(v.task_id, 'task_id');
  else if (v.kind !== 'list') throw new WorkServiceError('invalid_input', 'Unknown query kind');
  if (v.limit !== undefined) integer(v.limit, 'limit', 1, 200);
  if (v.offset !== undefined) integer(v.offset, 'offset');
  if (v.after !== undefined) integer(v.after, 'after');
  if (v.status !== undefined) status(v.status);
  if (v.query !== undefined) text(v.query, 'query', true);
  if (v.view !== undefined) text(v.view, 'view');
  return structuredClone(v) as WorkQuery;
}
export function parseMutation(value: unknown): WorkMutation {
  const v = object(value);
  if (v.kind === 'create') { text(v.title, 'title'); text(v.content, 'content'); optionalFields(v); }
  else if (['edit', 'follow', 'set_status'].includes(String(v.kind))) {
    text(v.task_id, 'task_id');
    if (v.expected_revision !== undefined) integer(v.expected_revision, 'expected_revision', 1);
    if (v.kind === 'follow') text(v.content, 'content');
    if (v.kind === 'set_status') status(v.status, false);
    if (v.kind === 'edit') {
      const c = object(v.changes, 'changes');
      if (!Object.keys(c).length || Object.keys(c).some(k => !['title', 'content', 'due', 'tags', 'status'].includes(k))) throw new WorkServiceError('invalid_input', 'Unsupported or empty changes');
      if ('title' in c) text(c.title, 'title');
      if ('content' in c) text(c.content, 'content');
      if ('status' in c) status(c.status);
      optionalFields(c);
    }
  } else throw new WorkServiceError('invalid_input', 'Unknown mutation kind');
  return structuredClone(v) as WorkMutation;
}
export function parseAck(value: unknown): MutationAck {
  const v = object(value, 'MutationAck');
  if (Object.keys(v).some(k => !['task_id', 'revision', 'status', 'log_id'].includes(k))) throw new WorkServiceError('invalid_response', 'MutationAck contains unbounded or unknown fields');
  const task_id = text(v.task_id, 'task_id'); const revision = integer(v.revision, 'revision', 1);
  if (task_id.length > 256 || (v.log_id !== undefined && text(v.log_id, 'log_id').length > 256)) throw new WorkServiceError('invalid_response', 'MutationAck identifier too long');
  if (v.status !== undefined) status(v.status);
  return structuredClone(v) as MutationAck;
}
export function parseResponse(value: unknown, request: BridgeRequest): BridgeResponse {
  const v = object(value, 'bridge response');
  if (v.protocol_version !== '1' || v.call_id !== request.call_id || typeof v.ok !== 'boolean') throw new WorkServiceError('invalid_response', 'Bridge protocol or call_id mismatch');
  if (v.binding !== undefined) parseBinding(v.binding);
  if (v.ok) {
    parseBinding(v.binding);
    const result = object(v.result, 'result');
    if (result.contract_version !== '1.0' || !Array.isArray(result.warnings) || !(result.next_cursor === null || typeof result.next_cursor === 'string')) throw new WorkServiceError('invalid_response', 'Invalid core result envelope');
    if (request.binding && !sameBinding(request.binding, v.binding as Binding)) throw new WorkServiceError('invalid_response', 'Response binding mismatch');
    if (['task.create', 'task.update', 'task.append_log'].includes(request.operation)) parseAck(result.data);
  } else {
    const e = object(v.error, 'error'); text(e.layer, 'error.layer'); text(e.code, 'error.code'); text(e.message, 'error.message');
  }
  return v as BridgeResponse;
}
export function errorDTO(error: unknown): { layer: string; code: string; message: string; details?: Record<string, unknown> } {
  if (error instanceof WorkServiceError) return { layer: error.layer, code: error.code, message: error.message, details: error.details };
  return { layer: 'host', code: 'internal_error', message: error instanceof Error ? error.message : String(error) };
}
export function canonical(value: unknown): string {
  if (value === undefined) return 'null';
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') return `{${Object.entries(value).filter(([,v]) => v !== undefined).sort(([a],[b]) => a.localeCompare(b)).map(([k,v]) => `${JSON.stringify(k)}:${canonical(v)}`).join(',')}}`;
  return JSON.stringify(value);
}
