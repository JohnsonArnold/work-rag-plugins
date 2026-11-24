import { runtimeModule } from './runtime.ts';
import { errorDTO, parseBinding } from '../shared/schema.ts';
import type { WorkService } from './work-service.ts';
import type { DraftService } from './draft-service.ts';
import type { DraftSave, WorkMutation, WorkQuery } from '../shared/contracts.ts';
const { TypertRemoteService } = await runtimeModule('@deepseek-ai/dsh-typert-protocol');
type AgentRef = { id: string };
type Input = Record<string, any>;

/** Direct authenticated DSH Remote service; deliberately has no model/tool executor dependency. */
export class WorkRemote extends TypertRemoteService {
  constructor(ctx: any, private readonly workService: WorkService, private readonly draftService: DraftService) { super(ctx, 'work'); }
  private async result(run: () => unknown): Promise<any> {
    try { return JSON.parse(JSON.stringify({ ok: true, data: await run() })); }
    catch (error: unknown) { return JSON.parse(JSON.stringify({ ok: false, error: errorDTO(error) })); }
  }
  health(_agent: AgentRef, _input: Input, _signal?: AbortSignal) { return this.result(() => ({ protocol_version: '1', contract_version: '1.0', no_model: true })); }
  query(agent: AgentRef, input: WorkQuery, signal?: AbortSignal) { return this.result(() => this.workService.query(agent.id, input, signal)); }
  submit(agent: AgentRef, input: Input, signal?: AbortSignal) { return this.result(() => this.workService.submit(agent.id, input.mutation as WorkMutation, input.submission_id, signal, parseBinding(input.binding))); }
  operation(agent: AgentRef, input: Input, _signal?: AbortSignal) { return this.result(() => this.workService.operation(agent.id, input.operation_id)); }
  operations(agent: AgentRef, input: Input, _signal?: AbortSignal) { return this.result(() => this.workService.operations(agent.id, input.limit, input.offset)); }
  retry(agent: AgentRef, input: Input, signal?: AbortSignal) { return this.result(() => this.workService.retry(agent.id, input.operation_id, signal)); }
  drafts(agent: AgentRef, _input: Input, _signal?: AbortSignal) { return this.result(() => this.draftService.drafts(agent.id)); }
  saveDraft(agent: AgentRef, input: DraftSave, _signal?: AbortSignal) { return this.result(() => this.draftService.saveDraft(agent.id, { ...input, binding: parseBinding(input.binding) })); }
  discardDraft(agent: AgentRef, input: Input, _signal?: AbortSignal) { return this.result(() => this.draftService.discardDraft(agent.id, input.draft_id, input.expected_draft_revision, input.mutation_id)); }
  submitDraft(agent: AgentRef, input: Input, signal?: AbortSignal) { return this.result(() => this.draftService.submitDraft(agent.id, input.draft_id, input.expected_draft_revision, signal)); }
}
