import test from 'node:test';
import assert from 'node:assert/strict';
import { parseWorkCommand, tokenizeWorkInput } from '../src/host/command-parser.ts';
import { createWorkCommandHandler, registerWorkCommands } from '../src/host/commands.ts';
import type { CommandWorkService, WorkCommandDefinition } from '../src/host/commands.ts';
import type { Binding, OperationDTO, TaskDTO, WorkMutation, WorkQuery } from '../src/shared/contracts.ts';
const taskId = 'task_' + 'a'.repeat(32);
const binding: Binding = { library_id: 'library_test', restore_generation: 'generation_test' };
const task: TaskDTO = { task_id: taskId, title: '<b>中文</b>', content: '正文\n第二行', revision: 4, status: 'pending' };
const operation: OperationDTO = { operation_id: 'op_test', submission_id: 'command_test', source: 'command', state: 'SUCCEEDED', created_at: '', updated_at: '', binding, result: { task_id: taskId, revision: 5, status: 'completed' }, retryable: false };
function mockService() {
  const calls: unknown[][] = [];
  const service: CommandWorkService = {
    async query(session, query) { calls.push(['query', session, query]); return { binding, contract_version: '1.0', data: query.kind === 'get' ? task : query.kind === 'logs' ? [{ log_id: 'log_a', task_id: taskId, task_revision: 4, content: '跟进' }] : [task], warnings: [], next_cursor: null }; },
    async submit(session, mutation, submission, signal) { calls.push(['submit', session, mutation, submission, signal]); return operation; },
    async operation() { return operation; }, async operations() { return [operation]; }, async retry(session, id) { calls.push(['retry', session, id]); return operation; },
  };
  return { service, calls };
}
const invocation = (rawInput: string) => ({ rawInput, commandId: 'command_1', agent: { id: 'session_test' }, signal: new AbortController().signal });
test('literal Chinese quoting, escapes, multiline, double dash and empty arguments', () => {
  assert.deepEqual(tokenizeWorkInput(`'中文 标题' "第一行\\n第二行" '' \\"`), ['中文 标题', '第一行\n第二行', '', '"']);
  assert.deepEqual(parseWorkCommand(`add '宽带 跟进' --content "第一行\n第二行" --tag IT --tag '行政 工作' --due 2026-12-01`), { kind: 'add', title: '宽带 跟进', content: '第一行\n第二行', due: '2026-12-01', tags: ['IT', '行政 工作'], usedTitleAsContent: false });
  assert.equal((parseWorkCommand('add -- --content') as { title: string }).title, '--content');
  assert.equal((parseWorkCommand(`follow ${taskId} -- --literal 值`) as { content: string }).content, '--literal 值');
  assert.equal((parseWorkCommand('add 标题') as { content: string }).content, '标题');
});
test('syntax rejects ambiguous IDs, dates, unknown/duplicate flags and malformed quotes', () => {
  for (const input of ['wat', '__proto__', 'add 标题 --=x', 'add 标题 --due 2026-02-30T09:00:00+08:00', 'show task_short', `show ${taskId} --limit 0`, 'add 标题 --due 明天', 'add 标题 --due 2026-02-30', 'add 标题 --due 2026-12-01T10:00', 'add 标题 --content', 'add 标题 --due 2026-12-01 --due 2026-12-02', 'add "未闭合', 'add 标题 --wat 1', 'list done', 'help extra', 'add 标题\\']) assert.throws(() => parseWorkCommand(input), { name: 'CommandSyntaxError' }, input);
  assert.equal(parseWorkCommand('/work help').kind, 'help');
});
test('commands register one direct handler; help and syntax errors never invoke work', async () => {
  const { service, calls } = mockService(); let definition: WorkCommandDefinition | undefined;
  let disposed = false;
  const dispose = registerWorkCommands({ commands: { register(d) { definition = d; return () => { disposed = true; }; } } }, service);
  assert.equal(definition?.name, 'work');
  assert.equal((await definition!.handler(invocation('help'))).kind, 'success');
  assert.equal((await definition!.handler(invocation('show short'))).kind, 'error');
  assert.equal(calls.length, 0); dispose(); assert.equal(disposed, true);
});
test('add/status/follow reuse commandId and leave first-read revision freezing to host', async () => {
  const { service, calls } = mockService(); const handler = createWorkCommandHandler(service);
  assert.match((await handler(invocation('add 中文'))).text, /正文/);
  await handler(invocation(`done ${taskId}`)); await handler(invocation(`follow ${taskId} '处理完成'`));
  assert.equal(calls.filter(c => c[0] === 'query').length, 0);
  assert.deepEqual((calls[1]![2] as WorkMutation), { kind: 'set_status', task_id: taskId, status: 'completed' });
  for (const call of calls) { assert.equal(call[1], 'session_test'); assert.equal(call[3], 'command_1'); }
});
test('show reads bounded logs and list uses completed status, not cancelled', async () => {
  const { service, calls } = mockService(); const handler = createWorkCommandHandler(service);
  const result = await handler(invocation(`show ${taskId} --after 2 --limit 20`));
  assert.match(result.text, /<b>中文<\/b>/); assert.match(result.text, /--after 4/);
  assert.deepEqual(calls[1]![2], { kind: 'logs', task_id: taskId, after: 2, limit: 20 });
  await handler(invocation('list completed --query 中文 --limit 10 --offset 20'));
  assert.deepEqual(calls[2]![2] as WorkQuery, { kind: 'list', status: 'completed', query: '中文', limit: 10, offset: 20 });
});
test('rejected and uncertain operation output preserves recovery id and error details', async () => {
  const { service } = mockService(); service.submit = async () => ({ ...operation, state: 'REJECTED', error: { layer: 'core', code: 'conflict', message: 'revision changed', details: { actual_revision: 6 } } });
  const handler = createWorkCommandHandler(service); const result = await handler(invocation('add 标题'));
  assert.equal(result.kind, 'error'); assert.match(result.text, /actual_revision/); assert.match(result.text, /op_test/);
  service.retry = async () => ({ ...operation, state: 'UNCERTAIN', retryable: true });
  assert.match((await handler(invocation('retry op_test'))).text, /勿重新录入/);
});
