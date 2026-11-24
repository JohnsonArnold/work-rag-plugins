import { WorkPanel } from './WorkPanel.tsx';
import { sessionApi } from './api-transport.ts';
import { workRemoteInfo } from '../shared/remote-info.ts';
export const name = 'work-rag-work-client';
export const inject = ['remote', 'sidebarRightTabs', 'slots'];
export function apply(ctx: any) {
  ctx.effect(async () => {
    const unmount = await ctx.remote.$mount(workRemoteInfo);
    const ui = ctx.inject(['remote.work', 'sidebarRightTabs', 'slots'], (scope: any) => {
      const remote = scope.remote.work;
      scope.effect(() => scope.sidebarRightTabs.register({
        id: 'work-rag-work', kind: 'work-rag-work', title: () => '事项', multiple: false,
        guide: [{ id: 'work-rag-work-open', order: 30, title: () => '事项', description: () => '新建、跟进、完成与跨设备草稿，不调用模型' }],
      }), 'work: sidebar tab');
      scope.effect(() => scope.slots.inject('sidebar.right.pane.tab', () => scope.slots.register({
        name: 'sidebar.right.pane.tab', key: 'work-rag-work',
        inject: (sessionId: string) => ({ sessionId, api: sessionApi(remote, sessionId) }),
      }, WorkPanel)), 'work: sidebar body');
    });
    try {
      await ui;
    } catch (error) {
      await ui.dispose();
      await unmount();
      throw error;
    }
    return async () => { await ui.dispose(); await unmount(); };
  }, 'work: Remote and session sidebar');
}
