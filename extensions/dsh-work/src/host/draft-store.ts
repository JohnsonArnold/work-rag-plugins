import { join, resolve } from 'node:path';
import type { DraftDTO } from '../shared/contracts.ts';
import { canonical, text, WorkServiceError } from '../shared/schema.ts';
import { atomicWrite, fileKey, readJSON, serialized } from './atomic-store.ts';

export type DraftRecord = DraftDTO & { submissions: Record<string, string> };
export type DraftReceipt = { fingerprint: string; result: DraftDTO };
export type DraftData = { version: 1; partitions: Record<string, Record<string, DraftRecord>>; receipts: Record<string, DraftReceipt> };
export function allDrafts(data: DraftData): DraftRecord[] { return Object.values(data.partitions).flatMap(p => Object.values(p)); }
export function findDraft(data: DraftData, id: string): DraftRecord | undefined { return allDrafts(data).find(d => d.draft_id === id); }
export function draftDTO(record: DraftRecord): DraftDTO { const { submissions: _submissions, ...dto } = record; return structuredClone(dto); }
export class DraftStore {
  readonly directory: string;
  constructor(directory: string) { this.directory = resolve(directory); }
  path(sessionId: string): string { text(sessionId, 'sessionId'); return join(this.directory, `${fileKey(sessionId)}.json`); }
  async transaction<T>(sessionId: string, action: (data: DraftData) => T | Promise<T>): Promise<T> {
    const path = this.path(sessionId);
    return serialized(path, async () => {
      const data = await readJSON<DraftData>(path, () => ({ version: 1, partitions: {}, receipts: {} }));
      if (data.version !== 1 || !data.partitions || !data.receipts) throw new WorkServiceError('corrupt_store', 'Unsupported draft store');
      const before = canonical(data); const result = await action(data);
      if (canonical(data) !== before) await atomicWrite(path, data);
      return structuredClone(result);
    });
  }
}
