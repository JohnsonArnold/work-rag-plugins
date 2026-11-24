import { join, resolve } from 'node:path';
import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { BridgeRunner } from './bridge-runner.ts';
import { OperationLedger } from './operation-ledger.ts';
import { WorkService } from './work-service.ts';
import { DraftStore } from './draft-store.ts';
import { DraftService } from './draft-service.ts';
import { WorkRemote } from './work-remote.ts';
import { registerWorkCommands } from './commands.ts';
import { hostContribution } from '../shared/remote-info.ts';

export const name = 'work-rag-work';
export const inject = ['commands', 'typert'];

function existingDefault(current: string, previous: string): string {
  return !existsSync(current) && existsSync(previous) ? previous : current;
}

export function apply(ctx: any, config: { projectRoot?: string; dataDir?: string; stateDir?: string; bridgeTimeoutMs?: number } = {}) {
  // Explicit project binding: installing the bundle elsewhere must never select a different empty library.
  const sourceRoot = resolve(import.meta.dirname, '../../..');
  const projectRoot = config.projectRoot ?? process.env.WORK_RAG_PROJECT_ROOT ?? process.env.IT_RAG_PROJECT_ROOT ??
    (existsSync(join(sourceRoot, 'tools/run_local_bridge.sh')) ? sourceRoot : existingDefault('/root/work-rag', '/root/IT-rag'));
  const dataDir = config.dataDir ?? process.env.WORK_RAG_DATA_DIR ?? process.env.IT_RAG_DATA_DIR ??
    existingDefault(join(projectRoot, '.work-rag-data'), join(projectRoot, '.it-rag-data'));
  const dshState = resolve(homedir(), '.dsh');
  const stateDir = resolve(config.stateDir ?? process.env.WORK_RAG_WORK_STATE_DIR ?? process.env.IT_RAG_WORK_STATE_DIR ??
    existingDefault(join(dshState, 'work-rag-work'), join(dshState, 'it-rag-work')));
  const runner = new BridgeRunner({ projectRoot, dataDir, bridgeTimeoutMs: config.bridgeTimeoutMs });
  const work = new WorkService(runner, new OperationLedger(resolve(stateDir, 'receipts')));
  const drafts = new DraftService(work, new DraftStore(resolve(stateDir, 'drafts')));
  new WorkRemote(ctx, work, drafts);
  ctx.effect(() => ctx.typert.register(hostContribution), 'work: strict Remote contracts');
  registerWorkCommands(ctx, work);
  return { work, drafts, dataDir, stateDir };
}
