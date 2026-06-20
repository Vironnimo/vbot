// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

const listSessionsMock = vi.fn(async () => ({ sessions: [] }));

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  listSessions: (...args) => listSessionsMock(...args),
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
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
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
        agentCurrentSessionId: 'session-1',
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
        agentCurrentSessionId: 'session-1',
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
