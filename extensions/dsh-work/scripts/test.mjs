import { spawnSync } from 'node:child_process';
import { mkdtemp, readdir, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { build } from 'esbuild';

const output = await mkdtemp(join(process.cwd(), '.test-'));
try {
  const tests = (await readdir('tests')).filter(file => file.endsWith('.test.ts')).sort();
  await build({
    entryPoints: tests.map(file => join('tests', file)),
    outdir: output,
    outExtension: { '.js': '.mjs' },
    bundle: true,
    packages: 'external',
    platform: 'node',
    format: 'esm',
    sourcemap: 'inline',
  });
  const result = spawnSync(process.execPath, [
    '--enable-source-maps', '--test', ...tests.map(file => join(output, file.replace(/\.ts$/, '.mjs'))),
  ], { stdio: 'inherit' });
  if (result.error) throw result.error;
  process.exitCode = result.status ?? 1;
} finally {
  await rm(output, { recursive: true, force: true });
}
