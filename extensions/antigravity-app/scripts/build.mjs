import { build } from 'esbuild';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const repoRoot = resolve(root, '../..');

const jsResult = await build({
  entryPoints: [resolve(root, 'src/main.tsx')],
  bundle: true,
  minify: true,
  format: 'iife',
  platform: 'browser',
  target: 'es2022',
  jsx: 'automatic',
  write: false,
});

const jsCode = jsResult.outputFiles[0].text.replace(/<\/script>/gi, '<\\/script>');
const cssCode = await readFile(resolve(root, 'src/styles.css'), 'utf8');

const html = `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Work-RAG</title>
  <style>${cssCode}</style>
</head>
<body>
  <div id="root"></div>
  <script>${jsCode}</script>
</body>
</html>
`;

const targets = [
  resolve(root, 'dist/panel.html'),
  resolve(repoRoot, 'work_rag/assets/panel.html'),
];

for (const target of targets) {
  await mkdir(dirname(target), { recursive: true });
  await writeFile(target, html, 'utf8');
}

console.log(`Built single-file MCP App HTML (${Buffer.byteLength(html, 'utf8')} bytes) -> ${targets.join(', ')}`);
