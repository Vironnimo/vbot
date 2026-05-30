// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

const rpcMock = vi.fn();
const listQueueMock = vi.fn(() => Promise.resolve({ items: [] }));
const listLogsMock = vi.fn();
const readLogFileMock = vi.fn();
const subscribeLogEventsMock = vi.fn(() => ({
  close: vi.fn(),
  socket: null,
}));
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
  listQueue: (...args) => listQueueMock(...args),
  listLogs: (...args) => listLogsMock(...args),
  readLogFile: (...args) => readLogFileMock(...args),
  subscribeLogEvents: (...args) => subscribeLogEventsMock(...args),
  subscribeRunEvents: (...args) => subscribeRunEventsMock(...args),
  subscribeServerEvents: (...args) => subscribeServerEventsMock(...args),
}));

const { default: App } = await import('../../App.svelte');

describe('App', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    localStorage.clear();
    init('en');
    mountedComponent = null;
    listLogsMock.mockReset();
    listQueueMock.mockReset();
    listQueueMock.mockResolvedValue({ items: [] });
    readLogFileMock.mockReset();
    subscribeLogEventsMock.mockClear();
    subscribeRunEventsMock.mockClear();
    subscribeServerEventsMock.mockClear();
    rpcMock.mockImplementation(createEmptyChatRpcMock());
    listLogsMock.mockResolvedValue({
      files: ['2026-05-11.log'],
      default_file: '2026-05-11.log',
    });
    readLogFileMock.mockResolvedValue({
      file: '2026-05-11.log',
      entries: [],
      cursor: 'app-log-cursor',
    });
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }

    document.body.innerHTML = '';
    localStorage.clear();
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

  it('renders Logs as a live view from the app shell', async () => {
    mountedComponent = mount(App, { target: document.body });
    flushSync();

    const logsButton = Array.from(document.querySelectorAll('nav button')).find(
      (button) => button.textContent?.includes('Logs'),
    );

    expect(logsButton).toBeTruthy();

    logsButton?.click();
    await waitForAssertion(() => {
      expect(readLogFileMock).toHaveBeenCalledWith('2026-05-11.log');
      expect(subscribeLogEventsMock).toHaveBeenCalledWith(
        '2026-05-11.log',
        expect.objectContaining({
          onOpen: expect.any(Function),
          onEvent: expect.any(Function),
          onError: expect.any(Function),
          onClose: expect.any(Function),
        }),
        { cursor: 'app-log-cursor' },
      );
    });
    flushSync();

    expect(document.querySelector('#logs-title')?.textContent).toContain(
      'Logs',
    );
    expect(listLogsMock).toHaveBeenCalledTimes(1);
    expect(
      document
        .querySelector('button#logs-file .dropdown-primitive__trigger-label')
        ?.textContent?.trim(),
    ).toBe('2026-05-11.log');
    expect(document.body.textContent).toContain('Current file: 2026-05-11.log');
  });

  it('persists the selected agent and restores it after remount', async () => {
    const agents = [
      {
        id: 'alpha',
        name: 'Alpha',
        current_session_id: 'session-alpha',
      },
      {
        id: 'beta',
        name: 'Beta',
        current_session_id: 'session-beta',
      },
    ];
    rpcMock.mockImplementation(createChatRpcMock(agents));

    mountedComponent = mount(App, { target: document.body });
    flushSync();

    await waitForAssertion(() => {
      expect(agentTabByName('Beta')).toBeTruthy();
    });

    agentTabByName('Beta')?.click();
    flushSync();

    await waitForAssertion(() => {
      expect(localStorage.getItem('vbot.selectedAgentId')).toBe('beta');
    });

    await unmount(mountedComponent);
    mountedComponent = null;
    rpcMock.mockClear();

    mountedComponent = mount(App, { target: document.body });
    flushSync();

    await waitForAssertion(() => {
      expect(activeAgentTab()?.textContent).toContain('Beta');
      expect(rpcMock).toHaveBeenCalledWith('chat.history', {
        agent_id: 'beta',
        session_id: 'session-beta',
        limit: 100,
      });
    });
  });

  it('renders the cron navigation item with a clock icon', () => {
    mountedComponent = mount(App, { target: document.body });
    flushSync();

    const cronButton = Array.from(document.querySelectorAll('nav button')).find(
      (button) => button.textContent?.includes('Cron'),
    );

    expect(cronButton).toBeTruthy();
    expect(
      cronButton?.querySelector(
        'svg.app-shell__nav-icon circle[cx="8"][cy="8"][r="6"]',
      ),
    ).toBeTruthy();
    expect(
      cronButton?.querySelector(
        'svg.app-shell__nav-icon path[d="M8 4.5V8l2.5 2.5"]',
      ),
    ).toBeTruthy();
  });

  it('opens the same sub-agent session again after returning to the parent', async () => {
    const agents = [
      {
        id: 'alpha',
        name: 'Alpha',
        current_session_id: 'session-parent',
      },
    ];
    rpcMock.mockImplementation(createSubAgentNavigationRpcMock(agents));

    mountedComponent = mount(App, { target: document.body });
    flushSync();

    await waitForAssertion(() => {
      expect(document.body.textContent).toContain('Inspect again');
    });

    const firstViewSessionButton = viewSessionButton();
    expect(firstViewSessionButton).toBeTruthy();
    firstViewSessionButton?.click();
    flushSync();

    await waitForAssertion(() => {
      expect(document.body.textContent).toContain('Sub-agent response');
    });

    returnToCurrentSessionButton()?.click();
    flushSync();

    await waitForAssertion(() => {
      expect(document.body.textContent).toContain('Inspect again');
      expect(document.body.textContent).not.toContain('Sub-agent response');
    });

    viewSessionButton()?.click();
    flushSync();

    await waitForAssertion(() => {
      expect(
        rpcMock.mock.calls.filter(
          ([method, params]) =>
            method === 'chat.history' &&
            params?.session_id === 'sub-session-repeat',
        ),
      ).toHaveLength(2);
      expect(document.body.textContent).toContain('Sub-agent response');
    });
  });
});

