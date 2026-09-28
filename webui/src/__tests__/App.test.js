// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount } from 'svelte';

import { englishCatalog, t } from '../lib/i18n.js';
import {
  App,
  NAVIGATION_ITEMS,
  buttonWithText,
  cleanupAppHarness,
  createAppRpcMock,
  createOnboardingRpcMock,
  createRunningSubAgentRpcMock,
  createSettingsRpcMock,
  debugEnabledToggle,
  debugStatusMock,
  resetAppHarness,
  rpcMock,
  runServerEvent,
  selectedPersonalAgentName,
  settingsPanelButton,
  sidebarNavButton,
  subscribeRunEventsMock,
  subscribeServerEventsMock,
  waitForCondition,
} from './App.support.js';

vi.mock('svelte', async () => {
  return import('../../node_modules/svelte/src/index-client.js');
});

const ALPHA = [
  { id: 'alpha', name: 'Alpha', current_session_id: 'session-parent' },
];

describe('NAVIGATION_ITEMS', () => {
  it('lists the navigation views grouped by section', () => {
    expect(NAVIGATION_ITEMS.map(({ id, section }) => [id, section])).toEqual([
      ['chat', 'work'],
      ['terminals', 'work'],
      ['agents', 'work'],
      ['projects', 'work'],
      ['calendar', 'work'],
      ['jev', 'work'],
      ['skills', 'configure'],
      ['cron', 'configure'],
      ['system-prompt', 'configure'],
      ['settings', 'configure'],
      ['statistics', 'insights'],
      ['logs', 'insights'],
      ['debug', 'insights'],
    ]);
  });

  it('describes each view only by a translated label', () => {
    for (const item of NAVIGATION_ITEMS) {
      expect(Object.keys(item).sort()).toEqual(['id', 'label', 'section']);
      expect(Object.values(englishCatalog)).toContain(item.label());
    }
  });
});

