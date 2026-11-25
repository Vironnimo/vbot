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
      context: new Map([['permissionApprovals', { resolve }]]),
      target: document.body,
      props: {
        request: approval({
          shell_credential_sources: {
            API_TOKEN: 'API_TOKEN',
            LOGIN: '{{secret:DB.username}}',
          },
        }),
      },
    });
    flushSync();
    const card = document.querySelector('.approval-card');
    const needs = card.querySelector('.approval-card__needs');
    expect(needs.textContent).toContain('API_TOKEN');
    expect(needs.textContent).toContain('LOGIN ← {{secret:DB.username}}');
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

  it.each([
    ['vbot', 'vbot agent update other --permissions full-access'],
    [
      'bash',
      'vbot config set defaults.agent.permission_rules --stdin\n{"read":"anywhere"}',
    ],
  ])(
    'shows the reviewed %s CLI invocation before the user decides',
    (tool, command) => {
      component = mount(ChatApprovalCard, {
        target: document.body,
        props: {
          request: approval({
            tool_name: tool,
            requirements: [
              { kind: 'review', access: 'review', shown: command },
            ],
            session_grant: null,
          }),
        },
      });
      flushSync();
      expect(document.querySelector('.approval-card__target').textContent).toBe(
        command,
      );
      expect(document.body.textContent).toContain(
        t('approvals.need.agentAccess'),
      );
      expect(button(t('approvals.allowOnce'))).toBeDefined();
      expect(button(t('approvals.allowSession'))).toBeUndefined();
    },
  );

  function mountProfile(profile, argv, stdin) {
    component = mount(ChatApprovalCard, {
      target: document.body,
      props: {
        request: approval({
          tool_name: 'vbot',
          arguments: { argv, stdin },
          requirements: [
            {
              kind: 'review',
              shown: ['vbot', ...argv].join(' '),
              permission_profile: profile,
            },
          ],
          session_grant: null,
        }),
      },
    });
    flushSync();
    return document.querySelector('.approval-card__needs');
  }

  const rules = {
    read: 'anywhere',
    write: 'scratch',
    commands: 'any',
    web: 'listed',
    allow: ['Shell(git status)', 'Shell(git diff *)'],
    deny: [],
  };

  it('shows the complete reviewed rules for a partial profile save', () => {
    const stdin = JSON.stringify({ write: 'scratch', allow: rules.allow });
    const argv = ['permission-profiles', 'save', 'analyst', '--stdin'];
    const base = { ...rules, write: 'anywhere' };
    const review = mountProfile(
      { action: 'save', name: 'analyst', rules, base },
      argv,
      stdin,
    );
    expect(review.textContent).toContain('Update permission profile “analyst”');
    expect(review.textContent).not.toContain(stdin);
    const definition = review.querySelector('dl');
    const values = [...definition.querySelectorAll('dt')].map((dt) => [
      dt.textContent.trim(),
      dt.nextElementSibling.textContent.trim(),
    ]);
    expect(values).toEqual([
      [t('permissions.part.read'), t('permissions.base.read.anywhere')],
      [t('permissions.part.write'), t('permissions.base.write.scratch')],
      [t('permissions.part.commands'), t('permissions.base.commands.any')],
      [t('permissions.part.web'), t('permissions.base.web.listed')],
      [t('permissions.allowTitle'), expect.stringContaining(rules.allow[0])],
      [t('permissions.denyTitle'), t('permissions.rulesNone')],
    ]);
    expect(
      [...definition.querySelectorAll('li')].map((li) => li.textContent),
    ).toEqual(rules.allow);
    const previous = review.querySelector('details');
    expect(previous.open).toBe(false);
    expect(previous.querySelectorAll('dd')[1].textContent.trim()).toBe(
      t('permissions.base.write.anywhere'),
    );
    const details = document.querySelector('.approval-card__details');
    expect(details.open).toBe(false);
    expect(JSON.parse(details.querySelector('pre').textContent)).toEqual({
      argv,
      stdin,
    });
  });

  it('shows the definition being removed when a profile is deleted', () => {
    const review = mountProfile(
      { action: 'delete', name: 'analyst', rules: null, base: rules },
      ['permission-profiles', 'delete', 'analyst'],
    );
    expect(review.textContent).toContain('Delete permission profile “analyst”');
    expect(review.textContent).toContain(rules.allow[0]);
  });

  it('keeps additional access flags visible when assigning a profile', () => {
    const argv =
      'agent update beta --permission-profile analyst --tool-access-mode all'.split(
        ' ',
      );
    const review = mountProfile(
      { action: 'assign', name: 'analyst', target: 'beta', rules },
      argv,
    );
    expect(review.textContent).toContain(
      'Assign permission profile “analyst” to beta',
    );
    expect(review.textContent).toContain(rules.allow[0]);
    expect(review.textContent).toContain(['vbot', ...argv].join(' '));
  });

  it.each(['inherit', 'inline'])(
    'names unrestricted access when %s resolves to Full access',
    (action) => {
      const review = mountProfile({ action, target: 'beta', rules: null }, []);
      expect(review.textContent).toContain('beta');
      expect(review.textContent).toContain(t('permissions.preset.full_access'));
    },
  );

  it('remembers the suggested, the exact or the edited rules', async () => {
    resolve.mockResolvedValue({ resolved: true });
    const request = approval();
    component = mount(ChatApprovalCard, {
      context: new Map([['permissionApprovals', { resolve }]]),
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
        context: new Map([['permissionApprovals', { resolve }]]),
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

  it.each([false, true])(
    'grants the displayed folder or its selected ancestor (%s)',
    async (chooseParent) => {
      const folders = [
        '/python/mcp/client/auth',
        '/python/mcp/client',
        '/python/mcp',
      ];
      component = mount(ChatApprovalCard, {
        context: new Map([['permissionApprovals', { resolve }]]),
        target: document.body,
        props: {
          subAgent: 'coder',
          request: approval({
            session_grant: { rules: [], exact: null, folders },
          }),
        },
      });
      flushSync();
      expect(
        document.querySelector('.approval-card__scope').textContent,
      ).toContain(folders[0]);
      if (chooseParent) {
        document.querySelector('[data-approval="session-menu"]').click();
        flushSync();
        [...document.querySelectorAll('[role="menuitem"]')]
          .find((item) => item.textContent.trim() === folders[1])
          .click();
      } else {
        button(t('approvals.session.readFolder', { folder: 'auth' })).click();
      }
      expect(resolve).toHaveBeenCalledWith('approval-one', 'allow', {
        read_folder: folders[chooseParent ? 1 : 0],
      });
    },
  );

  it('offers only Allow once when no Session Grant could help', () => {
    component = mount(ChatApprovalCard, {
      context: new Map([['permissionApprovals', { resolve }]]),
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
      context: new Map([['permissionApprovals', { requests, resolve }]]),
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