async function waitForAssertion(assertion) {
  let lastError = null;

  for (let attempt = 0; attempt < 10; attempt += 1) {
    try {
      assertion();
      return;
    } catch (error) {
      lastError = error;
      await Promise.resolve();
      await new Promise((resolve) => setTimeout(resolve, 0));
      flushSync();
    }
  }

  throw lastError;
}

function createEmptyChatRpcMock() {
  return async (method) => {
    if (method === 'agent.list') {
      return { agents: [] };
    }

    if (method === 'chat.commands') {
      return { items: [] };
    }

    if (method === 'skill.list') {
      return { skills: [], invalid_skills: [] };
    }

    throw new Error(`Unexpected RPC method: ${method}`);
  };
}

function createChatRpcMock(agents) {
  return async (method, params) => {
    if (method === 'agent.list') {
      return { agents };
    }

    if (method === 'chat.commands') {
      return { items: [] };
    }

    if (method === 'chat.history') {
      return {
        agent_id: params?.agent_id ?? '',
        session_id: params?.session_id ?? '',
        messages: [],
      };
    }

    if (method === 'skill.list') {
      return { skills: [], invalid_skills: [] };
    }

    throw new Error(`Unexpected RPC method: ${method}`);
  };
}

function createSubAgentNavigationRpcMock(agents) {
  const messagesBySession = {
    'session-parent': [
      {
        id: 'parent-user',
        role: 'user',
        content: 'Start sub-agent',
      },
      {
        id: 'parent-assistant-tool',
        role: 'assistant',
        content: null,
        tool_calls: [
          {
            id: 'call-subagent-repeat',
            name: 'subagent',
            arguments: {
              agent_id: 'alpha',
              blocking: false,
              content: 'Inspect again',
            },
          },
        ],
      },
      {
        id: 'parent-tool-result',
        role: 'tool',
        tool_call_id: 'call-subagent-repeat',
        name: 'subagent',
        content: JSON.stringify({
          ok: true,
          data: {
            agent_id: 'alpha',
            session_id: 'sub-session-repeat',
            run_id: 'sub-run-repeat',
            status: 'completed',
          },
        }),
      },
    ],
    'sub-session-repeat': [
      {
        id: 'sub-agent-assistant',
        role: 'assistant',
        content: 'Sub-agent response',
      },
    ],
  };

  return async (method, params) => {
    if (method === 'agent.list') {
      return { agents };
    }

    if (method === 'chat.commands') {
      return { items: [] };
    }

    if (method === 'chat.history') {
      return {
        agent_id: params?.agent_id ?? '',
        session_id: params?.session_id ?? '',
        messages: messagesBySession[params?.session_id] ?? [],
      };
    }

    if (method === 'chat.queue_list') {
      return { items: [] };
    }

    if (method === 'skill.list') {
      return { skills: [], invalid_skills: [] };
    }

    throw new Error(`Unexpected RPC method: ${method}`);
  };
}

function agentTabByName(name) {
  return Array.from(document.querySelectorAll('.agent-tabs .agent-tab')).find(
    (button) => button.textContent?.includes(name),
  );
}

function activeAgentTab() {
  return document.querySelector('.agent-tabs .agent-tab.active');
}

function viewSessionButton() {
  return Array.from(document.querySelectorAll('button')).find(
    (button) => button.textContent?.trim() === 'view session',
  );
}

function returnToCurrentSessionButton() {
  return Array.from(document.querySelectorAll('button')).find(
    (button) => button.textContent?.trim() === 'Return to current session',
  );
}
