// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { TOOLTIP_SHOW_DELAY_MS } from '../../lib/tooltip.js';

const listSessionsMock = vi.fn(async () => ({ sessions: [] }));
const renameSessionMock = vi.fn(async () => ({ title: 'Release planning' }));
const deleteSessionMock = vi.fn(async () => ({
  agent_id: 'alpha',
  session_id: 'session-1',
  next_session_id: 'session-2',
}));

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  listSessions: (...args) => listSessionsMock(...args),
  renameSession: (...args) => renameSessionMock(...args),
  deleteSession: (...args) => deleteSessionMock(...args),
}));

const { default: SessionListDrawer } =
  await import('../SessionListDrawer.svelte');

describe('SessionListDrawer', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    listSessionsMock.mockReset();
    listSessionsMock.mockResolvedValue({
      sessions: [{ id: 'session-1', created_at: '2026-05-09T00:00:00+00:00' }],
    });
    renameSessionMock.mockReset();
    renameSessionMock.mockResolvedValue({ title: 'Release planning' });
    deleteSessionMock.mockReset();
    deleteSessionMock.mockResolvedValue({
      agent_id: 'alpha',
      session_id: 'session-1',
      next_session_id: 'session-2',
    });
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    vi.unstubAllGlobals();
    document.body.innerHTML = '';
  });

  it('reloads the session list when the reload token bumps', async () => {
    const { createChatViewParentHarness } =
      await import('./chatViewParentHarness.svelte.js');
    // The harness exposes a reactive counter (`sessionsRefreshToken`) that maps
    // 1:1 to the drawer's `reloadToken` prop.
    const harness = createChatViewParentHarness();

    mountedComponent = mount(SessionListDrawer, {
      target: document.body,
      props: {
        agentId: 'alpha',
        currentSessionId: 'session-1',
        get reloadToken() {
          return harness.sessionsRefreshToken;
        },
      },
    });
    flushSync();

    // The agent-change effect loads the list once on mount.
    await waitForCondition(() => listSessionsMock.mock.calls.length === 1);
    const callsBefore = listSessionsMock.mock.calls.length;

    // A sessions resource_changed (forwarded as a token bump) reloads the list
    // so a new/switched session shows up without pressing Refresh.
    harness.bumpSessionsRefreshToken();
    flushSync();

    await waitForCondition(
      () => listSessionsMock.mock.calls.length === callsBefore + 1,
    );
    expect(listSessionsMock).toHaveBeenLastCalledWith('alpha');
  });

  it('does not reload on mount before the token ever changes', async () => {
    const { createChatViewParentHarness } =
      await import('./chatViewParentHarness.svelte.js');
    const harness = createChatViewParentHarness();

    mountedComponent = mount(SessionListDrawer, {
      target: document.body,
      props: {
        agentId: 'alpha',
        currentSessionId: 'session-1',
        get reloadToken() {
          return harness.sessionsRefreshToken;
        },
      },
    });
    flushSync();

    await waitForCondition(() => listSessionsMock.mock.calls.length === 1);
    // The initial token value must not trigger a second load on its own.
    flushSync();
    expect(listSessionsMock.mock.calls.length).toBe(1);
  });

  it('hides the permanent refresh action and offers Retry only after a load failure', async () => {
    listSessionsMock
      .mockRejectedValueOnce(new Error('session list unavailable'))
      .mockResolvedValueOnce({
        sessions: [
          { id: 'session-1', created_at: '2026-05-09T00:00:00+00:00' },
        ],
      });

    mountedComponent = mount(SessionListDrawer, {
      target: document.body,
      props: {
        agentId: 'alpha',
        currentSessionId: 'session-1',
      },
    });
    flushSync();

    await waitForCondition(() => buttonByText('Retry') !== null);
    expect(buttonByText('Refresh')).toBeNull();

    buttonByText('Retry').click();
    flushSync();

    await waitForCondition(
      () => document.querySelector('.session-row') !== null,
    );
    expect(listSessionsMock).toHaveBeenCalledTimes(2);
    expect(buttonByText('Retry')).toBeNull();
    expect(buttonByText('Refresh')).toBeNull();
  });

  it('identifies an unread Session unless it is already displayed', async () => {
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'session-1',
          created_at: '2026-05-09T00:00:00+00:00',
          has_unread_completion: true,
          unread_run_id: 'run-one',
          unread_run_status: 'completed',
          unread_run_at: '2026-07-20T10:00:00+00:00',
        },
        {
          id: 'session-2',
          created_at: '2026-05-10T00:00:00+00:00',
          has_unread_completion: true,
          unread_run_id: 'run-two',
          unread_run_status: 'completed',
          unread_run_at: '2026-07-20T10:05:00+00:00',
        },
      ],
    });

    mountedComponent = mount(SessionListDrawer, {
      target: document.body,
      props: {
        agentId: 'alpha',
        currentSessionId: 'session-1',
      },
    });
    flushSync();

    await waitForCondition(
      () => document.querySelector('.session-row__unread-dot') !== null,
    );

    const unreadMarker = document.querySelector('.session-row__unread');
    expect(unreadMarker?.textContent.trim()).toBe('');
    expect(unreadMarker?.getAttribute('aria-label')).toBe('Unread');
    expect(
      document
        .querySelector('.session-row__select--active')
        ?.querySelector('.session-row__unread'),
    ).toBeNull();
  });

  it('renders the centered row-action affordance as a vertical ellipsis', async () => {
    mountedComponent = mount(SessionListDrawer, {
      target: document.body,
      props: {
        agentId: 'alpha',
        currentSessionId: 'session-1',
      },
    });
    flushSync();

    await waitForCondition(
      () => document.querySelector('.session-row__menu-trigger') !== null,
    );

    const dots = [
      ...document.querySelectorAll('.session-row__menu-trigger circle'),
    ];
    expect(dots.map((dot) => dot.getAttribute('cx'))).toEqual(['8', '8', '8']);
    expect(dots.map((dot) => dot.getAttribute('cy'))).toEqual(['3', '8', '13']);
  });

  it('renames a session through the row menu and reloads the list', async () => {
    mountedComponent = mount(SessionListDrawer, {
      target: document.body,
      props: {
        agentId: 'alpha',
        currentSessionId: 'session-1',
      },
    });
    flushSync();
    await waitForCondition(
      () => document.querySelector('.session-row') !== null,
    );
    const loadsBefore = listSessionsMock.mock.calls.length;

    // Open the "…" menu, then choose Rename to enter inline edit.
    document.querySelector('.session-row__menu-trigger').click();
    flushSync();
    document.querySelector('.session-row__menu-item').click();
    flushSync();

    const input = document.querySelector('.session-row__edit-input');
    expect(input).not.toBeNull();
    input.value = 'Release planning';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
    );
    flushSync();

    await waitForCondition(() => renameSessionMock.mock.calls.length === 1);
    expect(renameSessionMock).toHaveBeenCalledWith(
      'alpha',
      'session-1',
      'Release planning',
    );
    // A successful rename re-fetches so the row shows the server-stored title.
    await waitForCondition(
      () => listSessionsMock.mock.calls.length === loadsBefore + 1,
    );
  });

  it('portals the complete row menu outside the clipped session drawer', async () => {
    mountedComponent = mount(SessionListDrawer, {
      target: document.body,
      props: {
        agentId: 'alpha',
        currentSessionId: 'session-1',
      },
    });
    flushSync();
    await waitForCondition(
      () => document.querySelector('.session-row') !== null,
    );

    document.querySelector('.session-row__menu-trigger').click();
    flushSync();
    await waitForCondition(
      () =>
        document.querySelector('.session-row__menu')?.style.visibility !==
        'hidden',
    );

    const drawer = document.querySelector('.session-drawer');
    const menu = document.querySelector('.session-row__menu');
    const labels = Array.from(
      menu.querySelectorAll('.session-row__menu-item'),
    ).map((item) => item.textContent.trim());

    expect(menu.parentElement).toBe(document.body);
    expect(drawer.contains(menu)).toBe(false);
    expect(menu.dataset.positioning).toBe('fixed');
    expect(labels).toEqual(['Rename', 'Compaction Policy', 'Delete']);
  });

  it('cancels inline rename on Escape without calling the API', async () => {
    mountedComponent = mount(SessionListDrawer, {
      target: document.body,
      props: {
        agentId: 'alpha',
        currentSessionId: 'session-1',
      },
    });
    flushSync();
    await waitForCondition(
      () => document.querySelector('.session-row') !== null,
    );

    document.querySelector('.session-row__menu-trigger').click();
    flushSync();
    document.querySelector('.session-row__menu-item').click();
    flushSync();

    const input = document.querySelector('.session-row__edit-input');
    expect(input).not.toBeNull();
    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
    );
    flushSync();

    expect(document.querySelector('.session-row__edit-input')).toBeNull();
    expect(renameSessionMock).not.toHaveBeenCalled();
  });

  it('deletes a session through the row menu after confirming the dialog', async () => {
    const onSessionDeleted = vi.fn();
    mountedComponent = mount(SessionListDrawer, {
      target: document.body,
      props: {
        agentId: 'alpha',
        currentSessionId: 'session-1',
        onSessionDeleted,
      },
    });
    flushSync();
    await waitForCondition(
      () => document.querySelector('.session-row') !== null,
    );
    const loadsBefore = listSessionsMock.mock.calls.length;

    document.querySelector('.session-row__menu-trigger').click();
    flushSync();
    document.querySelector('.session-row__menu-item--danger').click();
    flushSync();

    // The row action opens the shared ConfirmDialog; nothing is deleted until
    // the dialog is confirmed.
    expect(deleteSessionMock).not.toHaveBeenCalled();
    confirmDialog('Delete');
    flushSync();

    // Wait on the downstream callback so the delete promise has resolved (the
    // mock's call count flips the moment it is invoked, before onSessionDeleted).
    await waitForCondition(() => onSessionDeleted.mock.calls.length === 1);
    expect(deleteSessionMock).toHaveBeenCalledWith('alpha', 'session-1');
    expect(onSessionDeleted).toHaveBeenCalledWith({
      deletedSessionId: 'session-1',
      nextSessionId: 'session-2',
    });
    // A successful delete re-fetches so the removed row disappears.
    await waitForCondition(
      () => listSessionsMock.mock.calls.length === loadsBefore + 1,
    );
  });

  it('does not delete when the dialog is cancelled', async () => {
    const onSessionDeleted = vi.fn();
    mountedComponent = mount(SessionListDrawer, {
      target: document.body,
      props: {
        agentId: 'alpha',
        currentSessionId: 'session-1',
        onSessionDeleted,
      },
    });
    flushSync();
    await waitForCondition(
      () => document.querySelector('.session-row') !== null,
    );

    document.querySelector('.session-row__menu-trigger').click();
    flushSync();
    document.querySelector('.session-row__menu-item--danger').click();
    flushSync();

    confirmDialog('Cancel');
    flushSync();

    expect(deleteSessionMock).not.toHaveBeenCalled();
    expect(onSessionDeleted).not.toHaveBeenCalled();
    expect(document.querySelector('.modal-footer')).toBeNull();
  });

  it('surfaces a delete failure as an inline error', async () => {
    deleteSessionMock.mockRejectedValueOnce(
      new Error('cannot delete session with an active or queued run'),
    );
    mountedComponent = mount(SessionListDrawer, {
      target: document.body,
      props: {
        agentId: 'alpha',
        currentSessionId: 'session-1',
      },
    });
    flushSync();
    await waitForCondition(
      () => document.querySelector('.session-row') !== null,
    );

    document.querySelector('.session-row__menu-trigger').click();
    flushSync();
    document.querySelector('.session-row__menu-item--danger').click();
    flushSync();

    confirmDialog('Delete');
    flushSync();

    await waitForCondition(
      () => document.querySelector('.session-drawer__state--error') !== null,
    );
    expect(
      document.querySelector('.session-drawer__state--error').textContent,
    ).toContain('active or queued run');
  });

  it('renders the Fork badge only for forked sessions', async () => {
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'plain-session',
          created_at: '2026-05-09T00:00:00+00:00',
        },
        {
          id: 'fork-session',
          created_at: '2026-05-10T00:00:00+00:00',
          fork_source: {
            agent_id: 'alpha',
            session_id: 'plain-session',
            message_count: 4,
          },
        },
      ],
    });
    mountedComponent = mount(SessionListDrawer, {
      target: document.body,
      props: {
        agentId: 'alpha',
        currentSessionId: 'fork-session',
      },
    });
    flushSync();
    await waitForCondition(
      () => document.querySelectorAll('.session-row').length === 2,
    );

    const forkMarkers = document.querySelectorAll(
      '[data-session-marker="fork"]',
    );
    expect(forkMarkers.length).toBe(1);
    expect(forkMarkers[0].textContent.trim()).toBe('');
    expect(forkMarkers[0].getAttribute('aria-label')).toBe('Fork');
    expect(forkMarkers[0].querySelector('svg')).toBeTruthy();
  });

  it('renders Telegram, Discord, and fallback Channels as labelled icon markers', async () => {
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'telegram-session',
          created_at: '2026-05-09T00:00:00+00:00',
          platform: 'telegram',
          platform_conv_id: 'telegram-chat',
        },
        {
          id: 'discord-session',
          created_at: '2026-05-10T00:00:00+00:00',
          platform: 'discord',
          platform_conv_id: 'discord-chat',
        },
        {
          id: 'matrix-session',
          created_at: '2026-05-11T00:00:00+00:00',
          platform: 'matrix',
          platform_conv_id: 'matrix-room',
        },
      ],
    });
    mountedComponent = mount(SessionListDrawer, {
      target: document.body,
      props: {
        agentId: 'alpha',
        currentSessionId: 'telegram-session',
      },
    });
    flushSync();
    await waitForCondition(
      () => document.querySelectorAll('[data-session-marker]').length === 3,
    );

    const markers = Array.from(
      document.querySelectorAll('[data-session-marker]'),
    );
    expect(markers.map((marker) => marker.getAttribute('aria-label'))).toEqual([
      'Matrix',
      'Discord',
      'Telegram',
    ]);
    expect(markers.every((marker) => marker.querySelector('svg'))).toBe(true);
    expect(markers.every((marker) => marker.textContent.trim() === '')).toBe(
      true,
    );
  });

  it('moves secondary session metadata into the row tooltip', async () => {
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'child-session-with-a-long-identifier',
          title: 'Child session title',
          source_channel_id: 'telegram-main',
          created_at: '2026-05-09T00:00:00+00:00',
          last_active_at: '2026-05-09T01:00:00+00:00',
          is_subagent_session: true,
          subagent_parent: {
            agent_id: 'orchestrator',
            session_id: 'parent-session',
          },
        },
      ],
    });
    mountedComponent = mount(SessionListDrawer, {
      target: document.body,
      props: {
        agentId: 'alpha',
        currentSessionId: 'child-session-with-a-long-identifier',
      },
    });
    flushSync();

    await waitForCondition(
      () => document.querySelector('.session-row__select') !== null,
    );
    const sessionButton = document.querySelector('.session-row__select');
    expect(sessionButton.textContent).toContain('Child session title');
    expect(sessionButton.textContent).not.toContain('Last active');
    expect(sessionButton.textContent).not.toContain('Source channel');
    expect(sessionButton.textContent).not.toContain('Parent');

    sessionButton.focus();
    await new Promise((resolve) =>
      setTimeout(resolve, TOOLTIP_SHOW_DELAY_MS + 50),
    );

    const tooltipText = document.getElementById('app-tooltip')?.textContent;
    expect(tooltipText).toContain('Child session title');
    expect(tooltipText).toContain('Last active:');
    expect(tooltipText).toContain('Source channel: telegram-main');
    expect(tooltipText).toContain('Parent: orchestrator/parent-session');
  });

  it('shows important sessions by default and reveals labelled execution sessions', async () => {
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'user-session',
          created_at: '2026-05-09T00:00:00+00:00',
          run_kinds: ['user'],
        },
        {
          id: 'cron-session',
          created_at: '2026-05-10T00:00:00+00:00',
          run_kinds: ['cron'],
        },
        {
          id: 'reflection-session',
          created_at: '2026-05-11T00:00:00+00:00',
          run_kinds: ['reflection'],
        },
        {
          id: 'subagent-session',
          created_at: '2026-05-12T00:00:00+00:00',
          is_subagent_session: true,
          subagent_parent: {
            agent_id: 'alpha',
            session_id: 'user-session',
          },
        },
      ],
    });
    mountedComponent = mount(SessionListDrawer, {
      target: document.body,
      props: {
        agentId: 'alpha',
        currentSessionId: 'user-session',
      },
    });
    flushSync();

    await waitForCondition(
      () => document.querySelectorAll('.session-row').length === 1,
    );
    expect(document.body.textContent).not.toContain('cron-session');
    expect(document.body.textContent).not.toContain('reflection-session');
    expect(document.body.textContent).not.toContain('subagent-session');

    document.querySelector('[role="switch"]').click();
    flushSync();

    await waitForCondition(
      () => document.querySelectorAll('.session-row').length === 4,
    );
    const markerLabels = Array.from(
      document.querySelectorAll('[data-session-marker]'),
    ).map((marker) => marker.getAttribute('aria-label'));
    expect(markerLabels).toContain('Cron');
    expect(markerLabels).toContain('Reflection');
    expect(markerLabels).toContain('Subagent');
    expect(
      Array.from(document.querySelectorAll('[data-session-marker]')).every(
        (marker) =>
          marker.textContent.trim() === '' && marker.querySelector('svg'),
      ),
    ).toBe(true);
  });
});

async function waitForCondition(check, attempts = 50) {
  for (let index = 0; index < attempts; index += 1) {
    if (check()) {
      return;
    }
    await Promise.resolve();
    flushSync();
  }
  throw new Error('Condition was not met in time');
}

function buttonByText(text) {
  return (
    [...document.body.querySelectorAll('button')].find(
      (button) => button.textContent.trim() === text,
    ) ?? null
  );
}

// Clicks a button in the open ConfirmDialog by its label (Delete / Cancel).
function confirmDialog(label) {
  const footer = document.querySelector('.modal-footer');
  expect(footer, 'confirm dialog not open').toBeTruthy();
  const button = Array.from(footer.querySelectorAll('button')).find(
    (item) => item.textContent.trim() === label,
  );
  expect(button, `confirm button not found: ${label}`).toBeTruthy();
  button.click();
}
