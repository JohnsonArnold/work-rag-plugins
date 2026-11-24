import { mkdir, open, readFile, rename, unlink } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { createHash, randomUUID } from 'node:crypto';

// One DSH host process owns these stores. All service/store instances in that
// process share the same queue; arbitrary multi-process writers are not supported.
const queues = new Map<string, Promise<unknown>>();
export async function serialized<T>(scope: string, action: () => Promise<T>): Promise<T> {
  const key = resolve(scope); const previous = queues.get(key) ?? Promise.resolve();
  const next = previous.catch(() => {}).then(action);
  queues.set(key, next);
  try { return await next; } finally { if (queues.get(key) === next) queues.delete(key); }
}
export function fileKey(value: string): string { return createHash('sha256').update(value).digest('hex'); }
async function fsyncDirectory(path: string): Promise<void> {
  const handle = await open(path, 'r'); try { await handle.sync(); } finally { await handle.close(); }
}
export async function durableDirectory(path: string): Promise<void> {
  const absolute = resolve(path);
  try { await mkdir(absolute, { mode: 0o700 }); await fsyncDirectory(dirname(absolute)); }
  catch (e) {
    if ((e as NodeJS.ErrnoException).code === 'ENOENT') { await durableDirectory(dirname(absolute)); await durableDirectory(absolute); }
    else if ((e as NodeJS.ErrnoException).code !== 'EEXIST') throw e;
  }
}
export async function atomicWrite(path: string, value: unknown): Promise<void> {
  const absolute = resolve(path); await durableDirectory(dirname(absolute));
  const temporary = `${absolute}.${randomUUID()}.tmp`; let published = false;
  try {
    const handle = await open(temporary, 'wx', 0o600);
    try { await handle.writeFile(`${JSON.stringify(value)}\n`, 'utf8'); await handle.sync(); } finally { await handle.close(); }
    await rename(temporary, absolute); published = true; await fsyncDirectory(dirname(absolute));
  } finally {
    // temporary is derived from the verified absolute destination and a UUID;
    // never remove the published destination on an fsync failure.
    if (!published) await unlink(temporary).catch(e => { if (e.code !== 'ENOENT') throw e; });
  }
}
export async function readJSON<T>(path: string, initial: () => T): Promise<T> {
  try { return JSON.parse(await readFile(path, 'utf8')) as T; }
  catch (e) { if ((e as NodeJS.ErrnoException).code === 'ENOENT') return initial(); throw e; }
}
