// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import {
  baseAgent,
  createAgentsRpcMock,
} from '../components/__tests__/AgentsView.support.js';
import { AUTOSAVE_STILL_SAVING_MS } from '../lib/autosave.js';
import { t } from '../lib/i18n.js';
import {
  App,
  buttonWithText,
  cleanupAppHarness,
  createAppRpcMock,
  createSettingsRpcMock,
  createSubAgentNavigationRpcMock,
  listSessionActivityMock,
  resetAppHarness,
  returnToCurrentSessionButton,
  rpcMock,
  selectPersonalAgent,
  selectedPersonalAgentName,
  settingsPanelButton,
  sidebarNavButton,
  viewSessionButton,
  waitForCondition,
} from './App.support.js';

vi.mock('svelte', async () => {
  return import('../../node_modules/svelte/src/index-client.js');
});

const FIXTURE_EXTENSION_ROUTE = 'extension:fixture:main';
const FIXTURE_EXTENSION_PAGE = {
  extension: 'fixture',
  page: 'main',
  title: 'Fixture page',
  route: FIXTURE_EXTENSION_ROUTE,
  entry_url: '/fixture-page.html',
  epoch: 'fixture-epoch',
};
const TRANSITION_FAILURE_DIALOG =
  '[role="dialog"][aria-labelledby="autosave-transition-failure-title"]';
const STILL_SAVING_DIALOG =
  '[role="dialog"][aria-labelledby="autosave-transition-slow-title"]';

const isCurrent = (viewId) =>
  sidebarNavButton(viewId)?.getAttribute('aria-current') === 'page';
const logsShown = () => Boolean(document.querySelector('#logs-title'));
const depthInput = () =>
  document.querySelector(
    `input[aria-label="${t('settings.subagents.maxDepth')}"]`,
  );
const settingsUpdates = () =>
  rpcMock.mock.calls.filter(([method]) => method === 'settings.update');
const agentButton = (agentId) =>
  document.querySelector(
    `.agent-list [data-sortable-key="${agentId}"] button.agent-item`,
  );
const agentShown = (agentId) =>
  agentButton(agentId)?.classList.contains('active') === true;
const labelledButton = (key) =>
  document.querySelector(`button[aria-label="${t(key)}"]`);

// The Agents view's reads plus the Chat startup reads.
function agentsAppRpc(options) {
  const agentsRpc = createAgentsRpcMock(options);
  const chatRpc = createAppRpcMock();
  return (method, params) =>
    ['chat.commands', 'chat.history', 'chat.queue_list'].includes(method)
      ? chatRpc(method, params)
      : agentsRpc(method, params);
}

