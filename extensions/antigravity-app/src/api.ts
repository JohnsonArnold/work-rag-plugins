import type { App } from '@modelcontextprotocol/ext-apps';

export interface Envelope<T> {
  data: T;
  coverage?: string | null;
  warnings?: string[];
}

function parseErrorMessage(raw: string): string {
  try {
    const parsed = JSON.parse(raw);
    if (parsed?.error?.message) {
      return parsed.error.code ? `${parsed.error.code}: ${parsed.error.message}` : String(parsed.error.message);
    }
  } catch {
    // plain text error
  }
  return raw || '操作失败';
}

export function extractEnvelope<T>(result: unknown): Envelope<T> {
  const res = result as {
    isError?: boolean;
    structuredContent?: unknown;
    content?: Array<{ type?: string; text?: string }>;
  };

  if (res?.isError) {
    const text = res.content?.find((item) => typeof item.text === 'string')?.text ?? '';
    throw new Error(parseErrorMessage(text));
  }

  if (res?.structuredContent && typeof res.structuredContent === 'object') {
    const structured = res.structuredContent as Record<string, unknown>;
    if ('data' in structured) {
      return structured as unknown as Envelope<T>;
    }
    return { data: structured as unknown as T };
  }

  const text = res?.content?.find((item) => typeof item.text === 'string')?.text;
  if (text) {
    const parsed = JSON.parse(text);
    if (parsed && typeof parsed === 'object' && 'data' in parsed) {
      return parsed as Envelope<T>;
    }
    return { data: parsed as T };
  }

  throw new Error('MCP 工具返回了空结果');
}

export async function callAppTool<T>(
  app: App,
  name: string,
  args: Record<string, unknown> = {},
): Promise<Envelope<T>> {
  const cleanArgs = Object.fromEntries(
    Object.entries(args).filter(([, value]) => value !== undefined),
  );
  const raw = await app.callServerTool({ name, arguments: cleanArgs });
  return extractEnvelope<T>(raw);
}

export function makeRequestId(prefix: string): string {
  const rand = typeof crypto !== 'undefined' && crypto.randomUUID
    ? crypto.randomUUID().replaceAll('-', '')
    : `${Date.now()}_${Math.random().toString(16).slice(2)}`;
  return `agy_${prefix}_${rand}`;
}