describe('App', () => {
  let mountedComponent;

  beforeEach(() => {
    resetAppHarness();
    mountedComponent = null;
  });

  afterEach(async () => {
    mountedComponent = await cleanupAppHarness(mountedComponent);
    document.documentElement.removeAttribute('style');
    vi.useRealTimers();
  });

  function mountApp() {
    mountedComponent = mount(App, { target: document.body });
    flushSync();
    return subscribeServerEventsMock.mock.calls[0][0];
  }

  it('gives every navigation view its own icon', async () => {
    debugStatusMock.mockResolvedValue({ enabled: true });
    mountApp();
    await waitForCondition(() =>
      expect(sidebarNavButton('debug')).toBeTruthy(),
    );

    const icons = NAVIGATION_ITEMS.map(
      ({ id }) =>
        sidebarNavButton(id).querySelector('svg.app-shell__nav-icon').innerHTML,
    );
    expect(icons.every((icon) => icon.trim() !== '')).toBe(true);
    expect(new Set(icons).size).toBe(NAVIGATION_ITEMS.length);
  });

  it.each([
    [false, '.onboarding-view', '.chat-view'],
    [true, '.chat-view', '.onboarding-view'],
  ])(
    'with a connected Provider (%s) shows %s instead of %s',
    async (connected, shown, hidden) => {
      rpcMock.mockImplementation(createOnboardingRpcMock({ connected }));
      mountApp();

      await waitForCondition(() => {
        expect(document.querySelector(shown)).toBeTruthy();
      });
      expect(document.querySelector(hidden)).toBeFalsy();
    },
  );

  it('keeps a dismissed onboarding hidden and offers a Finish setup re-entry', async () => {
    localStorage.setItem('vbot.onboardingDismissed', '1');
    rpcMock.mockImplementation(createOnboardingRpcMock({ connected: false }));
    mountApp();

    await waitForCondition(() => {
      expect(document.querySelector('.app-finish-setup')).toBeTruthy();
    });
    expect(document.querySelector('.onboarding-view')).toBeFalsy();

    document.querySelector('.app-finish-setup button').click();
    flushSync();

    await waitForCondition(() => {
      expect(document.querySelector('.onboarding-view')).toBeTruthy();
    });
  });

  it('drops the Finish setup re-entry and its dismissal once a Provider change connects vBot', async () => {
    localStorage.setItem('vbot.onboardingDismissed', '1');
    rpcMock.mockImplementation(createOnboardingRpcMock({ connected: false }));
    const handlers = mountApp();
    await waitForCondition(() => {
      expect(document.querySelector('.app-finish-setup')).toBeTruthy();
    });

    rpcMock.mockImplementation(createOnboardingRpcMock({ connected: true }));
    handlers.onEvent({
      type: 'resource_changed',
      sequence: 1,
      payload: { kind: 'providers' },
    });

    await waitForCondition(() => {
      expect(document.querySelector('.app-finish-setup')).toBeNull();
    });
    expect(localStorage.getItem('vbot.onboardingDismissed')).toBeNull();
  });

  it.each([false, true])(
    'toggles the Debug navigation from Debug Mode %s in Settings without remounting',
    async (initiallyEnabled) => {
      rpcMock.mockImplementation(
        createSettingsRpcMock({ initialDebugEnabled: initiallyEnabled }),
      );
      debugStatusMock.mockResolvedValue({ enabled: initiallyEnabled });
      const debugVisible = () => Boolean(sidebarNavButton('debug'));
      mountApp();
      const firstMount = mountedComponent;

      await waitForCondition(() => {
        expect(debugVisible()).toBe(initiallyEnabled);
      });
      sidebarNavButton('settings').click();
      flushSync();
      await waitForCondition(() => {
        expect(settingsPanelButton('system')).toBeTruthy();
      });
      settingsPanelButton('system').click();
      flushSync();
      await waitForCondition(() => {
        expect(debugEnabledToggle().getAttribute('aria-checked')).toBe(
          String(initiallyEnabled),
        );
      });

      debugEnabledToggle().click();
      flushSync();

      await waitForCondition(() => {
        const updates = rpcMock.mock.calls.filter(
          ([method]) => method === 'settings.update',
        );
        expect(updates.at(-1)?.[1]?.debug?.enabled).toBe(!initiallyEnabled);
        expect(debugVisible()).toBe(!initiallyEnabled);
      });
      expect(mountedComponent).toBe(firstMount);
    },
  );

  it('re-fetches the Agent roster on resource_changed(agents)', async () => {
    rpcMock.mockImplementation(createAppRpcMock({ agents: ALPHA }));
    const handlers = mountApp();
    await waitForCondition(() => {
      expect(selectedPersonalAgentName()).toBe('Alpha');
    });
    const agentReads = () =>
      rpcMock.mock.calls.filter(([method]) => method === 'agent.list').length;
    const readsBefore = agentReads();

    // The event carries no Agent data.
    await handlers.onEvent({
      type: 'resource_changed',
      sequence: 1,
      payload: { kind: 'agents' },
    });

    expect(agentReads()).toBeGreaterThan(readsBefore);
  });

  it('shows app_error events as error Toasts that stay until dismissed', () => {
    vi.useFakeTimers();
    const handlers = mountApp();

    handlers.onEvent({
      type: 'app_error',
      sequence: 1,
      payload: { message: 'app-error-sentinel' },
    });
    flushSync();

    const toast = document.querySelector('.toast.error');
    expect(toast.closest('[aria-live="polite"]')).toBeTruthy();
    expect(toast.textContent).toContain('app-error-sentinel');
    vi.advanceTimersByTime(10000);
    flushSync();
    expect(document.querySelector('.toast.error')).toBe(toast);
  });

  it('shows one global server notice across tabs and refreshes after reconnect', () => {
    vi.useFakeTimers();
    const initialConnection = mountApp();
    expect(document.querySelector('.server-availability-notice')).toBeNull();
    initialConnection.onEvent({
      type: 'app_error',
      sequence: 1,
      payload: { message: 'app-error-sentinel' },
    });
    flushSync();
    const unrelatedErrorToast = document.querySelector('.toast.error');
    expect(unrelatedErrorToast).toBeTruthy();

    initialConnection.onClose();
    flushSync();

    expect(
      document.querySelector('.app-shell')?.dataset.serverUnavailable,
    ).toBe('true');
    expect(document.querySelector('.app-shell__content')?.inert).toBe(true);

    vi.advanceTimersByTime(999);
    flushSync();
    expect(document.querySelector('.server-availability-notice')).toBeNull();

    vi.advanceTimersByTime(1);
    flushSync();
    const offlineNotice = document.querySelector('.server-availability-notice');
    expect(offlineNotice.getAttribute('role')).toBe('alert');
    // The offline notice replaces connection symptoms, not unrelated sticky
    // errors the user has not dismissed.
    expect(document.querySelector('.toast.error')).toBe(unrelatedErrorToast);

    sidebarNavButton('agents').click();
    flushSync();
    expect(sidebarNavButton('agents').getAttribute('aria-current')).toBe(
      'page',
    );
    expect(document.querySelector('.server-availability-notice')).toBe(
      offlineNotice,
    );

    const connectionsBeforeRetry = subscribeServerEventsMock.mock.calls.length;
    buttonWithText(
      '.server-availability-notice button',
      t('status.retryNow'),
    ).click();
    flushSync();
    expect(subscribeServerEventsMock).toHaveBeenCalledTimes(
      connectionsBeforeRetry + 1,
    );

    const agentReads = () =>
      rpcMock.mock.calls.filter(([method]) => method === 'agent.list').length;
    const agentReadsBeforeRecovery = agentReads();
    subscribeServerEventsMock.mock.calls.at(-1)[0].onOpen();
    flushSync();

    const restoredNotice = document.querySelector(
      '.server-availability-notice--restored',
    );
    expect(restoredNotice.getAttribute('role')).toBe('status');
    expect(
      document.querySelector('.app-shell__content')?.inert,
    ).toBeUndefined();
    expect(agentReads()).toBeGreaterThan(agentReadsBeforeRecovery);

    vi.advanceTimersByTime(1400);
    flushSync();
    expect(document.querySelector('.server-availability-notice')).toBeNull();
  });

  it('offers Desktop server switching outside the inert app content', async () => {
    vi.useFakeTimers();
    window.history.replaceState({}, '', '/?accessor=desktop');
    window.pywebview = {
      api: {
        getDesktopCapabilities: vi.fn().mockResolvedValue({
          wakeword: false,
          serverSelection: true,
        }),
        listServers: vi
          .fn()
          .mockResolvedValue([
            { host: 'pi.lan', port: 8420, label: 'Home', active: true },
          ]),
      },
    };
    const connection = mountApp();
    await vi.advanceTimersByTimeAsync(0);
    flushSync();

    connection.onClose();
    await vi.advanceTimersByTimeAsync(1000);
    flushSync();

    buttonWithText(
      '.server-availability-notice button',
      t('status.switchServer'),
    ).click();
    flushSync();
    await vi.advanceTimersByTimeAsync(0);
    flushSync();

    const modal = document.querySelector('[role="dialog"]');
    expect(modal.closest('.app-shell__content')).toBeNull();
    expect(document.querySelector('.server-availability-notice')).toBeNull();
    expect(modal.textContent).toContain('Home');
    expect(
      modal.querySelector('.desktop-server-row .chip.success'),
    ).toBeTruthy();
  });

  it('passes the live shell theme and display context to an Extension page', async () => {
    const rootStyle = document.documentElement.style;
    for (const [name, value] of [
      ['--bg', '#111111'],
      ['--surface', '#222222'],
      ['--surface-2', '#333333'],
      ['--border', '#444444'],
      ['--text-hi', '#eeeeee'],
      ['--text-med', '#999999'],
      ['--accent', '#ff8800'],
    ]) {
      rootStyle.setProperty(name, value);
    }
    rootStyle.colorScheme = 'dark';
    rpcMock.mockImplementation(
      createAppRpcMock({
        methods: {
          'extensions.pages': () => ({
            pages: [
              {
                extension: 'fixture',
                page: 'main',
                title: 'Fixture page',
                route: 'extension:fixture:main',
                entry_url: '/fixture-page.html',
                epoch: 'fixture-epoch',
              },
            ],
          }),
          'settings.get': () => ({
            general: { timezone: 'Europe/Berlin' },
            appearance: { language: 'de', available_languages: ['de'] },
          }),
        },
      }),
    );
    mountApp();

    await waitForCondition(() => {
      expect(sidebarNavButton('Fixture page')).toBeTruthy();
    });
    sidebarNavButton('Fixture page').click();
    flushSync();

    const frame = document.querySelector('iframe');
    const child = frame.contentWindow;
    const sent = vi.spyOn(child, 'postMessage');
    frame.dispatchEvent(new Event('load'));
    const init = sent.mock.calls.find(
      ([message]) => message.type === 'vbot.extension.init',
    )[0];
    expect(init).toMatchObject({
      locale: 'de',
      timezone: 'Europe/Berlin',
      theme: {
        mode: 'dark',
        background: '#111111',
        surface: '#222222',
        elevatedSurface: '#333333',
        border: '#444444',
        text: '#eeeeee',
        mutedText: '#999999',
        accent: '#ff8800',
      },
    });

    const event = new MessageEvent('message', {
      data: {
        type: 'vbot.extension.ready',
        version: 1,
        nonce: init.nonce,
        epoch: init.epoch,
        descriptor: init.descriptor,
      },
    });
    Object.defineProperties(event, {
      origin: { value: 'null' },
      source: { value: child },
    });
    window.dispatchEvent(event);
    rootStyle.setProperty('--accent', '#00cc88');

    await waitForCondition(() => {
      expect(sent).toHaveBeenCalledWith(
        expect.objectContaining({
          type: 'vbot.extension.context',
          theme: expect.objectContaining({ accent: '#00cc88' }),
        }),
        '*',
      );
    });
  });
});

