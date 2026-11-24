// Public DSH runtime dependency is supplied by the already running host, not installed twice.
declare module '@deepseek-ai/dsh-typert-protocol/remote-info' {
  export function createTypertRemoteInfo(spec: unknown, zod: unknown): { descriptors: readonly unknown[] };
}