function typeInto(input, value) {
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

// Settings whose `settings.update` fails for the first `failures` saves.
function failingSettingsRpc(failures) {
  const settingsRpc = createSettingsRpcMock();
  let attempt = 0;
  return (method, params) =>
    method === 'settings.update' && attempt++ < failures
      ? Promise.reject(new Error('save unavailable'))
      : settingsRpc(method, params);
}

describe('App navigation', () => {
  let mountedComponent;

  beforeEach(() => {
    resetAppHarness();
    mountedComponent = null;
  });

  afterEach(async () => {
    vi.useRealTimers();
    mountedComponent = await cleanupAppHarness(mountedComponent);
  });

  function mountApp() {
    mountedComponent = mount(App, { target: document.body });
    flushSync();
  }

  async function openSettingsTools() {
    sidebarNavButton('settings').click();
    await waitForCondition(() =>
      expect(settingsPanelButton('tools')).toBeTruthy(),
    );
    settingsPanelButton('tools').click();
    flushSync();
  }

  async function openSubAgentSession() {
    await waitForCondition(() => {
      expect(document.body.textContent).toContain('Inspect again');
    });
    viewSessionButton().click();
    flushSync();
    await waitForCondition(() => {
      expect(document.body.textContent).toContain('Sub-agent response');
    });
  }

  it('routes shared defaults search into Agents, returns there from the Main menu and starts over on a second click', async () => {
    rpcMock.mockImplementation(createSettingsRpcMock());
    mountApp();
    sidebarNavButton('settings').click();
    await waitForCondition(() =>
      expect(
        document.querySelector('#settings-section-appearance'),
      ).toBeTruthy(),
    );
    typeInto(document.querySelector('.settings-search-input'), 'thinking');
    Array.from(document.querySelectorAll('.settings-search-result'))
      .find((button) => button.textContent.includes(t('agents.shared.title')))
      .click();
    await waitForCondition(() =>
      expect(document.querySelector('#settings-defaults-model')).toBeTruthy(),
    );
    expect(isCurrent('agents')).toBe(true);

    sidebarNavButton('settings').click();
    await waitForCondition(() =>
      expect(document.querySelector('.settings-content')).toBeTruthy(),
    );
    // The Main menu returns to the place Agents showed last.
    sidebarNavButton('agents').click();
    await waitForCondition(() => {
      expect(document.querySelector('#settings-defaults-model')).toBeTruthy();
      expect(window.location.hash).toBe('#agents/~defaults');
    });

    // Choosing the shown view again starts at its start: the selected Agent.
    sidebarNavButton('agents').click();
    await waitForCondition(() => {
      expect(document.querySelector('.agent-editor-host')?.hidden).toBe(false);
      expect(document.querySelector('.agent-shared-pane')?.hidden).toBe(true);
    });
  });

  it('records each chosen record as a step and restores it with Back and Forward', async () => {
    rpcMock.mockImplementation(
      agentsAppRpc({
        agents: [baseAgent(), { ...baseAgent(), id: 'bravo', name: 'Bravo' }],
      }),
    );
    mountApp();
    sidebarNavButton('agents').click();
    await waitForCondition(() => expect(agentButton('bravo')).toBeTruthy());

    agentButton('bravo').click();
    await waitForCondition(() => {
      expect(window.location.hash).toBe('#agents/bravo');
      expect(agentShown('bravo')).toBe(true);
    });

    agentButton('alpha').click();
    await waitForCondition(() => {
      expect(window.location.hash).toBe('#agents/alpha');
      expect(agentShown('alpha')).toBe(true);
    });

    window.history.back();
    await waitForCondition(() => {
      expect(window.location.hash).toBe('#agents/bravo');
      expect(agentShown('bravo')).toBe(true);
    });

    window.history.forward();
    await waitForCondition(() => {
      expect(window.location.hash).toBe('#agents/alpha');
      expect(agentShown('alpha')).toBe(true);
    });
  });

  it('closes an open dialog on Back and stays at the place', async () => {
    rpcMock.mockImplementation(agentsAppRpc({ agents: [baseAgent()] }));
    mountApp();
    sidebarNavButton('logs').click();
    sidebarNavButton('agents').click();
    await waitForCondition(() => expect(agentShown('alpha')).toBe(true));
    const hash = window.location.hash;

    labelledButton('agents.create').click();
    await waitForCondition(() =>
      expect(document.querySelector('[role="dialog"]')).toBeTruthy(),
    );

    window.history.back();
    await waitForCondition(() => {
      expect(document.querySelector('[role="dialog"]')).toBeNull();
      expect(window.location.hash).toBe(hash);
    });
    expect(isCurrent('agents')).toBe(true);
  });

  it('keeps the Desktop app open at its first entry and moves with Alt+Arrow keys', async () => {
    window.history.replaceState(null, '', '/?accessor=desktop');
    mountApp();

    sidebarNavButton('logs').click();
    await waitForCondition(() => expect(logsShown()).toBe(true));

    window.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'ArrowLeft',
        altKey: true,
        cancelable: true,
      }),
    );
    await waitForCondition(() => expect(isCurrent('chat')).toBe(true));

    // Back from the first app entry reaches the floor below it, which sends
    // the WebView forward again.
    const popped = [];
    const recordPop = (event) => popped.push(event.state);
    window.addEventListener('popstate', recordPop);
    try {
      window.history.back();
      await waitForCondition(() => expect(popped).toHaveLength(2));
    } finally {
      window.removeEventListener('popstate', recordPop);
    }
    expect(popped[0]?.floor).toBe(true);
    expect(window.history.state?.view).toBe('chat');
    expect(isCurrent('chat')).toBe(true);
  });

  it('retains shared defaults when their transition save fails', async () => {
    rpcMock.mockImplementation(failingSettingsRpc(Infinity));
    mountApp();
    sidebarNavButton('agents').click();
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
    typeInto(input, '0.73');
    document.querySelector('.agent-shared-title button').click();
    await waitForCondition(() =>
      expect(document.querySelector(TRANSITION_FAILURE_DIALOG)).toBeTruthy(),
    );
    expect(document.querySelector('.agent-shared-pane').hidden).toBe(false);
    expect(input.value).toBe('0.73');
  });

  it('persists the selected Agent and restores it after remount', async () => {
    rpcMock.mockImplementation(
      createAppRpcMock({
        agents: [
          { id: 'alpha', name: 'Alpha', current_session_id: 'session-alpha' },
          { id: 'beta', name: 'Beta', current_session_id: 'session-beta' },
        ],
      }),
    );
    mountApp();

    await selectPersonalAgent('Beta');
    await waitForCondition(() => {
      expect(localStorage.getItem('vbot.selectedAgentId')).toBe('beta');
    });

    await unmount(mountedComponent);
    rpcMock.mockClear();
    mountApp();

    await waitForCondition(() => {
      expect(selectedPersonalAgentName()).toBe('Beta');
      expect(rpcMock).toHaveBeenCalledWith('chat.history', {
        agent_id: 'beta',
        session_id: 'session-beta',
        limit: 100,
      });
    });
  });

  it('opens an Extension page deep link once the page catalog has loaded', async () => {
    let releasePages;
    const pages = new Promise((resolve) => {
      releasePages = () => resolve({ pages: [FIXTURE_EXTENSION_PAGE] });
    });
    rpcMock.mockImplementation(
      createAppRpcMock({ methods: { 'extensions.pages': () => pages } }),
    );
    window.history.replaceState(null, '', `#${FIXTURE_EXTENSION_ROUTE}`);
    mountApp();

    // Startup cannot know the route yet; the link stays in the URL meanwhile.
    expect(window.location.hash).toBe(`#${FIXTURE_EXTENSION_ROUTE}`);
    expect(document.querySelector('iframe')).toBeFalsy();

    releasePages();

    await waitForCondition(() => {
      expect(document.querySelector('iframe')).toBeTruthy();
      expect(isCurrent('Fixture page')).toBe(true);
      expect(window.location.hash).toBe(`#${FIXTURE_EXTENSION_ROUTE}`);
      expect(window.history.state?.view).toBe(FIXTURE_EXTENSION_ROUTE);
    });
  });

  describe('Session links from outside the page', () => {
    const ALPHA = {
      id: 'alpha',
      name: 'Alpha',
      current_session_id: 'session-alpha',
    };
    const olderSessionRpc = () =>
      createAppRpcMock({
        agents: [ALPHA],
        history: (params) => ({
          messages:
            params.session_id === 'session-old'
              ? [{ id: 'old-reply', role: 'assistant', content: 'Older reply' }]
              : [],
        }),
      });

    it('opens the Session a startup link names and takes the link out of the address bar', async () => {
      rpcMock.mockImplementation(olderSessionRpc());
      window.history.replaceState(
        null,
        '',
        '/?desktop_session=launch-1&open_agent=alpha&open_session=session-old',
      );
      mountApp();

      expect(window.location.search).toBe('?desktop_session=launch-1');
      await waitForCondition(() => {
        expect(document.body.textContent).toContain('Older reply');
        expect(window.location.hash).toBe('#chat/alpha/session-old');
      });

      // Back returns to the Agent's current Session; no entry keeps the link.
      window.history.back();
      await waitForCondition(() => {
        expect(document.body.textContent).not.toContain('Older reply');
        expect(window.location.hash).toBe('#chat/alpha/session-alpha');
      });
      expect(window.location.search).toBe('?desktop_session=launch-1');
    });

    it('keeps the app usable when a startup link names an unknown Session', async () => {
      const appRpc = createAppRpcMock({
        agents: [
          ALPHA,
          { id: 'beta', name: 'Beta', current_session_id: 'session-beta' },
        ],
      });
      rpcMock.mockImplementation((method, params) =>
        method === 'chat.history' && params?.session_id === 'missing'
          ? Promise.reject(new Error('Session not found'))
          : appRpc(method, params),
      );
      window.history.replaceState(
        null,
        '',
        '/?open_agent=alpha&open_session=missing',
      );
      mountApp();

      await waitForCondition(() => {
        expect(document.body.textContent).toContain('Session not found');
      });
      expect(window.location.search).toBe('');

      await selectPersonalAgent('Beta');
      await waitForCondition(() => {
        expect(rpcMock).toHaveBeenCalledWith('chat.history', {
          agent_id: 'beta',
          session_id: 'session-beta',
          limit: 100,
        });
        expect(document.body.textContent).not.toContain('Session not found');
      });
    });

    it('opens the Session the Desktop app asks for', async () => {
      rpcMock.mockImplementation(olderSessionRpc());
      window.history.replaceState(null, '', '/?accessor=desktop');
      mountApp();
      await waitForCondition(() => {
        expect(rpcMock).toHaveBeenCalledWith('chat.history', {
          agent_id: 'alpha',
          session_id: 'session-alpha',
          limit: 100,
        });
      });

      const handled = !window.dispatchEvent(
        new CustomEvent('vbot-desktop-open-session', {
          cancelable: true,
          detail: { agent: 'alpha', session: 'session-old' },
        }),
      );

      expect(handled).toBe(true);
      await waitForCondition(() => {
        expect(document.body.textContent).toContain('Older reply');
        expect(window.location.hash).toBe('#chat/alpha/session-old');
      });
    });
  });

  it('treats tab switches as history entries so browser back returns to the previous tab', async () => {
    mountApp();

    sidebarNavButton('logs').click();
    flushSync();
    await waitForCondition(() => {
      expect(logsShown()).toBe(true);
      expect(window.location.hash).toMatch(/^#logs(\/|$)/);
    });

    window.history.back();

    await waitForCondition(() => {
      expect(window.location.hash).toBe('#chat');
      expect(logsShown()).toBe(false);
      expect(isCurrent('chat')).toBe(true);
    });
  });

  it('keeps Chat initialized while another main view is active', async () => {
    rpcMock.mockImplementation(
      createAppRpcMock({
        agents: [
          { id: 'alpha', name: 'Alpha', current_session_id: 'session-alpha' },
        ],
      }),
    );
    listSessionActivityMock.mockResolvedValue({
      agents: [{ agent_id: 'alpha', project_id: null, sessions: [] }],
    });
    mountApp();

    await waitForCondition(() => {
      expect(document.querySelector('.msg-input')).toBeTruthy();
      expect(rpcMock).toHaveBeenCalledWith('chat.history', expect.anything());
      expect(listSessionActivityMock).toHaveBeenCalled();
    });

    const composer = document.querySelector('.msg-input');
    const chatView = document.querySelector('.chat-view');
    const timeline = document.querySelector('.messages');
    typeInto(composer, 'Draft kept across views');
    timeline.scrollTop = 137;

    const readsBeforeSwitch = rpcMock.mock.calls.length;
    const activityReadsBeforeSwitch = listSessionActivityMock.mock.calls.length;

    sidebarNavButton('logs').click();
    await waitForCondition(() => {
      expect(logsShown()).toBe(true);
      expect(document.querySelector('.chat-view')).toBe(chatView);
      expect(chatView.hidden).toBe(true);
      expect(document.querySelector('.msg-input')).toBe(composer);
    });

    sidebarNavButton('chat').click();
    await waitForCondition(() => {
      expect(document.querySelector('.chat-view')).toBe(chatView);
      expect(chatView.hidden).toBe(false);
      expect(document.querySelector('.msg-input')).toBe(composer);
      expect(composer.value).toBe('Draft kept across views');
      expect(document.querySelector('.messages')?.scrollTop).toBe(137);
    });

    const readsAfterSwitch = rpcMock.mock.calls
      .slice(readsBeforeSwitch)
      .map(([method]) => method);
    expect(readsAfterSwitch).not.toContain('agent.list');
    expect(readsAfterSwitch).not.toContain('chat.history');
    expect(listSessionActivityMock).toHaveBeenCalledTimes(
      activityReadsBeforeSwitch,
    );
    expect(document.querySelector('.chat-view__state-banner')).toBeNull();
  });

  it('names the shown Session when Chat is first opened after loading in the background', async () => {
    rpcMock.mockImplementation(
      createAppRpcMock({
        agents: [
          { id: 'alpha', name: 'Alpha', current_session_id: 'session-alpha' },
        ],
      }),
    );
    window.history.replaceState(null, '', '#logs');
    mountApp();

    await waitForCondition(() => {
      expect(logsShown()).toBe(true);
      expect(rpcMock).toHaveBeenCalledWith('chat.history', expect.anything());
    });

    sidebarNavButton('chat').click();
    await waitForCondition(() => {
      expect(isCurrent('chat')).toBe(true);
      expect(window.location.hash).toBe('#chat/alpha/session-alpha');
    });
  });

  it('keeps an unsaved new Cron job across other views and guards unloading the page', async () => {
    rpcMock.mockImplementation(
      createAppRpcMock({
        agents: [{ id: 'alpha', name: 'Alpha' }],
        methods: {
          'cron.list': () => ({ jobs: [] }),
          'project.list': () => ({ projects: [] }),
        },
      }),
    );
    mountApp();
    const prompt = () => document.getElementById('cron-job-prompt');
    sidebarNavButton('cron').click();
    await waitForCondition(() =>
      expect(labelledButton('cron.detail.createTitle')?.disabled).toBe(false),
    );
    labelledButton('cron.detail.createTitle').click();
    await waitForCondition(() => expect(prompt()).toBeTruthy());
    typeInto(prompt(), 'Unfinished prompt');

    sidebarNavButton('logs').click();
    await waitForCondition(() => expect(logsShown()).toBe(true));
    const unload = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(true);

    window.history.back();
    await waitForCondition(() => {
      expect(window.location.hash).toBe('#cron/new');
      expect(prompt()?.value).toBe('Unfinished prompt');
    });
  });

  it('retains Settings input after a failed topic change and retries the same navigation', async () => {
    rpcMock.mockImplementation(failingSettingsRpc(1));
    mountApp();
    await openSettingsTools();
    await waitForCondition(() =>
      expect(
        document.querySelector('[data-settings-section="subagents"]').hidden,
      ).toBe(false),
    );
    const input = depthInput();
    typeInto(input, '6');
    settingsPanelButton('general').click();
    await waitForCondition(() =>
      expect(document.querySelector('[role="dialog"]')).toBeTruthy(),
    );
    expect(
      document.querySelector('[data-settings-section="subagents"]').hidden,
    ).toBe(false);
    expect(input.value).toBe('6');

    buttonWithText('[role="dialog"] button', t('common.retry')).click();
    await waitForCondition(() =>
      expect(
        document.querySelector('[data-settings-section="appearance"]').hidden,
      ).toBe(false),
    );
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(rpcMock).toHaveBeenCalledWith('settings.update', {
      subagents: expect.objectContaining({ max_subagent_depth: 6 }),
      base: { subagents: expect.objectContaining({ max_subagent_depth: 4 }) },
    });
  });

  it('restores the Settings topic and its reading position after switching to another tab', async () => {
    rpcMock.mockImplementation(createSettingsRpcMock());
    mountApp();
    await openSettingsTools();
    // The topic change ends by focusing the page heading.
    await waitForCondition(() => {
      expect(document.activeElement?.id).toBe('settings-page-tools');
    });
    const firstScrollContainer = document.querySelector('.settings-content');
    firstScrollContainer.scrollTop = 640;

    sidebarNavButton('logs').click();
    await waitForCondition(() => {
      expect(logsShown()).toBe(true);
    });

    sidebarNavButton('settings').click();
    await waitForCondition(() => {
      const restoredContainer = document.querySelector('.settings-content');
      expect(restoredContainer).not.toBe(firstScrollContainer);
      expect(restoredContainer.scrollTop).toBe(640);
      expect(
        document.querySelector('[data-settings-section="media_models"]').hidden,
      ).toBe(false);
    });
  });

  it('flushes edits before the latest requested tab and guards closing while pending', async () => {
    const settingsRpc = createSettingsRpcMock();
    let resolveUpdate;
    rpcMock.mockImplementation((method, params) =>
      method === 'settings.update'
        ? new Promise((resolve) => {
            resolveUpdate = () => resolve(settingsRpc(method, params));
          })
        : settingsRpc(method, params),
    );
    mountApp();
    await openSettingsTools();
    typeInto(depthInput(), '5');

    sidebarNavButton('logs').click();
    await waitForCondition(() => {
      expect(rpcMock).toHaveBeenCalledWith('settings.update', {
        subagents: {
          max_subagent_depth: 5,
          max_active_subagents: 8,
          max_active_subagents_total: 50,
        },
        base: {
          subagents: {
            max_subagent_depth: 4,
            max_active_subagents: 8,
            max_active_subagents_total: 50,
          },
        },
      });
    });
    expect(isCurrent('settings')).toBe(true);
    expect(logsShown()).toBe(false);

    const pendingUnload = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(pendingUnload);
    expect(pendingUnload.defaultPrevented).toBe(true);
    sidebarNavButton('projects').click();
    flushSync();
    resolveUpdate();
    await waitForCondition(() => {
      expect(isCurrent('projects')).toBe(true);
    });
    const savedUnload = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(savedUnload);
    expect(savedUnload.defaultPrevented).toBe(false);
  });

  it('saves an Agent tool change before leaving the Agents tab', async () => {
    let resolveAgentUpdate;
    rpcMock.mockImplementation(
      agentsAppRpc({
        agents: [baseAgent(), { ...baseAgent(), id: 'bravo', name: 'Bravo' }],
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
      }),
    );
    mountApp();

    const writeToggle = () =>
      document.querySelector(
        '[data-tool-name="write"][data-tool-access-toggle]',
      );
    sidebarNavButton('agents').click();
    await waitForCondition(() => {
      expect(writeToggle()).toBeTruthy();
    });
    writeToggle().click();
    flushSync();
    sidebarNavButton('chat').click();

    await waitForCondition(() => {
      expect(rpcMock).toHaveBeenCalledWith('agent.update', {
        id: 'alpha',
        tool_access: { mode: 'all', denied: ['write'] },
      });
    });
    expect(isCurrent('agents')).toBe(true);

    resolveAgentUpdate();
    await waitForCondition(() => {
      expect(isCurrent('chat')).toBe(true);
    });
  });

  it.each([
    ['common.retry', 1, 2],
    ['autosave.discardAndContinue', Infinity, 1],
  ])(
    'leaves an editor after a failed transition save through %s',
    async (actionKey, failures, saves) => {
      rpcMock.mockImplementation(failingSettingsRpc(failures));
      mountApp();
      await openSettingsTools();
      typeInto(depthInput(), '5');
      sidebarNavButton('logs').click();

      await waitForCondition(() => {
        expect(document.querySelector(TRANSITION_FAILURE_DIALOG)).toBeTruthy();
      });
      expect(isCurrent('settings')).toBe(true);

      buttonWithText(
        `${TRANSITION_FAILURE_DIALOG} button`,
        t(actionKey),
      ).click();

      await waitForCondition(() => {
        expect(logsShown()).toBe(true);
        expect(document.querySelector(TRANSITION_FAILURE_DIALOG)).toBeFalsy();
      });
      expect(settingsUpdates()).toHaveLength(saves);
    },
  );

  it('offers to leave while a transition save still runs, then stops waiting for that save', async () => {
    const settingsRpc = createSettingsRpcMock();
    rpcMock.mockImplementation((method, params) =>
      method === 'settings.update'
        ? new Promise(() => {})
        : settingsRpc(method, params),
    );
    mountApp();
    await openSettingsTools();
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    typeInto(depthInput(), '5');
    const sectionShown = (id) =>
      document.querySelector(`[data-settings-section="${id}"]`)?.hidden ===
      false;

    settingsPanelButton('general').click();
    await vi.advanceTimersByTimeAsync(AUTOSAVE_STILL_SAVING_MS - 1);
    flushSync();
    expect(settingsUpdates()).toHaveLength(1);
    expect(document.querySelector(STILL_SAVING_DIALOG)).toBeNull();
    await vi.advanceTimersByTimeAsync(1);
    flushSync();
    expect(document.querySelector(STILL_SAVING_DIALOG)).toBeTruthy();

    // Keep waiting closes the prompt; the next navigation asks again at once
    // and becomes the one that runs.
    buttonWithText(
      `${STILL_SAVING_DIALOG} button`,
      t('autosave.keepWaiting'),
    ).click();
    flushSync();
    expect(document.querySelector(STILL_SAVING_DIALOG)).toBeNull();
    settingsPanelButton('memory').click();
    flushSync();
    expect(sectionShown('subagents')).toBe(true);

    buttonWithText(
      `${STILL_SAVING_DIALOG} button`,
      t('autosave.leaveAnyway'),
    ).click();
    flushSync();
    expect(document.querySelector(STILL_SAVING_DIALOG)).toBeNull();
    expect(sectionShown('recall')).toBe(true);

    // The editor stays mounted, but its left-behind save holds nothing.
    settingsPanelButton('system').click();
    flushSync();
    expect(sectionShown('server')).toBe(true);
    const unload = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(false);
    expect(settingsUpdates()).toHaveLength(1);
  });

  it('never locks on a failed transition save: closing stays, Discard works while Retry runs', async () => {
    const settingsRpc = createSettingsRpcMock();
    rpcMock.mockImplementation((method, params) => {
      if (method !== 'settings.update') return settingsRpc(method, params);
      return settingsUpdates().length <= 2
        ? Promise.reject(new Error('save unavailable'))
        : new Promise(() => {});
    });
    mountApp();
    await openSettingsTools();
    await waitForCondition(() =>
      expect(window.location.hash).toBe('#settings/tools'),
    );
    typeInto(depthInput(), '5');

    // Closing the prompt keeps the edits and returns the pending Back.
    window.history.back();
    await waitForCondition(() =>
      expect(document.querySelector(TRANSITION_FAILURE_DIALOG)).toBeTruthy(),
    );
    document.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
    );
    flushSync();
    await waitForCondition(() => {
      expect(document.querySelector(TRANSITION_FAILURE_DIALOG)).toBeNull();
      expect(window.location.hash).toBe('#settings/tools');
    });
    expect(depthInput().value).toBe('5');

    // A Retry that hangs still leaves Discard and continue available.
    sidebarNavButton('logs').click();
    await waitForCondition(() =>
      expect(document.querySelector(TRANSITION_FAILURE_DIALOG)).toBeTruthy(),
    );
    buttonWithText(
      `${TRANSITION_FAILURE_DIALOG} button`,
      t('common.retry'),
    ).click();
    flushSync();
    expect(
      buttonWithText(`${TRANSITION_FAILURE_DIALOG} button`, t('common.saving'))
        .disabled,
    ).toBe(true);
    const discard = buttonWithText(
      `${TRANSITION_FAILURE_DIALOG} button`,
      t('autosave.discardAndContinue'),
    );
    expect(discard.disabled).toBe(false);
    discard.click();
    await waitForCondition(() => {
      expect(logsShown()).toBe(true);
      expect(document.querySelector(TRANSITION_FAILURE_DIALOG)).toBeNull();
    });
    expect(settingsUpdates()).toHaveLength(3);
  });

  it('returns from a sub-agent Session to its parent and opens the same child again', async () => {
    rpcMock.mockImplementation(
      createSubAgentNavigationRpcMock([
        { id: 'alpha', name: 'Alpha', current_session_id: 'session-parent' },
      ]),
    );
    mountApp();
    const parentShown = () => {
      expect(document.body.textContent).toContain('Inspect again');
      // The child's response also surfaces in the parent tool block, so only
      // the missing return notice tells the parent Session is displayed.
      expect(returnToCurrentSessionButton()).toBeFalsy();
    };

    await openSubAgentSession();
    returnToCurrentSessionButton().click();
    flushSync();
    await waitForCondition(parentShown);

    viewSessionButton().click();
    flushSync();
    await waitForCondition(() => {
      // Two navigation loads (limit 100); the deduplicated result fetch for
      // the parent tool block (limit 20) is not one.
      expect(
        rpcMock.mock.calls.filter(
          ([method, params]) =>
            method === 'chat.history' &&
            params?.session_id === 'sub-session-repeat' &&
            params?.limit === 100,
        ),
      ).toHaveLength(2);
      expect(returnToCurrentSessionButton()).toBeTruthy();
    });

    window.history.back();
    await waitForCondition(parentShown);
  });

  it('restores the selected Agent together with the Session override on browser back', async () => {
    rpcMock.mockImplementation(
      createSubAgentNavigationRpcMock([
        { id: 'alpha', name: 'Alpha', current_session_id: 'session-parent' },
        { id: 'beta', name: 'Beta', current_session_id: 'session-beta' },
      ]),
    );
    mountApp();
    await openSubAgentSession();

    // Selecting Beta clears the override in a new history entry.
    await selectPersonalAgent('Beta');
    await waitForCondition(() => {
      expect(selectedPersonalAgentName()).toBe('Beta');
      expect(document.body.textContent).not.toContain('Sub-agent response');
    });

    window.history.back();

    // Back restores the entry's whole Chat context: the sub-agent Session and
    // the Agent it belongs to.
    await waitForCondition(() => {
      expect(document.body.textContent).toContain('Sub-agent response');
      expect(selectedPersonalAgentName()).toBe('Alpha');
    });
  });

  it('keeps display and history entry in sync after tab-away, tab-back and a double back', async () => {
    rpcMock.mockImplementation(
      createSubAgentNavigationRpcMock([
        { id: 'alpha', name: 'Alpha', current_session_id: 'session-parent' },
      ]),
    );
    mountApp();
    await openSubAgentSession();

    // Tab away and back: the Main menu returns to Chat's last place, the
    // child Session the persistent Chat owner still shows.
    sidebarNavButton('logs').click();
    flushSync();
    await waitForCondition(() => {
      expect(window.location.hash).toMatch(/^#logs(\/|$)/);
    });
    sidebarNavButton('chat').click();
    flushSync();
    await waitForCondition(() => {
      expect(window.location.hash).toBe('#chat/alpha/sub-session-repeat');
      expect(document.body.textContent).toContain('Sub-agent response');
    });

    window.history.back();
    await waitForCondition(() => {
      expect(window.location.hash).toMatch(/^#logs(\/|$)/);
    });

    // The second back pops the earlier Chat entry with the child Session:
    // restoring it must not replace that entry with a place without it.
    window.history.back();
    await waitForCondition(() => {
      expect(window.location.hash).toBe('#chat/alpha/sub-session-repeat');
      expect(document.body.textContent).toContain('Sub-agent response');
      expect(window.history.state?.extra?.subAgent).toBe(true);
    });
  });
});