describe('App Run events', () => {
  let mountedComponent;
  let handlers;

  beforeEach(() => {
    resetAppHarness();
    mountedComponent = null;
  });

  afterEach(async () => {
    mountedComponent = await cleanupAppHarness(mountedComponent);
  });

  async function mountChat(rpc) {
    rpcMock.mockImplementation(rpc);
    mountedComponent = mount(App, { target: document.body });
    flushSync();
    [handlers] = subscribeServerEventsMock.mock.calls[0];
  }

  const streamedRuns = () =>
    subscribeRunEventsMock.mock.calls.map(([url]) => url);

  it('keeps the assistant output of rapid Run events and streams the rest', async () => {
    await mountChat(createAppRpcMock({ agents: ALPHA }));
    await waitForCondition(() => {
      expect(selectedPersonalAgentName()).toBe('Alpha');
    });

    await Promise.all([
      handlers.onEvent(
        runServerEvent('run_started', 'run-follow-up', 1, {
          run_event_type: 'run_started',
          status: 'running',
        }),
      ),
      handlers.onEvent(
        runServerEvent('run_output', 'run-follow-up', 2, {
          run_event_type: 'assistant_output',
          output: {
            message: {
              role: 'assistant',
              content: 'assistant-output-sentinel',
            },
          },
        }),
      ),
      handlers.onEvent(
        runServerEvent('run_completed', 'run-follow-up', 3, {
          run_event_type: 'run_completed',
          status: 'completed',
        }),
      ),
    ]);
    flushSync();

    await waitForCondition(() => {
      expect(document.body.textContent).toContain('assistant-output-sentinel');
    });
    expect(subscribeRunEventsMock).toHaveBeenCalledWith(
      '/api/runs/run-follow-up/events',
      expect.any(Object),
      { afterSequence: 1 },
    );
  });

  it('streams only the displayed Session’s active Run from the connection_ready snapshot', async () => {
    await mountChat(createAppRpcMock({ agents: ALPHA }));
    await waitForCondition(() => {
      expect(selectedPersonalAgentName()).toBe('Alpha');
    });
    const streamedBefore = streamedRuns().length;

    handlers.onEvent({
      type: 'connection_ready',
      epoch: 'bus-epoch-7',
      last_sequence: 42,
      active_runs: [
        {
          run_id: 'run-snapshot-1',
          agent_id: 'alpha',
          session_id: 'session-parent',
          status: 'running',
          sse_url: '/api/runs/run-snapshot-1/events',
        },
        {
          run_id: 'run-snapshot-2',
          agent_id: 'alpha',
          session_id: 'session-other',
          status: 'running',
          sse_url: '/api/runs/run-snapshot-2/events',
        },
      ],
    });
    flushSync();
    handlers.onEvent(
      runServerEvent('run_started', 'run-plain', 43, {
        run_event_type: 'run_started',
        status: 'running',
      }),
    );
    flushSync();

    expect(streamedRuns().slice(streamedBefore)).toEqual([
      '/api/runs/run-snapshot-1/events',
      '/api/runs/run-plain/events',
    ]);
  });

  it('completes a background sub-agent row when its child Run completes', async () => {
    await mountChat(createRunningSubAgentRpcMock(ALPHA));
    await waitForCondition(() => {
      expect(
        document.querySelector('.subagent-tool-event .te-dot.running'),
      ).toBeTruthy();
    });

    await handlers.onEvent(
      runServerEvent('run_completed', 'sub-run-running', 1, {
        session_id: 'sub-session-running',
        run_event_type: 'run_completed',
        status: 'completed',
      }),
    );
    flushSync();

    await waitForCondition(() => {
      expect(
        document.querySelector('.subagent-tool-event .te-dot.done'),
      ).toBeTruthy();
      expect(
        document.querySelector('.subagent-tool-event .te-dot.running'),
      ).toBeFalsy();
    });
  });
});
