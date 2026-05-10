// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

const rpcMock = vi.fn();
const subscribeRunEventsMock = vi.fn(() => ({ close: vi.fn(), source: null }));
const subscribeServerEventsMock = vi.fn(() => ({
  close: vi.fn(),
  socket: null,
}));

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  rpc: (...args) => rpcMock(...args),
  subscribeRunEvents: (...args) => subscribeRunEventsMock(...args),
  subscribeServerEvents: (...args) => subscribeServerEventsMock(...args),
}));

const { default: App } = await import('../../App.svelte');

describe('App', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
    subscribeRunEventsMock.mockClear();
    subscribeServerEventsMock.mockClear();
    rpcMock.mockImplementation(createEmptyChatRpcMock());
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }

    document.body.innerHTML = '';
    rpcMock.mockReset();
  });

  it('maps app_error WebSocket events to error toasts', () => {
    mountedComponent = mount(App, { target: document.body });
    flushSync();

    expect(subscribeServerEventsMock).toHaveBeenCalledTimes(1);
    const [handlers] = subscribeServerEventsMock.mock.calls[0];

    handlers.onEvent({
      type: 'app_error',
      sequence: 1,
      payload: { message: 'Provider credentials are missing.' },
    });
    flushSync();

    const toast = document.querySelector('.toast.error');
    expect(toast).toBeTruthy();
    expect(toast.textContent).toContain('Error');
    expect(toast.textContent).toContain('Provider credentials are missing.');
  });
});

function createEmptyChatRpcMock() {
  return async (method) => {
    if (method === 'agent.list') {
      return { agents: [] };
    }

    if (method === 'skill.list') {
      return { skills: [], invalid_skills: [] };
    }

    throw new Error(`Unexpected RPC method: ${method}`);
  };
}
