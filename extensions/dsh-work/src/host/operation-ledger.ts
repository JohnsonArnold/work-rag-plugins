import { resolve, join } from 'node:path';
import type { FrozenOperation, OperationDTO } from '../shared/contracts.ts';
import { bindingKey, canonical, text, WorkServiceError } from '../shared/schema.ts';
import { atomicWrite, fileKey, readJSON, serialized } from './atomic-store.ts';

export type LedgerData = { version: 1; partitions: Record<string, Record<string, FrozenOperation>> };
const recovered = new Set<string>();
export function allOperations(data: LedgerData): FrozenOperation[] { return Object.values(data.partitions).flatMap(p => Object.values(p)); }
export function findOperation(data: LedgerData, id: string): FrozenOperation | undefined { return allOperations(data).find(o => o.operation_id === id); }
export function operationDTO(op: FrozenOperation): OperationDTO {
  const { input: _input, request: _request, ...dto } = op; return structuredClone(dto);
}
export class OperationLedger {
  readonly directory: string;
  constructor(directory: string) { this.directory = resolve(directory); }
  path(sessionId: string): string { text(sessionId, 'sessionId'); return join(this.directory, `${fileKey(sessionId)}.json`); }
  async transaction<T>(sessionId: string, action: (data: LedgerData) => T | Promise<T>): Promise<T> {
    const path = this.path(sessionId);
    return serialized(path, async () => {
      const data = await readJSON<LedgerData>(path, () => ({ version: 1, partitions: {} }));
      if (data.version !== 1 || !data.partitions) throw new WorkServiceError('corrupt_store', 'Unsupported operation ledger');
      const before = canonical(data);
      if (!recovered.has(path)) {
        for (const op of allOperations(data)) if (op.state === 'RUNNING') {
          op.state = 'UNCERTAIN'; op.retryable = true; op.updated_at = new Date().toISOString();
          op.error = { layer: 'host', code: 'interrupted', message: 'Host restarted before the operation outcome was recorded' };
        }
      }
      const result = await action(data);
      if (canonical(data) !== before) await atomicWrite(path, data);
      recovered.add(path);
      return structuredClone(result);
    });
  }
  async get(sessionId: string, id: string): Promise<FrozenOperation> {
    return this.transaction(sessionId, data => {
      const op = findOperation(data, id);
      if (!op) throw new WorkServiceError('operation_not_found', 'Operation not found in this session');
      return op;
    });
  }
  async findSubmission(sessionId: string, submissionId: string): Promise<FrozenOperation | undefined> {
    return this.transaction(sessionId, data => allOperations(data).find(o => o.submission_id === submissionId));
  }
  async insert(sessionId: string, op: FrozenOperation): Promise<FrozenOperation> {
    return this.transaction(sessionId, data => {
      const previous = allOperations(data).find(o => o.submission_id === op.submission_id);
      if (previous) return previous;
      const partition = data.partitions[bindingKey(op.binding)] ??= {};
      partition[op.operation_id] = structuredClone(op); return op;
    });
  }
  async change(sessionId: string, id: string, change: (op: FrozenOperation) => void): Promise<FrozenOperation> {
    return this.transaction(sessionId, data => {
      const op = findOperation(data, id);
      if (!op) throw new WorkServiceError('operation_not_found', 'Operation not found in this session');
      change(op); return op;
    });
  }
  async list(sessionId: string, limit = 50, offset = 0): Promise<OperationDTO[]> {
    return this.transaction(sessionId, data => allOperations(data).sort((a,b) => b.created_at.localeCompare(a.created_at) || b.operation_id.localeCompare(a.operation_id)).slice(offset, offset + limit).map(operationDTO));
  }
}
