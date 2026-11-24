import { useEffect, useMemo, useSyncExternalStore } from 'react';
import type { WorkApi } from './work-api.ts';
import { WorkStore } from './work-store.ts';
export function useWorkController(sessionId: string, api: WorkApi) {
  const store = useMemo(() => {
    let storage: Storage | undefined;
    try { storage = typeof window === 'undefined' ? undefined : window.localStorage; } catch { /* Memory-only mode is announced by the panel. */ }
    return new WorkStore(sessionId, api, storage);
  }, [sessionId, api]);
  const state = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
  useEffect(() => {
    store.resume();
    store.activate('create');
    const online = () => { store.setOnline(typeof navigator === 'undefined' || navigator.onLine); if (typeof navigator === 'undefined' || navigator.onLine) void store.reconnect(); };
    const focus = () => { if (typeof navigator === 'undefined' || navigator.onLine) void store.reconnect(); };
    online();
    if (typeof window !== 'undefined') { window.addEventListener('online', online); window.addEventListener('offline', online); window.addEventListener('focus', focus); }
    return () => { if (typeof window !== 'undefined') { window.removeEventListener('online', online); window.removeEventListener('offline', online); window.removeEventListener('focus', focus); } store.dispose(); };
  }, [store]);
  useEffect(() => {
    if (!state.online) return;
    const dirty = Object.values(state.drafts).filter(d => d.dirty && !d.conflict && !d.submitting && !d.error);
    if (!dirty.length) return;
    const timer = setTimeout(() => { for (const draft of dirty) void store.save(draft.key); }, 800);
    return () => clearTimeout(timer);
  }, [store, state.drafts, state.online]);
  return { state, store };
}
