import { build } from 'esbuild';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';
import { mkdir } from 'node:fs/promises';
const runtime = process.env.DSH_PACKAGE_ROOT ?? '/usr/local/lib/node_modules/@deepseek-ai/dsh';
const req = createRequire(resolve(runtime, 'package.json'));
const runtimeResolve = {
  name: 'dsh-public-runtime',
  setup(b) {
    b.onResolve({ filter: /^@deepseek-ai\// }, args => ({ path: req.resolve(args.path) }));
  },
};
await mkdir('lib', { recursive: true });
await mkdir('client', { recursive: true });
await build({ entryPoints: ['src/host/index.ts'], outfile: 'lib/index.js', bundle: true, platform: 'node', format: 'esm', target: 'node22', sourcemap: true, plugins: [runtimeResolve] });
await build({
  entryPoints: ['src/client/index.tsx'], outfile: 'client/client.js', bundle: true,
  platform: 'browser', format: 'cjs', target: 'es2022', jsx: 'automatic',
  external: ['react', 'react/jsx-runtime', 'react-dom'], plugins: [runtimeResolve],
  banner: { js: 'window.__ModuleLoader__.load({ id: "work-rag-dsh-work", factory: (require) => { const module = {exports:{}}; const exports = module.exports;' },
  footer: { js: 'return module.exports; }});' },
  sourcemap: true,
});
console.log('Built host and DSH client bundles');
