// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { TOOLTIP_SHOW_DELAY_MS } from '../../lib/tooltip.js';

const listSessionsMock = vi.fn(async () => ({ sessions: [] }));
const renameSessionMock = vi.fn(async () => ({ title: 'Release planning' }));
const setSessionCompactionPolicyMock = vi.fn();
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
  setSessionCompactionPolicy: (...args) =>
    setSessionCompactionPolicyMock(...args),
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
    setSessionCompactionPolicyMock.mockReset();
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
    vi.clearAllTimers();
    vi.useRealTimers();
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
    expect(listSessionsMock.mock.calls.at(-1)[0]).toBe('alpha');
    expect(listSessionsMock.mock.calls.at(-1)[1]).toMatchObject({ limit: 35 });
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

  it('reloads once per burst of Session changes that name a listed Agent', async () => {
    const { createChatViewParentHarness } =
      await import('./chatViewParentHarness.svelte.js');
    const harness = createChatViewParentHarness();
    // A signal that predates the drawer is already reflected by its first load.
    harness.pushSessionInvalidation({ agent_id: 'alpha', session_id: 'old' });

    mountedComponent = mount(SessionListDrawer, {
      target: document.body,
      props: {
        agentId: 'alpha',
        currentSessionId: 'session-1',
        get invalidations() {
          return harness.sessionInvalidations;
        },
      },
    });
    flushSync();
    await waitForCondition(() => listSessionsMock.mock.calls.length === 1);

    vi.useFakeTimers();
    // Another Agent's Session and a read acknowledgement leave this list as is.
    harness.pushSessionInvalidation({ agent_id: 'beta', session_id: 'b1' });
    harness.pushSessionInvalidation({
      agent_id: 'alpha',
      session_id: 'session-1',
      read_run_id: 'run-1',
    });
    flushSync();
    await vi.advanceTimersByTimeAsync(500);
    expect(listSessionsMock).toHaveBeenCalledTimes(1);

    // A burst naming the listed Agent collapses into one reload.
    harness.pushSessionInvalidation({ agent_id: 'alpha', session_id: 'new' });
    flushSync();
    harness.pushSessionInvalidation({
      agent_id: 'alpha',
      session_id: 'session-1',
      run_id: 'run-2',
    });
    flushSync();
    await vi.advanceTimersByTimeAsync(500);
    expect(listSessionsMock).toHaveBeenCalledTimes(2);
    expect(listSessionsMock.mock.calls.at(-1)[0]).toBe('alpha');
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

  it('updates a mounted row from live running to unread activity without reloading', async () => {
    const { createChatViewParentHarness } =
      await import('./chatViewParentHarness.svelte.js');
    const harness = createChatViewParentHarness();
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'session-1',
          created_at: '2026-05-09T00:00:00+00:00',
          has_active_run: false,
          has_unread_completion: false,
        },
      ],
    });

    mountedComponent = mount(SessionListDrawer, {
      target: document.body,
      props: {
        agentId: 'alpha',
        currentSessionId: 'another-session',
        get liveActivity() {
          return harness.sessionListActivity;
        },
      },
    });
    flushSync();
    await waitForCondition(
      () => document.querySelector('.session-row') !== null,
    );
    expect(document.querySelector('.session-row__active-dot')).toBeNull();

    harness.setSessionListActivity([
      {
        agent_address: 'alpha',
        session_id: 'session-1',
        has_active_run: true,
        has_unread_completion: false,
      },
    ]);
    flushSync();
    await waitForCondition(
      () => document.querySelector('.session-row__active-dot') !== null,
    );
    expect(listSessionsMock).toHaveBeenCalledTimes(1);

    harness.setSessionListActivity([
      {
        agent_address: 'alpha',
        session_id: 'session-1',
        has_active_run: false,
        has_unread_completion: true,
        latest_completion_run_id: 'run-one',
        unread_run_id: 'run-one',
        unread_run_status: 'completed',
        unread_run_at: '2026-08-31T12:00:00+00:00',
      },
    ]);
    flushSync();
    await waitForCondition(
      () => document.querySelector('.session-row__unread-dot') !== null,
    );
    expect(document.querySelector('.session-row__active-dot')).toBeNull();
    expect(listSessionsMock).toHaveBeenCalledTimes(1);
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
    // An Enter/Escape that confirms or cancels an IME candidate stays with
    // the composition instead of committing or abandoning the rename.
    for (const key of ['Enter', 'Escape']) {
      input.dispatchEvent(
        new KeyboardEvent('keydown', { key, bubbles: true, isComposing: true }),
      );
    }
    flushSync();
    await Promise.resolve();
    expect(renameSessionMock).not.toHaveBeenCalled();
    expect(document.querySelector('.session-row__edit-input')).toBe(input);

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

  it.each([
    ['rename', 'Agent selection'],
    ['delete', 'Agent selection'],
    ['policy', 'Agent selection'],
    ['rename', 'All agents filter'],
    ['delete', 'All agents filter'],
  ])(
    'refreshes the current list after a pending %s and a change to %s',
    async (operation, transition) => {
      const { createChatViewParentHarness } =
        await import('./chatViewParentHarness.svelte.js');
      const harness = createChatViewParentHarness();
      const changeAgent = transition === 'Agent selection';
      const mutationAgent = changeAgent ? 'alpha' : 'beta';
      const displayedAgent = changeAgent ? 'beta' : 'alpha';
      const mutation = {
        rename: renameSessionMock,
        delete: deleteSessionMock,
        policy: setSessionCompactionPolicyMock,
      }[operation];
      let finishMutation;
      mutation.mockImplementationOnce(
        () => new Promise((resolve) => (finishMutation = resolve)),
      );
      listSessionsMock.mockImplementation(async (requested, query) => {
        const addresses = Array.isArray(requested) ? requested : [requested];
        // Mirror the server contract: a required Session must belong to one
        // of the requested Agents, including during post-mutation refreshes.
        if (!addresses.includes(query.requiredSession?.agentId)) {
          throw new Error('required Session is outside the requested Agents');
        }
        const requestNumber = listSessionsMock.mock.calls.length;
        return {
          sessions: addresses.map((address) => ({
            id: `session-${address}`,
            agent_address: address,
            title: `${address} Session ${requestNumber}`,
          })),
        };
      });
      const onSessionDeleted = vi.fn();
      const onCompactionPolicyChange = vi.fn();
      mountedComponent = mount(SessionListDrawer, {
        target: document.body,
        props: {
          get agentId() {
            return harness.selectedAgentId;
          },
          get currentSessionId() {
            return `session-${harness.selectedAgentId}`;
          },
          agents: [{ address: 'alpha' }, { address: 'beta' }],
          initialFilters: { allAgents: !changeAgent },
          onSessionDeleted,
          onCompactionPolicyChange,
        },
      });
      flushSync();
      await waitForCondition(
        () =>
          document.querySelectorAll('.session-row').length ===
          (changeAgent ? 1 : 2),
      );

      sessionRowButton(`${mutationAgent} Session`)
        .closest('.session-row')
        .querySelector('.session-row__menu-trigger')
        .click();
      flushSync();
      buttonByText(
        { rename: 'Rename', delete: 'Delete', policy: 'Compaction Policy' }[
          operation
        ],
      ).click();
      flushSync();
      if (operation === 'rename') {
        const input = document.querySelector('.session-row__edit-input');
        input.value = 'Updated title';
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.dispatchEvent(
          new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
        );
      } else if (operation === 'delete') {
        confirmDialog('Delete');
      } else {
        buttonByText('Save').click();
      }
      flushSync();
      await waitForCondition(() => mutation.mock.calls.length === 1);

      if (changeAgent) {
        harness.setSelectedAgentId(displayedAgent);
      } else {
        document.querySelector('[aria-label="All agents"]').click();
      }
      flushSync();
      await waitForCondition(() =>
        document
          .querySelector('.session-row__name')
          ?.textContent.includes(`${displayedAgent} Session 2`),
      );
      const effective = { enabled: false };
      finishMutation({ next_session_id: 'landing-session', effective });
      flushSync();

      // The final server snapshot must reach the new list without reverting
      // its Agent scope or reporting a mismatched required-Session error.
      await waitForCondition(() =>
        document
          .querySelector('.session-row__name')
          ?.textContent.includes(`${displayedAgent} Session 3`),
      );
      expect(document.querySelectorAll('.session-row')).toHaveLength(1);
      expect(document.querySelector('[role="alert"]')).toBeNull();
      expect(listSessionsMock.mock.calls.at(-1)[0]).toBe(displayedAgent);
      expect(mutation.mock.calls[0].slice(0, 2)).toEqual([
        mutationAgent,
        `session-${mutationAgent}`,
      ]);
      if (operation === 'delete') {
        expect(onSessionDeleted).toHaveBeenCalledWith({
          deletedSessionId: `session-${mutationAgent}`,
          nextSessionId: 'landing-session',
          agentAddress: mutationAgent,
        });
      } else if (operation === 'policy') {
        expect(onCompactionPolicyChange).toHaveBeenCalledWith(
          mutationAgent,
          `session-${mutationAgent}`,
          effective,
        );
      }
    },
  );

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

  it('hands a saved Session Compaction Policy to Chat', async () => {
    const effective = {
      enabled: false,
      trigger: { type: 'context_ratio', threshold: 0.8 },
      strategy: { type: 'summary_tail', tail_tokens: 15000 },
    };
    setSessionCompactionPolicyMock.mockReset();
    setSessionCompactionPolicyMock.mockResolvedValue({
      override: null,
      effective,
    });
    const onCompactionPolicyChange = vi.fn();
    mountedComponent = mount(SessionListDrawer, {
      target: document.body,
      props: {
        agentId: 'alpha',
        currentSessionId: 'session-1',
        onCompactionPolicyChange,
      },
    });
    flushSync();
    await waitForCondition(
      () => document.querySelector('.session-row') !== null,
    );

    document.querySelector('.session-row__menu-trigger').click();
    flushSync();
    buttonByText('Compaction Policy').click();
    flushSync();
    buttonByText('Save').click();

    await vi.waitFor(() =>
      expect(onCompactionPolicyChange).toHaveBeenCalledWith(
        'alpha',
        'session-1',
        effective,
      ),
    );
    expect(setSessionCompactionPolicyMock).toHaveBeenCalledWith(
      'alpha',
      'session-1',
      null,
    );
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
      agentAddress: 'alpha',
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
        initialFilters: { channels: true },
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

    vi.useFakeTimers();
    sessionButton.focus();
    await vi.advanceTimersByTimeAsync(TOOLTIP_SHOW_DELAY_MS);
    flushSync();

    const tooltipText = document.getElementById('app-tooltip')?.textContent;
    expect(tooltipText).toContain('Child session title');
    expect(tooltipText).toContain('Last active:');
    expect(tooltipText).toContain('Source channel: telegram-main');
    expect(tooltipText).toContain('Parent: orchestrator/parent-session');
  });

  it('shows important sessions by default and reveals labelled execution sessions through the filters', async () => {
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

    openFilterMenu();
    for (const label of ['Subagent runs', 'Memory reflections', 'Cron runs']) {
      filterSwitch(label).click();
      flushSync();
    }

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

  it('labels memory and skill reflection sessions with their own markers', async () => {
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'memory-review',
          created_at: '2026-05-09T00:00:00+00:00',
          run_kinds: ['memory_reflection'],
        },
        {
          id: 'skill-review',
          created_at: '2026-05-10T00:00:00+00:00',
          run_kinds: ['skill_reflection'],
        },
      ],
    });
    mountedComponent = mount(SessionListDrawer, {
      target: document.body,
      props: {
        agentId: 'alpha',
        currentSessionId: '',
      },
    });
    flushSync();

    openFilterMenu();
    filterSwitch('Memory reflections').click();
    flushSync();
    filterSwitch('Skill reflections').click();
    flushSync();

    await waitForCondition(
      () => document.querySelectorAll('.session-row').length === 2,
    );
    const markerLabels = Array.from(
      document.querySelectorAll('[data-session-marker]'),
    ).map((marker) => marker.getAttribute('aria-label'));
    expect(markerLabels).toContain('Memory reflection');
    expect(markerLabels).toContain('Skill reflection');
  });

  it('lists sessions for every roster agent in one bounded request', async () => {
    listSessionsMock.mockImplementation(async (requested) => {
      const addresses = Array.isArray(requested) ? requested : [requested];
      return {
        sessions: addresses.map((address) => ({
          id: `session-${address}`,
          title: `Session ${address}`,
          created_at: '2026-05-09T00:00:00+00:00',
          agent_address: address,
        })),
        total_count: addresses.length,
        next_cursor: null,
      };
    });
    const onSessionSelected = vi.fn();
    mountedComponent = mount(SessionListDrawer, {
      target: document.body,
      props: {
        agentId: 'alpha',
        currentSessionId: 'session-alpha',
        agents: [
          { address: 'alpha', name: 'Alpha' },
          { address: 'beta', name: 'Beta' },
        ],
        onSessionSelected,
      },
    });
    flushSync();
    await waitForCondition(
      () => document.querySelectorAll('.session-row').length === 1,
    );
    expect(listSessionsMock.mock.calls[0][0]).toBe('alpha');
    expect(document.body.textContent).not.toContain('Beta');

    const allAgents = document.querySelector('button[aria-label="All agents"]');
    expect(allAgents.getAttribute('aria-pressed')).toBe('false');
    allAgents.click();
    flushSync();

    await waitForCondition(() => listSessionsMock.mock.calls.length === 2);
    expect(listSessionsMock.mock.calls[1][0]).toEqual(['alpha', 'beta']);
    expect(allAgents.getAttribute('aria-pressed')).toBe('true');
    expect(document.querySelector('.session-drawer__filter-count')).toBeNull();
    expect(listSessionsMock.mock.calls[1][1]).toMatchObject({ limit: 35 });
    await waitForCondition(
      () => document.querySelectorAll('.session-row').length === 2,
    );

    // Merged rows carry their owning agent's name, and selecting one passes
    // that agent's address so ChatView can navigate across agents.
    expect(document.body.textContent).toContain('Alpha');
    expect(document.body.textContent).toContain('Beta');
    sessionRowButton('Session beta').click();
    flushSync();
    expect(onSessionSelected).toHaveBeenCalledWith(
      'session-beta',
      'beta',
      false,
    );
  });

  it('requests channels only when enabled and persists the filter choice', async () => {
    const onFiltersChange = vi.fn();
    listSessionsMock.mockImplementation(async (_agents, query) => ({
      sessions: [
        { id: 'ordinary', title: 'Ordinary session' },
        ...(query.includeChannels
          ? [
              {
                id: 'telegram',
                title: 'Telegram session',
                platform: 'telegram',
                platform_conv_id: '1',
              },
            ]
          : []),
      ],
    }));
    mountedComponent = mount(SessionListDrawer, {
      target: document.body,
      props: { agentId: 'alpha', onFiltersChange },
    });
    flushSync();
    await waitForCondition(
      () => document.querySelectorAll('.session-row').length === 1,
    );
    expect(listSessionsMock.mock.calls[0][1].includeChannels).toBe(false);
    openFilterMenu();
    expect(
      document.querySelector('[role="switch"][aria-label="All agents"]'),
    ).toBeNull();
    filterSwitch('Show channels').click();
    flushSync();
    await waitForCondition(
      () => document.querySelectorAll('.session-row').length === 2,
    );
    expect(listSessionsMock.mock.calls.at(-1)[1].includeChannels).toBe(true);
    expect(onFiltersChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ channels: true }),
    );
    expect(
      document
        .querySelector('.session-drawer__filter-count')
        .textContent.trim(),
    ).toBe('1');
    filterSwitch('Show channels').click();
    flushSync();
    await waitForCondition(
      () => document.querySelectorAll('.session-row').length === 1,
    );
  });

  it('loads 35 sessions initially and requests 20 more with the server cursor', async () => {
    const firstPage = Array.from({ length: 35 }, (_, index) => ({
      id: `session-${index}`,
      title: `Session ${index}`,
      created_at: `2026-05-01T00:${String(index).padStart(2, '0')}:00+00:00`,
      agent_address: 'alpha',
    }));
    const secondPage = Array.from({ length: 20 }, (_, index) => ({
      id: `session-${index + 35}`,
      title: `Session ${index + 35}`,
      created_at: '2026-04-01T00:00:00+00:00',
      agent_address: 'alpha',
    }));
    const cursor = {
      active_sort: 2460000,
      agent_id: 'alpha',
      session_id: 'session-34',
    };
    listSessionsMock
      .mockResolvedValueOnce({
        sessions: firstPage,
        total_count: 55,
        next_cursor: cursor,
      })
      .mockResolvedValueOnce({
        sessions: secondPage,
        total_count: 55,
        next_cursor: null,
      });

    mountedComponent = mount(SessionListDrawer, {
      target: document.body,
      props: {
        agentId: 'alpha',
        currentSessionId: 'session-0',
      },
    });
    flushSync();

    await waitForCondition(
      () => document.querySelectorAll('.session-row').length === 35,
    );
    expect(
      document.querySelector('.session-drawer__more-hint').textContent,
    ).toContain('20 more sessions');

    const list = document.querySelector('.session-drawer__list');
    Object.defineProperties(list, {
      scrollTop: { configurable: true, value: 900 },
      clientHeight: { configurable: true, value: 100 },
      scrollHeight: { configurable: true, value: 1000 },
    });
    list.dispatchEvent(new Event('scroll'));
    flushSync();

    await waitForCondition(
      () => document.querySelectorAll('.session-row').length === 55,
    );
    expect(listSessionsMock.mock.calls[1][0]).toBe('alpha');
    expect(listSessionsMock.mock.calls[1][1]).toMatchObject({
      limit: 20,
      cursor,
    });
    expect(document.querySelector('.session-drawer__more-hint')).toBeNull();
  });

  it('passes the row sub-agent flag so foreign sessions do not pose as sub-agent views', async () => {
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'child-session',
          title: 'Child session',
          is_subagent_session: true,
          created_at: '2026-05-09T00:00:00+00:00',
        },
      ],
    });
    const onSessionSelected = vi.fn();
    mountedComponent = mount(SessionListDrawer, {
      target: document.body,
      props: {
        agentId: 'alpha',
        currentSessionId: '',
        onSessionSelected,
      },
    });
    flushSync();

    openFilterMenu();
    filterSwitch('Subagent runs').click();
    flushSync();
    await waitForCondition(
      () => document.querySelectorAll('.session-row').length === 1,
    );

    sessionRowButton('Child session').click();
    flushSync();
    expect(onSessionSelected).toHaveBeenCalledWith(
      'child-session',
      'alpha',
      true,
    );
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

// Opens the header filter dropdown and waits for its portaled panel.
function openFilterMenu() {
  const trigger = document.querySelector('.session-drawer__filter-trigger');
  expect(trigger, 'filter trigger not rendered').toBeTruthy();
  trigger.click();
  flushSync();
  const menu = document.querySelector('.session-drawer__filter-menu');
  expect(menu, 'filter menu did not open').toBeTruthy();
  return menu;
}

// Returns the filter dropdown's switch toggle for one filter label.
function filterSwitch(label) {
  const toggle = [
    ...document.querySelectorAll(
      '.session-drawer__filter-menu [role="switch"]',
    ),
  ].find((candidate) => candidate.getAttribute('aria-label') === label);
  expect(toggle, `filter switch not found: ${label}`).toBeTruthy();
  return toggle;
}

// Returns the row-select button of the session row showing the given title.
function sessionRowButton(title) {
  const button = [...document.querySelectorAll('.session-row__select')].find(
    (candidate) => candidate.textContent.includes(title),
  );
  expect(button, `session row not found: ${title}`).toBeTruthy();
  return button;
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
