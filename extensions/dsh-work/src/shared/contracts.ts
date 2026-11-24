export const PROTOCOL_VERSION = '1' as const;
export const CONTRACT_VERSION = '1.0' as const;
export type Binding = { library_id: string; restore_generation: string };
export type TaskStatus = 'pending' | 'in_progress' | 'completed' | 'cancelled';
export type WorkError = { layer: string; code: string; message: string; details?: Record<string, unknown> };
export type TaskDTO = { task_id: string; title: string; content: string; revision: number; status: TaskStatus; due?: string | null; tags?: string[]; [key: string]: unknown };
export type TaskLogDTO = { log_id: string; task_id: string; task_revision: number; content: string; [key: string]: unknown };
export type MutationAck = { task_id: string; revision: number; status?: TaskStatus; log_id?: string };
export type WorkQuery =
  | { kind: 'get'; task_id: string }
  | { kind: 'list'; status?: TaskStatus; view?: string; query?: string; limit?: number; offset?: number }
  | { kind: 'logs'; task_id: string; after?: number; limit?: number };
export type TaskChanges = { title?: string; content?: string; due?: string | null; tags?: string[]; status?: TaskStatus };
export type WorkMutation =
  | { kind: 'create'; title: string; content: string; due?: string | null; tags?: string[] }
  | { kind: 'edit'; task_id: string; expected_revision?: number; changes: TaskChanges }
  | { kind: 'follow'; task_id: string; expected_revision?: number; content: string }
  | { kind: 'set_status'; task_id: string; expected_revision?: number; status: Exclude<TaskStatus, 'cancelled'> };
export type BridgeOperation = 'task.get' | 'task.list' | 'task.logs' | 'task.create' | 'task.update' | 'task.append_log';
export type BridgeRequest = { protocol_version: '1'; call_id: string; operation: BridgeOperation; binding?: Binding; params: Record<string, unknown> };
export type CoreResult<T = unknown> = { contract_version: '1.0'; data: T; warnings: unknown[]; next_cursor: string | null };
export type BridgeResponse<T = unknown> =
  | { protocol_version: '1'; call_id: string; binding: Binding; ok: true; result: CoreResult<T> }
  | { protocol_version: '1'; call_id: string; binding?: Binding; ok: false; error: WorkError };
export type WorkReply<T = unknown> = CoreResult<T> & { binding: Binding };
export interface BridgeExecutor { run(request: BridgeRequest, signal?: AbortSignal): Promise<BridgeResponse> }
export type OperationState = 'PREPARED' | 'RUNNING' | 'SUCCEEDED' | 'REJECTED' | 'UNCERTAIN';
export type OperationDTO = {
  operation_id: string; submission_id: string; source: 'command' | 'draft'; state: OperationState;
  created_at: string; updated_at: string; binding: Binding; task_id?: string; expected_revision?: number;
  result?: MutationAck; error?: WorkError; retryable: boolean;
};
export type FrozenOperation = OperationDTO & { input: WorkMutation; request: BridgeRequest };
export type DraftKind = 'create' | 'edit' | 'follow';
export type DraftDTO = {
  draft_id: string; kind: DraftKind; key: string; binding: Binding; task_id?: string;
  draft_revision: number; base_task_revision?: number; payload: Record<string, unknown>;
  updated_at: string; deleted?: boolean; operation_id?: string;
};
export type DraftSave = {
  kind: DraftKind; key?: string; draft_id?: string; task_id?: string; expected_draft_revision: number;
  mutation_id: string; payload: Record<string, unknown>; base_task_revision?: number; binding?: Binding;
};
