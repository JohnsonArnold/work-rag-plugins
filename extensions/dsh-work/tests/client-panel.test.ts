import test from 'node:test';
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdtemp, realpath, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createRequire } from 'node:module';

test('React panel renders literal draft/conflict text and real controls without a DSH/model runtime', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'dsh-work-panel-')); const output = join(dir, 'panel.cjs');
  try {
    await build({ stdin: { contents: `import React from 'react'; import {renderToStaticMarkup} from 'react-dom/server'; import {WorkPanel} from './src/client/WorkPanel.tsx'; export const render = (api) => renderToStaticMarkup(React.createElement(WorkPanel, {sessionId:'session_test',api}));`, resolveDir: process.cwd(), loader: 'tsx' }, outfile: output, bundle: true, platform: 'node', format: 'cjs', logLevel: 'silent' });
    const unsafe = '<script>alert("bad")</script>\n**不渲染 Markdown**';
    const saved = { sessionId: 'session_test', drafts: { create: { key: 'create', kind: 'create', revision: 1, payload: { title: unsafe, content: unsafe }, dirty: true, version: 1, conflict: { draft_id: 'draft_server', kind: 'create', key: 'create', draft_revision: 2, payload: { title: unsafe }, binding: { library_id: 'a', restore_generation: 'b' } } } } };
    const previous = Object.getOwnPropertyDescriptor(globalThis, 'window');
    Object.defineProperty(globalThis, 'window', { configurable: true, value: { localStorage: { getItem: () => JSON.stringify(saved), setItem() {} } } });
    try {
      const { render } = createRequire(import.meta.url)(output); const html: string = render({});
      assert.ok(!html.includes('<script>')); assert.match(html, /&lt;script&gt;/); assert.match(html, /\*\*不渲染 Markdown\*\*/);
      for (const label of ['事项筛选', '关键词', '同步草稿', '正式提交', '有其他设备修改', '保留本地并基于新修订保存']) assert.ok(html.includes(label), label);
      assert.match(html, /<textarea/); assert.match(html, /disabled=""/);
    } finally { if (previous) Object.defineProperty(globalThis, 'window', previous); else Reflect.deleteProperty(globalThis, 'window'); }
  } finally {
    const resolved = await realpath(dir); assert.ok(resolved.startsWith(join(await realpath(tmpdir()), 'dsh-work-panel-'))); await rm(resolved, { recursive: true, force: true });
  }
});
