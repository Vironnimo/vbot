// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { init, t } from '../../../lib/i18n.js';
import {
  assistantRun,
  bashChild,
  readChild,
  subAgentChild,
} from './ChatAssistantRun.support.js';

const resolve = vi.fn();
vi.mock(
  'svelte',
  async () => import('../../../../node_modules/svelte/src/index-client.js'),
);
vi.mock('$lib/api.js', async (importOriginal) => ({
  ...(await importOriginal()),
  resolvePermissionApproval: (...args) => resolve(...args),
}));
const { default: ChatApprovalCard } =
  await import('../ChatApprovalCard.svelte');
const { default: ChatAssistantRun } =
  await import('../ChatAssistantRun.svelte');
init('en');

let component;
afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  document.body.innerHTML = '';
  vi.clearAllMocks();
});

function button(label, root = document) {
  return [...root.querySelectorAll('button')].find(
    (element) => element.textContent.trim() === label,
  );
}

function approval(fields = {}) {
  return {
    id: 'approval-one',
    run_id: 'run-parent',
    tool_call_id: 'call-bash',
    agent_id: 'alpha',
    project_id: null,
    session_id: 'session-1',
    tool_name: 'bash',
    execution: { computer_id: 'computer' },
    reason: 'Permission Rules',
    cwd: '/work',
    display_redacted: true,
    requirements: [
      {
        kind: 'command',
        access: 'command',
        target: 'git push origin main',
      },
      {
        kind: 'path',
        access: 'write',
        target: '/outside/notes/today.md',
        shown: '/outside/Notes/Today.md',
        filtered: false,
      },
    ],
    arguments: { command: 'git push origin main' },
    session_grant: {
      rules: ['Shell(git push *)', 'Edit(/outside/Notes)'],
      exact: ['Shell(git push origin main)', 'Edit(/outside/Notes/Today.md)'],
    },
    ...fields,
  };
}

describe('ChatApprovalCard', () => {
  it('names the access and allows the call once', async () => {
    resolve.mockResolvedValue({ resolved: true });
    component = mount(ChatApprovalCard, {
      target: document.body,
      props: { request: approval() },
    });
    flushSync();
    const card = document.querySelector('.approval-card');
    for (const value of [
      'git push origin main',
      '/outside/Notes/Today.md',
      'computer',
      '/work',
      t('approvals.redacted'),
    ])
      expect(card.textContent).toContain(value);
    button(t('approvals.allowOnce')).click();
    await vi.waitFor(() =>
      expect(resolve).toHaveBeenCalledWith('approval-one', 'allow', {}),
    );
    flushSync();
    expect(button(t('approvals.reject')).disabled).toBe(true);
  });

  it('remembers the suggested, the exact or the edited rules', async () => {
    resolve.mockResolvedValue({ resolved: true });
    const request = approval();
    component = mount(ChatApprovalCard, {
      target: document.body,
      props: { request },
    });
    flushSync();
    button(t('approvals.allowSession')).click();
    await vi.waitFor(() =>
      expect(resolve).toHaveBeenLastCalledWith('approval-one', 'allow', {
        remember: request.session_grant.rules,
      }),
    );
    await unmount(component);

    for (const [item, expected] of [
      [t('approvals.session.exact'), request.session_grant.exact],
      [t('approvals.session.edit'), ['Shell(git push *)', 'Web(example.org)']],
    ]) {
      document.body.innerHTML = '';
      component = mount(ChatApprovalCard, {
        target: document.body,
        props: { request },
      });
      flushSync();
      document.querySelector('[data-approval="session-menu"]').click();
      flushSync();
      [...document.querySelectorAll('[role="menuitem"]')]
        .find((element) => element.textContent.includes(item))
        .click();
      flushSync();
      if (item === t('approvals.session.edit')) {
        const field = document.querySelector('.approval-card textarea');
        field.value = 'Shell(git push *)\n\n  Web(example.org)  ';
        field.dispatchEvent(new Event('input', { bubbles: true }));
        flushSync();
        button(t('approvals.session.allowEdited')).click();
      }
      await vi.waitFor(() =>
        expect(resolve).toHaveBeenLastCalledWith('approval-one', 'allow', {
          remember: expected,
        }),
      );
      await unmount(component);
    }
    component = null;
  });

  it('offers only Allow once when no Session Grant could help', () => {
    component = mount(ChatApprovalCard, {
      target: document.body,
      props: { request: approval({ session_grant: null }) },
    });
    flushSync();
    expect(button(t('approvals.allowOnce'))).toBeDefined();
    expect(button(t('approvals.allowSession'))).toBeUndefined();
  });
});

describe('approval cards in the Run', () => {
  function mountRun(items, requests, props = {}) {
    component = mount(ChatAssistantRun, {
      target: document.body,
      props: { item: assistantRun({ items }), ...props },
      context: new Map([['permissionApprovals', { requests }]]),
    });
    flushSync();
  }

  it('sits at the waiting Tool row, also in a folded Working group', () => {
    const requests = [approval()];
    mountRun([bashChild()], requests);
    const card = document.querySelector('.approval-card');
    expect(card.previousElementSibling.matches('.tool-event')).toBe(true);
    expect(card.dataset.approvalId).toBe('approval-one');
    // Another Run's call with the same id waits elsewhere.
    expect(document.querySelectorAll('[data-approval-id]').length).toBe(1);
    unmount(component);
    document.body.innerHTML = '';

    mountRun([readChild(), bashChild()], requests, {
      chatWorkingMode: 'compact',
    });
    const group = document.querySelector('.working-block');
    expect(group.open).toBe(false);
    expect(group.nextElementSibling.dataset.approvalId).toBe('approval-one');
  });

  it('shows a Sub-Agent Session’s request at its Sub-Agent row', () => {
    const onNavigateToSubAgent = vi.fn();
    mountRun(
      [subAgentChild()],
      [
        approval({
          id: 'approval-child',
          run_id: 'run-child',
          agent_id: 'worker',
          session_id: 'session-child',
        }),
        approval({ id: 'approval-other', run_id: 'run-other' }),
      ],
      { onNavigateToSubAgent },
    );
    const cards = document.querySelectorAll('[data-approval-id]');
    expect([...cards].map((card) => card.dataset.approvalId)).toEqual([
      'approval-child',
    ]);
    expect(cards[0].textContent).toContain(
      t('approvals.subAgentWaiting', { agent: 'worker' }),
    );
    button(t('approvals.openSession'), cards[0]).click();
    expect(onNavigateToSubAgent).toHaveBeenCalledWith({
      agentId: 'worker',
      sessionId: 'session-child',
    });
  });
});
