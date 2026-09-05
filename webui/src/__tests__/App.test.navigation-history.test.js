// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import {
  baseAgent,
  createAgentsRpcMock,
} from '../components/__tests__/AgentsView.support.js';
import {
  activeAgentTab,
  agentTabByName,
  App,
  cleanupAppHarness,
  createChatRpcMock,
  createSettingsRpcMock,
  createSubAgentNavigationRpcMock,
  listLogsMock,
  listSessionActivityMock,
  readLogFileMock,
  resetAppHarness,
  returnToCurrentSessionButton,
  rpcMock,
  settingsPanelButton,
  sidebarNavButton,
  subscribeLogEventsMock,
  viewSessionButton,
  waitForAssertion,
  waitForCondition,
} from './App.support.js';

vi.mock('svelte', async () => {
  return import('../../node_modules/svelte/src/index-client.js');
});

describe('App', () => {
  let mountedComponent;

  beforeEach(() => {
    resetAppHarness();
    mountedComponent = null;
  });

  afterEach(async () => {
    mountedComponent = await cleanupAppHarness(mountedComponent);
  });

  it('routes shared defaults search into Agents and returns ordinary navigation to the selected Agent', async () => {
    rpcMock.mockImplementation(createSettingsRpcMock());
    mountedComponent = mount(App, { target: document.body });
    flushSync();
    sidebarNavButton('Settings').click();
    await waitForCondition(() =>
      expect(
        document.querySelector('#settings-section-appearance'),
      ).toBeTruthy(),
    );
    const search = document.querySelector('.settings-search-input');
    search.value = 'thinking';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    Array.from(document.querySelectorAll('.settings-search-result'))
      .find((button) => button.textContent.includes('Shared defaults'))
      .click();
    await waitForCondition(() =>
      expect(document.querySelector('#settings-defaults-model')).toBeTruthy(),
    );
    expect(sidebarNavButton('Agents').getAttribute('aria-current')).toBe(
      'page',
    );
    sidebarNavButton('Settings').click();
    await waitForCondition(() =>
      expect(document.querySelector('.settings-content')).toBeTruthy(),
    );
    sidebarNavButton('Agents').click();
    await waitForCondition(() =>
      expect(document.querySelector('.agent-editor-host')?.hidden).toBe(false),
    );
    expect(document.querySelector('.agent-shared-pane')).toBeNull();
  });

  it('retains shared defaults when their transition save fails', async () => {
    const settingsRpc = createSettingsRpcMock();
    rpcMock.mockImplementation((method, params) =>
      method === 'settings.update'
        ? Promise.reject(new Error('save unavailable'))
        : settingsRpc(method, params),
    );
    mountedComponent = mount(App, { target: document.body });
    flushSync();
    sidebarNavButton('Agents').click();
    await waitForCondition(() =>
      expect(
        document.querySelector('.agent-list-defaults button'),
      ).toBeTruthy(),
    );
    document.querySelector('.agent-list-defaults button').click();
    await waitForCondition(() =>
      expect(
        document.querySelector('#settings-defaults-temperature'),
      ).toBeTruthy(),
    );
    const input = document.querySelector('#settings-defaults-temperature');
    input.value = '0.73';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    document.querySelector('.agent-shared-title button').click();
    await waitForCondition(() =>
      expect(
        document.querySelector(
          '[aria-labelledby="autosave-transition-failure-title"]',
        ),
      ).toBeTruthy(),
    );
    expect(document.querySelector('.agent-shared-pane').hidden).toBe(false);
    expect(input.value).toBe('0.73');
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

    expect(listLogsMock).toHaveBeenCalledTimes(1);
    expect(
      document
        .querySelector('button#logs-file .dropdown-primitive__trigger-label')
        ?.textContent?.trim(),
    ).toBe('2026-05-11.log');
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
      (button) => button.textContent?.includes('Schedules'),
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

  it('renders the projects navigation item with a folder icon', () => {
    mountedComponent = mount(App, { target: document.body });
    flushSync();

    const projectsButton = Array.from(
      document.querySelectorAll('nav button'),
    ).find((button) => button.textContent?.includes('Projects'));

    expect(projectsButton).toBeTruthy();
    expect(
      projectsButton?.querySelector(
        'svg.app-shell__nav-icon path[d="M2 12.5V4h4l1.5 1.5h6.5v7z"]',
      ),
    ).toBeTruthy();
  });

  it('renders the statistics navigation item with a bar-chart icon', () => {
    mountedComponent = mount(App, { target: document.body });
    flushSync();

    const statisticsButton = Array.from(
      document.querySelectorAll('nav button'),
    ).find((button) => button.textContent?.includes('Statistics'));

    expect(statisticsButton).toBeTruthy();
    expect(
      statisticsButton?.querySelector(
        'svg.app-shell__nav-icon path[d="M4.5 13.5V10.5M8 13.5V8M11.5 13.5V5"]',
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
      // Back in the parent session: the sub-agent session notice is gone. The
      // child's response now also surfaces in the parent tool block, so its
      // presence no longer signals which session is displayed.
      expect(returnToCurrentSessionButton()).toBeFalsy();
    });

    viewSessionButton()?.click();
    flushSync();

    await waitForAssertion(() => {
      // Two navigation loads (limit 100). The one-off result fetch (limit 20)
      // that surfaces the response in the parent tool block is deduped and
      // excluded here.
      expect(
        rpcMock.mock.calls.filter(
          ([method, params]) =>
            method === 'chat.history' &&
            params?.session_id === 'sub-session-repeat' &&
            params?.limit === 100,
        ),
      ).toHaveLength(2);
      expect(document.body.textContent).toContain('Sub-agent response');
    });
  });

  it('treats tab switches as history entries so browser back returns to the previous tab', async () => {
    mountedComponent = mount(App, { target: document.body });
    flushSync();

    sidebarNavButton('Logs')?.click();
    flushSync();

    await waitForCondition(() => {
      expect(document.querySelector('#logs-title')).toBeTruthy();
      expect(window.location.hash).toBe('#logs');
    });

    window.history.back();

    await waitForCondition(() => {
      expect(window.location.hash).toBe('#chat');
      expect(document.querySelector('#logs-title')).toBeFalsy();
      expect(sidebarNavButton('Chat')?.getAttribute('aria-current')).toBe(
        'page',
      );
    });
  });

  it('keeps Chat initialized while another main view is active', async () => {
    const agents = [
      { id: 'alpha', name: 'Alpha', current_session_id: 'session-alpha' },
    ];
    rpcMock.mockImplementation(createChatRpcMock(agents));
    listSessionActivityMock.mockResolvedValue({
      agents: [
        {
          agent_id: 'alpha',
          project_id: null,
          sessions: [],
        },
      ],
    });
    mountedComponent = mount(App, { target: document.body });
    flushSync();

    await waitForCondition(() => {
      expect(document.querySelector('#chat-composer-input')).toBeTruthy();
      expect(
        rpcMock.mock.calls.some(([method]) => method === 'chat.history'),
      ).toBe(true);
      expect(listSessionActivityMock).toHaveBeenCalled();
    });

    const composer = document.querySelector('#chat-composer-input');
    const chatView = document.querySelector('.chat-view');
    const timeline = document.querySelector('.messages');
    composer.value = 'Draft kept across views';
    composer.dispatchEvent(new Event('input', { bubbles: true }));
    timeline.scrollTop = 137;
    flushSync();

    const agentLoadsBeforeSwitch = rpcMock.mock.calls.filter(
      ([method]) => method === 'agent.list',
    ).length;
    const historyLoadsBeforeSwitch = rpcMock.mock.calls.filter(
      ([method]) => method === 'chat.history',
    ).length;
    const activityLoadsBeforeSwitch = listSessionActivityMock.mock.calls.length;

    sidebarNavButton('Logs')?.click();
    await waitForCondition(() => {
      expect(document.querySelector('#logs-title')).toBeTruthy();
      expect(document.querySelector('.chat-view')).toBe(chatView);
      expect(chatView.hidden).toBe(true);
      expect(document.querySelector('#chat-composer-input')).toBe(composer);
    });

    sidebarNavButton('Chat')?.click();
    await waitForCondition(() => {
      expect(document.querySelector('.chat-view')).toBe(chatView);
      expect(chatView.hidden).toBe(false);
      expect(document.querySelector('#chat-composer-input')).toBe(composer);
      expect(document.querySelector('#chat-composer-input')?.value).toBe(
        'Draft kept across views',
      );
      expect(document.querySelector('.messages')?.scrollTop).toBe(137);
    });

    expect(
      rpcMock.mock.calls.filter(([method]) => method === 'agent.list'),
    ).toHaveLength(agentLoadsBeforeSwitch);
    expect(
      rpcMock.mock.calls.filter(([method]) => method === 'chat.history'),
    ).toHaveLength(historyLoadsBeforeSwitch);
    expect(listSessionActivityMock).toHaveBeenCalledTimes(
      activityLoadsBeforeSwitch,
    );
    expect(document.querySelector('.chat-view__state-banner')).toBeNull();
  });

  it('restores the Settings topic and its reading position after switching to another tab', async () => {
    rpcMock.mockImplementation(createSettingsRpcMock());
    mountedComponent = mount(App, { target: document.body });
    flushSync();

    sidebarNavButton('Settings')?.click();
    await waitForCondition(() => {
      expect(
        document.querySelector('[data-settings-section="providers"]'),
      ).toBeTruthy();
    });

    settingsPanelButton('Specialized Models')?.click();
    await waitForCondition(() => {
      expect(
        document.querySelector('[data-settings-section="specialized_models"]')
          .hidden,
      ).toBe(false);
    });
    const firstScrollContainer = document.querySelector('.settings-content');
    firstScrollContainer.scrollTop = 640;

    sidebarNavButton('Logs')?.click();
    await waitForCondition(() => {
      expect(document.querySelector('#logs-title')).toBeTruthy();
    });

    sidebarNavButton('Settings')?.click();
    await waitForCondition(() => {
      const restoredContainer = document.querySelector('.settings-content');
      expect(restoredContainer).toBeTruthy();
      expect(restoredContainer).not.toBe(firstScrollContainer);
      expect(restoredContainer.scrollTop).toBe(640);
      expect(
        document.querySelector('[data-settings-section="specialized_models"]')
          .hidden,
      ).toBe(false);
    });
  });

  it('flushes a pending Settings autosave before switching app tabs', async () => {
    const settingsRpc = createSettingsRpcMock();
    let resolveUpdate;
    rpcMock.mockImplementation((method, params) => {
      if (method === 'settings.update') {
        return new Promise((resolve) => {
          resolveUpdate = () => resolve(settingsRpc(method, params));
        });
      }
      return settingsRpc(method, params);
    });
    mountedComponent = mount(App, { target: document.body });
    flushSync();

    sidebarNavButton('Settings')?.click();
    await waitForCondition(() => {
      expect(settingsPanelButton('Sub-Agents')).toBeTruthy();
    });
    settingsPanelButton('Sub-Agents')?.click();
    const depthInput = document.querySelector(
      'input[aria-label="Max sub-agent depth"]',
    );
    depthInput.value = '5';
    depthInput.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    sidebarNavButton('Logs')?.click();
    await waitForCondition(() => {
      expect(rpcMock).toHaveBeenCalledWith('settings.update', {
        subagents: {
          max_subagent_depth: 5,
          max_subagents_per_turn: 8,
          subagent_timeout_minutes: 60,
        },
      });
    });
    expect(sidebarNavButton('Settings')?.getAttribute('aria-current')).toBe(
      'page',
    );
    expect(document.querySelector('#logs-title')).toBeFalsy();

    resolveUpdate();
    await waitForCondition(() => {
      expect(document.querySelector('#logs-title')).toBeTruthy();
    });
  });

  it('saves an Agent tool change before leaving the Agents tab', async () => {
    const agents = [
      baseAgent(),
      { ...baseAgent(), id: 'bravo', name: 'Bravo' },
    ];
    let resolveAgentUpdate;
    const agentsRpc = createAgentsRpcMock({
      agents,
      tools: [
        { name: 'bash', description: 'Run shell commands.' },
        { name: 'write', description: 'Write files.' },
      ],
      agentUpdate: (params) =>
        new Promise((resolve) => {
          resolveAgentUpdate = () =>
            resolve({
              ...baseAgent(),
              ...params,
              current_session_id: 'session-1',
            });
        }),
    });
    rpcMock.mockImplementation((method, params) => {
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
      if (method === 'chat.queue_list') {
        return { items: [] };
      }
      return agentsRpc(method, params);
    });
    mountedComponent = mount(App, { target: document.body });
    flushSync();

    sidebarNavButton('Agents')?.click();
    await waitForCondition(() => {
      expect(
        document.querySelector(
          '[data-tool-name="write"][data-tool-access-toggle]',
        ),
      ).toBeTruthy();
    });
    document
      .querySelector('[data-tool-name="write"][data-tool-access-toggle]')
      ?.click();
    flushSync();
    sidebarNavButton('Chat')?.click();

    await waitForCondition(() => {
      expect(rpcMock).toHaveBeenCalledWith('agent.update', {
        id: 'alpha',
        tool_access: { mode: 'all', denied: ['write'] },
      });
    });
    expect(sidebarNavButton('Agents')?.getAttribute('aria-current')).toBe(
      'page',
    );

    resolveAgentUpdate();
    await waitForCondition(() => {
      expect(sidebarNavButton('Chat')?.getAttribute('aria-current')).toBe(
        'page',
      );
    });
  });

  it('keeps the editor open after a failed transition save and retries it', async () => {
    const settingsRpc = createSettingsRpcMock();
    let updateAttempt = 0;
    rpcMock.mockImplementation((method, params) => {
      if (method === 'settings.update' && updateAttempt++ === 0) {
        return Promise.reject(new Error('save unavailable'));
      }
      return settingsRpc(method, params);
    });
    mountedComponent = mount(App, { target: document.body });
    flushSync();

    sidebarNavButton('Settings')?.click();
    await waitForCondition(() => {
      expect(settingsPanelButton('Sub-Agents')).toBeTruthy();
    });
    const depthInput = document.querySelector(
      'input[aria-label="Max sub-agent depth"]',
    );
    depthInput.value = '5';
    depthInput.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    sidebarNavButton('Logs')?.click();

    await waitForCondition(() => {
      expect(
        document.querySelector(
          '[role="dialog"][aria-labelledby="autosave-transition-failure-title"]',
        ),
      ).toBeTruthy();
    });
    expect(sidebarNavButton('Settings')?.getAttribute('aria-current')).toBe(
      'page',
    );
    const failureDialog = document.querySelector(
      '[role="dialog"][aria-labelledby="autosave-transition-failure-title"]',
    );
    expect(
      Array.from(failureDialog.querySelectorAll('button')).some(
        (button) => button.textContent?.trim() === 'Discard and continue',
      ),
    ).toBe(true);

    Array.from(failureDialog.querySelectorAll('button'))
      .find((button) => button.textContent?.trim() === 'Retry')
      ?.click();
    await waitForCondition(() => {
      expect(updateAttempt).toBeGreaterThanOrEqual(2);
      expect(document.querySelector('#logs-title')).toBeTruthy();
    });
  });

  it('can discard an unsaved draft after a failed transition save', async () => {
    const settingsRpc = createSettingsRpcMock();
    rpcMock.mockImplementation((method, params) => {
      if (method === 'settings.update') {
        return Promise.reject(new Error('save unavailable'));
      }
      return settingsRpc(method, params);
    });
    mountedComponent = mount(App, { target: document.body });
    flushSync();

    sidebarNavButton('Settings')?.click();
    await waitForCondition(() => {
      expect(settingsPanelButton('Sub-Agents')).toBeTruthy();
    });
    const depthInput = document.querySelector(
      'input[aria-label="Max sub-agent depth"]',
    );
    depthInput.value = '5';
    depthInput.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    sidebarNavButton('Logs')?.click();

    await waitForCondition(() => {
      expect(
        document.querySelector(
          '[role="dialog"][aria-labelledby="autosave-transition-failure-title"]',
        ),
      ).toBeTruthy();
    });
    const failureDialog = document.querySelector(
      '[role="dialog"][aria-labelledby="autosave-transition-failure-title"]',
    );
    Array.from(failureDialog.querySelectorAll('button'))
      .find((button) => button.textContent?.trim() === 'Discard and continue')
      ?.click();

    await waitForCondition(() => {
      expect(document.querySelector('#logs-title')).toBeTruthy();
      expect(
        document.querySelector(
          '[role="dialog"][aria-labelledby="autosave-transition-failure-title"]',
        ),
      ).toBeFalsy();
    });
  });

  it('returns from a sub-agent session override to the parent session via browser back', async () => {
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

    viewSessionButton()?.click();
    flushSync();

    await waitForAssertion(() => {
      expect(returnToCurrentSessionButton()).toBeTruthy();
    });

    window.history.back();

    await waitForCondition(() => {
      expect(document.body.textContent).toContain('Inspect again');
      expect(returnToCurrentSessionButton()).toBeFalsy();
    });
  });

  it('restores the selected agent together with the session override on browser back (item 1)', async () => {
    const agents = [
      { id: 'alpha', name: 'Alpha', current_session_id: 'session-parent' },
      { id: 'beta', name: 'Beta', current_session_id: 'session-beta' },
    ];
    rpcMock.mockImplementation(createSubAgentNavigationRpcMock(agents));

    mountedComponent = mount(App, { target: document.body });
    flushSync();

    await waitForAssertion(() => {
      expect(document.body.textContent).toContain('Inspect again');
    });

    // Open the sub-agent session (override entry pushed with selection alpha).
    viewSessionButton()?.click();
    flushSync();
    await waitForAssertion(() => {
      expect(document.body.textContent).toContain('Sub-agent response');
    });

    // Switch to Beta — override clears, a new entry with selection beta lands.
    agentTabByName('Beta')?.click();
    flushSync();
    await waitForAssertion(() => {
      expect(activeAgentTab()?.textContent).toContain('Beta');
      expect(document.body.textContent).not.toContain('Sub-agent response');
    });

    window.history.back();

    // Back restores the WHOLE chat context of the entry: the sub-agent
    // session view AND the selected-agent chip (previously the chip stayed
    // on Beta while Alpha's child session was displayed).
    await waitForAssertion(() => {
      expect(document.body.textContent).toContain('Sub-agent response');
      expect(activeAgentTab()?.textContent).toContain('Alpha');
    });
  });

  it('keeps display and history entry in sync after tab-away/tab-back plus double back (item 3)', async () => {
    const agents = [
      { id: 'alpha', name: 'Alpha', current_session_id: 'session-parent' },
    ];
    rpcMock.mockImplementation(createSubAgentNavigationRpcMock(agents));

    mountedComponent = mount(App, { target: document.body });
    flushSync();

    await waitForAssertion(() => {
      expect(document.body.textContent).toContain('Inspect again');
    });

    viewSessionButton()?.click();
    flushSync();
    await waitForAssertion(() => {
      expect(document.body.textContent).toContain('Sub-agent response');
    });

    // Tab away and back while the persistent Chat owner retains the override.
    sidebarNavButton('Logs')?.click();
    flushSync();
    await waitForCondition(() => {
      expect(window.location.hash).toBe('#logs');
    });
    sidebarNavButton('Chat')?.click();
    flushSync();
    await waitForCondition(() => {
      expect(window.location.hash).toBe('#chat');
    });

    // Back #1 lands on the Logs entry.
    window.history.back();
    await waitForCondition(() => {
      expect(window.location.hash).toBe('#logs');
    });

    // Back #2 pops the chat+child entry: the sub-agent session is displayed
    // AND history.state still carries the override — passive restoration must
    // not push a phantom chat(null) entry over the restored one.
    window.history.back();
    await waitForAssertion(() => {
      expect(window.location.hash).toBe('#chat');
      expect(document.body.textContent).toContain('Sub-agent response');
      expect(window.history.state?.session?.sessionId).toBe(
        'sub-session-repeat',
      );
    });
  });
});
