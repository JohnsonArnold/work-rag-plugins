import type { OperationDTO, WorkMutation, WorkQuery, WorkReply, TaskDTO, TaskLogDTO } from '../shared/contracts.ts';
import { parseWorkCommand, WORK_HELP } from './command-parser.ts';

export interface CommandWorkService {
  query(sessionId: string, input: WorkQuery, signal?: AbortSignal): Promise<WorkReply>;
  submit(sessionId: string, input: WorkMutation, submissionId: string, signal?: AbortSignal): Promise<OperationDTO>;
  operation(sessionId: string, id: string): Promise<OperationDTO>;
  operations(sessionId: string, limit?: number, offset?: number): Promise<OperationDTO[]>;
  retry(sessionId: string, id: string, signal?: AbortSignal): Promise<OperationDTO>;
}
export type WorkCommandInvocation = { commandId: string; agent: { id?: string; session?: { sessionId?: string } }; rawInput: string; signal: AbortSignal };
export type WorkCommandResult = { kind: 'success' | 'error'; text: string };
export type WorkCommandDefinition = { name: string; description: string; input: { hint: string }; handler: (input: WorkCommandInvocation) => Promise<WorkCommandResult> };
export interface WorkCommandContext { commands: { register(definition: WorkCommandDefinition): () => void } }
export function formatOperation(operation: OperationDTO): string {
  const ack = operation.result;
  return [`operation_id: ${operation.operation_id}`, `state: ${operation.state}`, ...(ack ? [`task_id: ${ack.task_id}`, `revision: ${ack.revision}`, ...(ack.status ? [`status: ${ack.status}`] : []), ...(ack.log_id ? [`log_id: ${ack.log_id}`] : [])] : []), ...(operation.error ? [`${operation.error.code}: ${operation.error.message}`, ...(operation.error.details ? [JSON.stringify(operation.error.details)] : [])] : []), ...(operation.retryable ? [`结果可通过 /work retry ${operation.operation_id} 确认；勿重新录入。`] : [])].join('\n');
}
function errorText(error: unknown): string {
  if (error && typeof error === 'object' && 'message' in error) {
    const value = error as { message: unknown; code?: unknown; layer?: unknown; details?: unknown };
    return `${value.code ? `${value.layer ? String(value.layer) + '/' : ''}${String(value.code)}: ` : ''}${String(value.message)}${value.details ? '\n' + JSON.stringify(value.details) : ''}`;
  }
  return String(error);
}
function warningText(reply: WorkReply): string { return reply.warnings.length ? `\n提示：${reply.warnings.map(w => typeof w === 'string' ? w : JSON.stringify(w)).join('；')}` : ''; }
export function createWorkCommandHandler(work: CommandWorkService, getSessionId = (invocation: WorkCommandInvocation) => invocation.agent.session?.sessionId ?? invocation.agent.id): WorkCommandDefinition['handler'] {
  return async invocation => {
    try {
      const command = parseWorkCommand(invocation.rawInput);
      if (command.kind === 'help') return { kind: 'success', text: WORK_HELP };
      const sessionId = getSessionId(invocation);
      if (!sessionId) return { kind: 'error', text: '当前事项命令未绑定会话，入口尚未就绪。' };
      if (command.kind === 'operations') {
        const rows = await work.operations(sessionId, command.limit, command.offset);
        return { kind: 'success', text: rows.length ? rows.map(formatOperation).join('\n\n') + `\n分页 offset=${command.offset} limit=${command.limit}` : '此页没有操作回执。' };
      }
      if (command.kind === 'retry') {
        const op = await work.retry(sessionId, command.operation_id, invocation.signal);
        return { kind: op.state === 'REJECTED' ? 'error' : 'success', text: formatOperation(op) };
      }
      if (command.kind === 'list') {
        const reply = await work.query(sessionId, { kind: 'list', ...(command.view === 'all' ? {} : command.view === 'completed' ? { status: 'completed' as const } : { view: 'unfinished' }), query: command.query, limit: command.limit, offset: command.offset }, invocation.signal);
        const tasks = reply.data as TaskDTO[];
        return { kind: 'success', text: (tasks.length ? tasks.map(t => `${t.task_id} | ${t.status} | r${t.revision} | ${t.title}`).join('\n') : '此页没有事项。') + `\n分页 offset=${command.offset} limit=${command.limit}` + warningText(reply) };
      }
      if (command.kind === 'show') {
        const reply = await work.query(sessionId, { kind: 'get', task_id: command.task_id }, invocation.signal);
        const task = reply.data as TaskDTO;
        let text = `${task.task_id}\nstatus: ${task.status}\nrevision: ${task.revision}\n标题：${task.title}\n内容：${task.content}\n截止：${task.due ?? '无'}\n标签：${task.tags?.join('、') ?? ''}` + warningText(reply);
        try {
          const logs = await work.query(sessionId, { kind: 'logs', task_id: command.task_id, after: command.after, limit: command.limit }, invocation.signal);
          const items = logs.data as TaskLogDTO[];
          text += '\n跟进（与详情分别读取）：\n' + (items.map(l => `${l.log_id} | r${l.task_revision}\n${l.content}`).join('\n') || '无') + warningText(logs);
          if (items.length) text += `\n续读 --after ${items[items.length - 1]!.task_revision}`;
        } catch (error) { text += `\n跟进读取失败：${errorText(error)}`; }
        return { kind: 'success', text };
      }
      let mutation: WorkMutation;
      if (command.kind === 'add') mutation = { kind: 'create', title: command.title, content: command.content, tags: command.tags, ...(command.due === undefined ? {} : { due: command.due }) };
      else if (command.kind === 'follow') mutation = { kind: 'follow', task_id: command.task_id, content: command.content };
      else mutation = { kind: 'set_status', task_id: command.task_id, status: command.kind === 'start' ? 'in_progress' : command.kind === 'done' ? 'completed' : 'pending' };
      // WorkService freezes a first-read revision, and resolves commandId replay before any new read.
      const operation = await work.submit(sessionId, mutation, invocation.commandId, invocation.signal);
      return { kind: operation.state === 'REJECTED' ? 'error' : 'success', text: formatOperation(operation) + (command.kind === 'add' && command.usedTitleAsContent ? '\n未指定正文，已使用标题作为内容。' : '') };
    } catch (error) {
      return { kind: 'error', text: `${errorText(error)}\n使用 /work help 查看用法；输入未发送给模型。` };
    }
  };
}
export function registerWorkCommands(ctx: WorkCommandContext, work: CommandWorkService): () => void {
  return ctx.commands.register({ name: 'work', description: '直接记录、跟进和查询事项（不调用模型）', input: { hint: 'help / add / show / follow / start / done / reopen / list / operations / retry' }, handler: createWorkCommandHandler(work) });
}
