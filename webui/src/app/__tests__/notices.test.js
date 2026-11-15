// @vitest-environment jsdom
import { expect, it, vi } from 'vitest';
import { flushSync } from 'svelte';
import { createAppNotices } from '../notices.svelte.js';
import { reactiveProps } from '../../components/__tests__/reactiveProps.support.svelte.js';

it('keeps approval attention until resolved and opens the root Session', () => {
  const openSession = vi.fn();
  const context = reactiveProps({ approvalRequests: [], openSession });
  const notices = createAppNotices(context);
  expect(notices.list).toEqual([]);
  context.approvalRequests = [
    {
      id: 'one',
      agent_id: 'worker',
      project_id: null,
      session_id: 'child',
      ancestors: [
        { agent_id: 'middle', session_id: 'middle' },
        {
          agent_id: 'parent',
          project_id: 'project',
          session_id: 'root',
        },
      ],
    },
  ];
  flushSync();
  const [notice] = notices.list;
  expect(notice.id).toBe('pending-approvals');
  expect(notice.onDismiss).toBeUndefined();
  notice.actions[0].onClick();
  expect(openSession).toHaveBeenCalledWith({
    agentId: 'parent@project',
    sessionId: 'root',
  });
  context.approvalRequests = [];
  flushSync();
  expect(notices.list).toEqual([]);
});
