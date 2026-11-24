import { z } from 'zod';
import { createTypertRemoteInfo } from '@deepseek-ai/dsh-typert-protocol/remote-info';

export const REMOTE_METHODS = ['health', 'query', 'submit', 'operation', 'operations', 'retry', 'drafts', 'saveDraft', 'discardDraft', 'submitDraft'] as const;
const codec = (name: string, create: () => z.ZodType) => ({ mode: 'strict' as const, typeSymbol: `work-rag-dsh-work#${name}`, create });
const agentCodec = { ...codec('agentId', () => z.string().min(1)), typeSymbol: '@deepseek-ai/dsh-session/types#SessionId' };
const jsonCodec = codec('json', () => z.json());
// Hand-written wire contracts registered through the supported Typert contribution API.
// The agent lookup preserves the host's normal session resolution/access policy.
export const workRemoteInfo = createTypertRemoteInfo({
  package: 'work-rag-dsh-work',
  methods: Object.fromEntries(REMOTE_METHODS.map(method => [`work/${method}`, {
    service: 'work', invocation: { kind: 'direct' },
    scope: { context: 'agent', wire: 'agentId' },
    parameters: [
      { name: 'agent', wire: 'agentId', source: 'lookup', lookup: 'agent', codec: agentCodec, strictCodec: agentCodec },
      { name: 'input', wire: 'input', source: 'json', codec: jsonCodec, strictCodec: jsonCodec },
    ],
    cancellation: { parameter: 'signal' }, result: jsonCodec,
  }])) as any,
}, z);

export const hostContribution = {
  package: 'work-rag-dsh-work', face: 'host', schemas: [],
  model: { services: [], events: [], objects: [] },
  invocations: workRemoteInfo.descriptors,
};
