import type { Binding, DraftDTO, DraftSave, OperationDTO, WorkMutation, WorkQuery, WorkReply } from '../shared/contracts.ts';
/** Session-bound transport. Parent integration adapts the existing DSH Remote, without model calls. */
export interface WorkApi {
  query(input: WorkQuery, signal?: AbortSignal): Promise<WorkReply>;
  submit(input: WorkMutation, submissionId: string, binding: Binding, signal?: AbortSignal): Promise<OperationDTO>;
  operation(id: string): Promise<OperationDTO>;
  operations(limit?: number, offset?: number): Promise<OperationDTO[]>;
  retry(id: string, signal?: AbortSignal): Promise<OperationDTO>;
  drafts(): Promise<DraftDTO[]>;
  saveDraft(input: DraftSave): Promise<DraftDTO>;
  discardDraft(id: string, expectedDraftRevision: number, mutationId?: string): Promise<DraftDTO>;
  submitDraft(id: string, expectedDraftRevision: number, signal?: AbortSignal): Promise<OperationDTO>;
}
export function workErrorText(error: unknown): string {
  if (error && typeof error === 'object' && 'message' in error) {
    const value = error as { message: unknown; code?: unknown; layer?: unknown; details?: unknown };
    return `${value.code ? `${value.layer ? String(value.layer) + '/' : ''}${String(value.code)}: ` : ''}${String(value.message)}${value.details ? '\n' + JSON.stringify(value.details) : ''}`;
  }
  return String(error);
}
