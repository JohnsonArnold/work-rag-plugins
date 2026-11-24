import { createRequire } from 'node:module';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

/** Resolve the running DSH's shared public services, never a second Cordis runtime. */
export async function runtimeModule(name: string) {
  const bases = [import.meta.url, ...(process.argv[1] ? [resolve(process.argv[1])] : [])];
  for (const base of bases) {
    const req = createRequire(base);
    let target: string;
    try { target = req.resolve(name); } catch { continue; }
    return import(pathToFileURL(target).href);
  }
  throw new Error(`Cannot resolve running DSH service ${name}`);
}
