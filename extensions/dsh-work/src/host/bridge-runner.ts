import { spawn } from 'node:child_process';
import { isAbsolute, join } from 'node:path';
import type { BridgeExecutor, BridgeRequest, BridgeResponse } from '../shared/contracts.ts';
import { parseResponse, WorkServiceError } from '../shared/schema.ts';

export type BridgeRunnerOptions = { projectRoot: string; dataDir: string; bridgeTimeoutMs?: number };
export class BridgeRunner implements BridgeExecutor {
  readonly dataDir: string;
  readonly timeoutMs: number;
  private readonly launcher: string;
  constructor(options: BridgeRunnerOptions) {
    if (!isAbsolute(options.projectRoot)) throw new WorkServiceError('invalid_config', 'projectRoot must be absolute');
    if (!isAbsolute(options.dataDir)) throw new WorkServiceError('invalid_config', 'dataDir must be absolute');
    this.launcher = join(options.projectRoot, 'tools', 'run_local_bridge.sh');
    this.dataDir = options.dataDir; this.timeoutMs = options.bridgeTimeoutMs ?? 30_000;
    if (!Number.isInteger(this.timeoutMs) || this.timeoutMs < 5_000 || this.timeoutMs > 120_000) throw new WorkServiceError('invalid_config', 'bridgeTimeoutMs must be 5000–120000');
  }
  async run(request: BridgeRequest, signal?: AbortSignal): Promise<BridgeResponse> {
    const input = Buffer.from(JSON.stringify(request) + '\n');
    if (input.length > 1024 * 1024) throw new WorkServiceError('input_too_large', 'Bridge input exceeds 1 MiB');
    if (signal?.aborted) throw new WorkServiceError('aborted', 'Bridge call cancelled before launch');
    return new Promise((resolve, reject) => {
      // Never accept executable, shell text, script path or environment overrides
      // from commands, Remote payloads or stored drafts.
      const child = spawn('/bin/sh', [this.launcher, this.dataDir], { stdio: ['pipe', 'pipe', 'pipe'] });
      const chunks: Buffer[] = []; let size = 0; let stderr = Buffer.alloc(0); let failure: WorkServiceError | undefined; let finished = false;
      const kill = (error: WorkServiceError) => { failure ??= error; child.kill('SIGKILL'); };
      const abort = () => kill(new WorkServiceError('aborted', 'Bridge call cancelled; mutation outcome may be unknown'));
      const timeout = setTimeout(() => kill(new WorkServiceError('timeout', 'Bridge timed out; mutation outcome may be unknown')), this.timeoutMs);
      signal?.addEventListener('abort', abort, { once: true });
      const cleanup = () => { clearTimeout(timeout); signal?.removeEventListener('abort', abort); };
      child.stdout.on('data', (chunk: Buffer) => {
        size += chunk.length;
        if (size > 8 * 1024 * 1024) kill(new WorkServiceError('output_too_large', 'Bridge output exceeds 8 MiB'));
        else chunks.push(chunk);
      });
      child.stderr.on('data', (chunk: Buffer) => { stderr = Buffer.concat([stderr, chunk]).subarray(-64 * 1024); });
      child.stdin.on('error', (e: Error) => { failure ??= new WorkServiceError('stdin_failed', e.message); });
      child.on('error', (e: Error) => {
        if (finished) return; finished = true; cleanup(); reject(new WorkServiceError('spawn_failed', e.message));
      });
      child.on('close', (code, terminationSignal) => {
        if (finished) return; finished = true; cleanup();
        if (failure) { reject(failure); return; }
        if (code !== 0) { reject(new WorkServiceError('process_failed', 'Bridge process failed', { exit_code: code, signal: terminationSignal, stderr: stderr.toString('utf8') })); return; }
        try {
          const decoded = new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks));
          resolve(parseResponse(JSON.parse(decoded), request));
        } catch (e) { reject(e instanceof WorkServiceError ? e : new WorkServiceError('invalid_response', 'Bridge returned invalid or incomplete UTF-8 JSON')); }
      });
      child.stdin.end(input);
      // Cover abort between the preflight check and listener registration.
      if (signal?.aborted) abort();
    });
  }
}
