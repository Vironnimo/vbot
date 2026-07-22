// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import {
  activeAgentTab,
  agentTabByName,
  App,
  cleanupAppHarness,
  createChatRpcMock,
  createSettingsRpcMock,
  createSubAgentNavigationRpcMock,
  listLogsMock,
  readLogFileMock,
  resetAppHarness,
  returnToCurrentSessionButton,
  rpcMock,
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

  it('restores the Settings reading position after switching to another tab', async () => {
    rpcMock.mockImplementation(createSettingsRpcMock());
    mountedComponent = mount(App, { target: document.body });
    flushSync();

    sidebarNavButton('Settings')?.click();
    await waitForCondition(() => {
      expect(document.querySelector('.settings-scroll-tail')).toBeTruthy();
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

    // Tab away (override dies with ChatView) and back to Chat.
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
    // AND history.state still carries the override — the remount must not
    // push a phantom chat(null) entry over the restored one.
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
