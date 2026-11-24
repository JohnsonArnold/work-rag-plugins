/** Literal argument parser: never evaluates shell syntax or expands environment values. */
export class CommandSyntaxError extends Error {
  constructor(message: string) { super(message); this.name = 'CommandSyntaxError'; }
}
export type ParsedWorkCommand =
  | { kind: 'help' }
  | { kind: 'add'; title: string; content: string; due?: string; tags: string[]; usedTitleAsContent: boolean }
  | { kind: 'show'; task_id: string; after: number; limit: number }
  | { kind: 'follow'; task_id: string; content: string }
  | { kind: 'start' | 'done' | 'reopen'; task_id: string }
  | { kind: 'list'; view: 'unfinished' | 'completed' | 'all'; query?: string; limit: number; offset: number }
  | { kind: 'operations'; limit: number; offset: number }
  | { kind: 'retry'; operation_id: string };

export const WORK_HELP = `/work help
/work add TITLE [--content TEXT] [--due YYYY-MM-DD|ISO带时区] [--tag TAG]（可重复）
/work show TASK [--after REV] [--limit N]
/work follow TASK TEXT
/work start|done|reopen TASK
/work list [unfinished|completed|all] [--query TEXT] [--limit N] [--offset N]
/work operations [--limit N] [--offset N]
/work retry OPERATION
使用完整事项编号 task_ + 32 位十六进制字符。支持中文、单双引号、多行、\\n/\\t/转义引号，以及 -- 后的字面参数。跟进与状态分别保存，不调用模型。`;

interface Token { value: string; literal: boolean }
function tokenize(input: string): Token[] {
  const out: Token[] = []; let value = ''; let active = false; let literal = false; let quote = '';
  for (let i = 0; i < input.length; i++) {
    const c = input[i]!;
    if (c === '\\') {
      if (i + 1 === input.length) throw new CommandSyntaxError('末尾转义符缺少字符');
      const next = input[++i]!; value += next === 'n' ? '\n' : next === 't' ? '\t' : next === 'r' ? '\r' : next;
      active = true; literal = true;
    } else if (quote) {
      if (c === quote) quote = ''; else value += c;
    } else if (c === '"' || c === "'") { quote = c; active = true; literal = true; }
    else if (/\s/u.test(c)) { if (active) { out.push({ value, literal }); value = ''; active = false; literal = false; } }
    else { value += c; active = true; }
  }
  if (quote) throw new CommandSyntaxError('引号未闭合');
  if (active) out.push({ value, literal });
  return out;
}
export function tokenizeWorkInput(input: string): string[] { return tokenize(input).map(t => t.value); }
function integer(value: string | undefined, label: string, fallback: number, max = Number.MAX_SAFE_INTEGER): number {
  if (value === undefined) return fallback;
  if (!/^\d+$/u.test(value) || !Number.isSafeInteger(Number(value)) || Number(value) > max || (label === 'limit' && Number(value) < 1))
    throw new CommandSyntaxError(`${label} 必须是${label === 'limit' ? '1–' + max : '非负'}整数`);
  return Number(value);
}
function required(value: string | undefined, label: string): string {
  if (!value?.trim()) throw new CommandSyntaxError(`缺少${label}`); return value;
}
function taskId(value: string | undefined): string {
  if (!/^task_[a-f0-9]{32}$/u.test(value ?? '')) throw new CommandSyntaxError('请使用完整事项编号 task_ + 32 位十六进制字符');
  return value!;
}
function explicitDate(value: string): string {
  const dateOnly = /^\d{4}-\d{2}-\d{2}$/u.test(value);
  if ((!dateOnly && !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})$/u.test(value)) || Number.isNaN(Date.parse(value)))
    throw new CommandSyntaxError('截止时间须为 YYYY-MM-DD 或带明确时区的 ISO 时间');
  const calendarDate = value.slice(0, 10);
  if (new Date(calendarDate).toISOString().slice(0, 10) !== calendarDate) throw new CommandSyntaxError('截止日期无效');
  return value;
}
export function parseWorkCommand(rawInput: string): ParsedWorkCommand {
  const tokens = tokenize(rawInput.replace(/^\s*\/work(?=\s|$)/u, ''));
  const kind = tokens.shift()?.value || 'help';
  const allowed: Record<string, string[]> = { help: [], add: ['content', 'due', 'tag'], show: ['after', 'limit'], follow: [], start: [], done: [], reopen: [], list: ['query', 'limit', 'offset'], operations: ['limit', 'offset'], retry: [] };
  if (!Object.hasOwn(allowed, kind)) throw new CommandSyntaxError(`未知子命令：${kind}`);
  const positional: string[] = []; const options: Record<string, string[]> = {}; let end = false;
  for (let i = 0; i < tokens.length; i++) {
    const token = tokens[i]!;
    if (!end && !token.literal && token.value === '--') { end = true; continue; }
    if (!end && !token.literal && token.value.startsWith('--')) {
      const match = /^--([^=]+)(?:=(.*))?$/su.exec(token.value);
      if (!match) throw new CommandSyntaxError('选项名称不能为空');
      const name = match[1]!;
      if (!allowed[kind]!.includes(name)) throw new CommandSyntaxError(`不支持参数 --${name}`);
      const next = tokens[i + 1];
      const value = match[2] ?? (next && (next.literal || !next.value.startsWith('--')) ? tokens[++i]!.value : undefined);
      if (value === undefined) throw new CommandSyntaxError(`--${name} 缺少值`);
      if (name !== 'tag' && options[name]) throw new CommandSyntaxError(`--${name} 不能重复`);
      (options[name] ??= []).push(value);
    } else positional.push(token.value);
  }
  const option = (name: string) => options[name]?.[0];
  const count = (min: number, max = min) => { if (positional.length < min || positional.length > max) throw new CommandSyntaxError(`${kind} 的位置参数数量不正确（含空格请加引号）`); };
  const paging = () => ({ limit: integer(option('limit'), 'limit', 50, 200), offset: integer(option('offset'), 'offset', 0) });
  switch (kind) {
    case 'help': count(0); return { kind };
    case 'add': { count(1); const title = required(positional[0], '标题'); const content = option('content'); return { kind, title, content: content === undefined ? title : required(content, '内容'), tags: options.tag ?? [], ...(option('due') === undefined ? {} : { due: explicitDate(option('due')!) }), usedTitleAsContent: content === undefined }; }
    case 'show': count(1); return { kind, task_id: taskId(positional[0]), after: integer(option('after'), 'after', 0), limit: integer(option('limit'), 'limit', 50, 200) };
    case 'follow': count(2, Number.MAX_SAFE_INTEGER); return { kind, task_id: taskId(positional[0]), content: required(positional.slice(1).join(' '), '跟进内容') };
    case 'start': case 'done': case 'reopen': count(1); return { kind, task_id: taskId(positional[0]) };
    case 'list': { count(0, 1); const view = positional[0] ?? 'unfinished'; if (view !== 'unfinished' && view !== 'completed' && view !== 'all') throw new CommandSyntaxError('列表筛选必须为 unfinished、completed 或 all'); return { kind, view, ...(option('query') === undefined ? {} : { query: option('query')! }), ...paging() }; }
    case 'operations': count(0); return { kind, ...paging() };
    case 'retry': count(1); return { kind, operation_id: required(positional[0], 'operation 编号') };
  }
  throw new CommandSyntaxError('无效命令');
}
