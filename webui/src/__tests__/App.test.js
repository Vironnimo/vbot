// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount } from 'svelte';

import { englishCatalog, t } from '../lib/i18n.js';
import {
  App,
  assistantHistory,
  NAVIGATION_ITEMS,
  buttonWithText,
  cleanupAppHarness,
  createAppRpcMock,
  createOnboardingRpcMock,
  createRunningSubAgentRpcMock,
  createSettingsRpcMock,
  debugEnabledToggle,
  debugStatusMock,
  getServedWebuiBuildMock,
  resetAppHarness,
  rpcMock,
  runServerEvent,
  selectedAgentName,
  sendConnectionReady,
  settingsPanelButton,
  sidebarNavButton,
  subscribeRunEventsMock,
  subscribeServerEventsMock,
  waitForCondition,
} from './App.support.js';

vi.mock('svelte', async () => {
  return import('../../node_modules/svelte/src/index-client.js');
});

const OUTDATED_NOTICE = '[data-toast-id="webui-outdated"]';
const UPDATE_NOTICE = '[data-toast-id="application-update"]';

const ALPHA = [
  { id: 'alpha', name: 'Alpha', current_session_id: 'session-parent' },
];

describe('NAVIGATION_ITEMS', () => {
  it('lists the navigation views grouped by section', () => {
    expect(NAVIGATION_ITEMS.map(({ id, section }) => [id, section])).toEqual([
      ['chat', 'main'],
      ['terminals', 'main'],
      ['agents', 'agents'],
      ['skills', 'agents'],
      ['system-prompt', 'agents'],
      ['projects', 'agents'],
      ['cron', 'planning'],
      ['calendar', 'planning'],
      ['statistics', 'system'],
      ['logs', 'system'],
      ['settings', 'settings'],
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

  it('replaces keyed Extension toasts, opens their detail route and refreshes navigation badges', async () => {
    let badge = 3;
    const route = 'extension:fixture:main';
    const pageButton = () =>
      Array.from(
        document.querySelectorAll(
          'nav.app-shell__navigation .app-shell__nav-item',
        ),
      ).find((button) => button.textContent.includes('Fixture page'));
    rpcMock.mockImplementation(
      createAppRpcMock({
        methods: {
          'extensions.pages': () => ({
            pages: [
              {
                extension: 'fixture',
                page: 'main',
                title: 'Fixture page',
                icon: 'workflow',
                route,
                entry_url: '/api/files/fixture',
                epoch: 'epoch-a',
                badge,
              },
            ],
          }),
        },
      }),
    );
    const connection = mountApp();
    connection.onEvent({ type: 'connection_ready', active_runs: [] });
    await waitForCondition(() =>
      expect(pageButton()?.textContent).toContain('3'),
    );
    const payload = {
      extension: 'fixture',
      page_id: 'main',
      route: '/card/card one',
      title: 'Old card',
      body: 'Review required',
      key: 'one',
    };
    connection.onEvent({ type: 'extension_notification', payload });
    connection.onEvent({
      type: 'extension_notification',
      payload: { ...payload, title: 'Latest card' },
    });
    flushSync();
    expect(document.querySelectorAll('.toast')).toHaveLength(1);
    expect(document.querySelector('.toast').textContent).toContain(
      'Latest card',
    );
    expect(document.querySelector('.toast').textContent).not.toContain(
      'Old card',
    );
    buttonWithText('.toast button', t('extensions.page.open')).click();
    await waitForCondition(() =>
      expect(window.location.hash).toBe(`#${route}/card/card%20one`),
    );
    expect(document.querySelectorAll('.toast')).toHaveLength(0);
    const frame = document.querySelector('iframe');
    const child = frame.contentWindow;
    const sent = vi.spyOn(child, 'postMessage');
    frame.dispatchEvent(new Event('load'));
    const init = sent.mock.calls.find(
      ([value]) => value.type === 'vbot.extension.init',
    )[0];
    const ready = new MessageEvent('message', {
      data: { ...init, type: 'vbot.extension.ready' },
    });
    Object.defineProperties(ready, {
      source: { value: child },
      origin: { value: 'null' },
    });
    window.dispatchEvent(ready);
    flushSync();
    sent.mockClear();
    badge = 125;
    connection.onEvent({
      type: 'resource_changed',
      payload: {
        kind: 'extensions',
        scope: {
          owner: 'fixture',
          resource: 'page_badges',
          ids: ['main'],
          revision: 1,
        },
      },
    });
    await waitForCondition(() =>
      expect(pageButton()?.textContent).toContain('99+'),
    );
    expect(document.querySelector('iframe')).toBe(frame);
    expect(pageButton().getAttribute('aria-label')).toContain('125');
    expect(
      sent.mock.calls.filter(
        ([value]) => value.type === 'vbot.extension.invalidate',
      ),
    ).toEqual([]);
    badge = 0;
    connection.onEvent({ type: 'connection_ready', active_runs: [] });
    await waitForCondition(() =>
      expect(pageButton()?.textContent).not.toContain('99+'),
    );
  });

  it('gives every navigation view its own icon', async () => {
    mountApp();
    await waitForCondition(() =>
      expect(sidebarNavButton('settings')).toBeTruthy(),
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

  it('keeps a dismissed onboarding hidden while Chat offers the Provider connection', async () => {
    rpcMock.mockImplementation(
      createOnboardingRpcMock({ connected: false, status: 'skipped' }),
    );
    mountApp();

    await waitForCondition(() => {
      expect(document.body.textContent).toContain(t('chat.noProvider.title'));
    });
    expect(document.querySelector('.onboarding-view')).toBeFalsy();
  });

  it('keeps shared completion after a Provider becomes unavailable', async () => {
    rpcMock.mockImplementation(
      createOnboardingRpcMock({ connected: true, status: 'completed' }),
    );
    const handlers = mountApp();
    await waitForCondition(() =>
      expect(document.querySelector('.chat-view')).toBeTruthy(),
    );
    rpcMock.mockImplementation(
      createOnboardingRpcMock({ connected: false, status: 'completed' }),
    );
    handlers.onEvent({
      type: 'resource_changed',
      sequence: 1,
      payload: { kind: 'providers' },
    });
    await waitForCondition(() =>
      expect(document.body.textContent).toContain(t('chat.noProvider.title')),
    );
    expect(document.querySelector('.onboarding-view')).toBeNull();
  });

  it('persists skipping at the first step and keeps the guide visible when that write fails', async () => {
    const read = createOnboardingRpcMock({ connected: false });
    let fail = true;
    rpcMock.mockImplementation((method, params) =>
      method === 'setup.finish' && fail
        ? Promise.reject(new Error('write-failure-sentinel'))
        : read(method, params),
    );
    mountApp();
    await waitForCondition(() =>
      expect(document.querySelector('.onboarding-view')).toBeTruthy(),
    );
    const finish = () =>
      [...document.querySelectorAll('button')].find((button) =>
        button.textContent.includes(t('onboarding.dismiss')),
      );
    finish().click();
    await waitForCondition(() =>
      expect(document.querySelector('[role="alert"]')?.textContent).toContain(
        'write-failure-sentinel',
      ),
    );
    expect(document.querySelector('.onboarding-view')).toBeTruthy();
    fail = false;
    finish().click();
    await waitForCondition(() =>
      expect(document.querySelector('.onboarding-view')).toBeNull(),
    );
    expect(rpcMock).toHaveBeenCalledWith('setup.finish');
  });

  it('opens an explicit setup link even after completion and consumes the link', async () => {
    rpcMock.mockImplementation(createOnboardingRpcMock({ connected: true }));
    window.history.replaceState(null, '', '/?setup=1');
    mountApp();
    await waitForCondition(() =>
      expect(document.querySelector('.onboarding-view')).toBeTruthy(),
    );
    expect(new URLSearchParams(window.location.search).has('setup')).toBe(
      false,
    );
    expect(
      rpcMock.mock.calls.some(([method]) => method === 'setup.finish'),
    ).toBe(false);
  });

  it.each([false, true])(
    'keeps Debug in Settings when toggling Debug Mode from %s',
    async (initiallyEnabled) => {
      rpcMock.mockImplementation(
        createSettingsRpcMock({ initialDebugEnabled: initiallyEnabled }),
      );
      debugStatusMock.mockResolvedValue({ enabled: initiallyEnabled });
      const debugVisible = () => Boolean(sidebarNavButton('debug'));
      mountApp();
      const firstMount = mountedComponent;

      await waitForCondition(() => {
        expect(sidebarNavButton('settings')).toBeTruthy();
        expect(debugVisible()).toBe(false);
      });
      sidebarNavButton('settings').click();
      flushSync();
      await waitForCondition(() => {
        expect(settingsPanelButton('debug')).toBeTruthy();
      });
      settingsPanelButton('debug').click();
      flushSync();
      await waitForCondition(() => {
        expect(debugEnabledToggle().getAttribute('aria-checked')).toBe(
          String(initiallyEnabled),
        );
      });

      debugEnabledToggle().click();
      flushSync();
      document
        .querySelector(
          '[data-settings-section="debug_capture"] .save-status button',
        )
        .click();

      await waitForCondition(() => {
        const updates = rpcMock.mock.calls.filter(
          ([method]) => method === 'settings.update',
        );
        expect(updates.at(-1)?.[1]?.debug?.enabled).toBe(!initiallyEnabled);
        expect(debugVisible()).toBe(false);
        expect(window.location.hash).toBe('#settings/debug');
        expect(
          document.querySelectorAll('#settings-debug-trace-limit'),
        ).toHaveLength(1);
      });
      expect(mountedComponent).toBe(firstMount);
    },
  );

  it('opens Debug traces under Settings and forwards trace invalidations there', async () => {
    const backend = createSettingsRpcMock({ initialDebugEnabled: true });
    const trace = {
      trace_id: 'test-trace',
      model_id: 'test-model',
      provider_id: 'openai',
      request: { body: '{}' },
      response: { body: '{}' },
    };
    let traces = [trace];
    rpcMock.mockImplementation((method, params) => {
      if (method === 'debug.trace_list') return { traces };
      if (method === 'debug.trace_get') return { trace };
      return backend(method, params);
    });
    window.history.replaceState({}, '', '#settings/debug/test-trace');
    localStorage.setItem('vbot.onboardingDismissed', '1');
    const handlers = mountApp();
    await waitForCondition(() => {
      expect(document.querySelector('.detail-header h3')?.textContent).toBe(
        'test-model',
      );
    });
    expect(sidebarNavButton('debug')).toBeFalsy();
    document.querySelector('.detail-back button').click();
    await waitForCondition(() =>
      expect(window.location.hash).toBe('#settings/debug'),
    );
    expect(
      document.querySelector('[data-settings-section="debug_capture"]').hidden,
    ).toBe(false);
    document.querySelector('.debug-trace__row').click();
    await waitForCondition(() =>
      expect(window.location.hash).toBe('#settings/debug/test-trace'),
    );

    traces = [];
    handlers.onEvent({
      type: 'resource_changed',
      sequence: 1,
      payload: { kind: 'debug_traces' },
    });
    await waitForCondition(() => {
      expect(document.querySelector('.detail-header h3')).toBeNull();
      expect(window.location.hash).toBe('#settings/debug');
    });
  });

  it('re-fetches the Agent roster on resource_changed(agents)', async () => {
    rpcMock.mockImplementation(createAppRpcMock({ agents: ALPHA }));
    const handlers = mountApp();
    await waitForCondition(() => {
      expect(selectedAgentName()).toBe('Alpha');
    });
    const agentReads = () =>
      rpcMock.mock.calls.filter(([method]) => method === 'agent.list').length;
    const readsBefore = agentReads();
    // Chat and the app shell each read the roster once when they mount.
    expect(readsBefore).toBe(2);

    // The event carries no Agent data.
    await handlers.onEvent({
      type: 'resource_changed',
      sequence: 1,
      payload: { kind: 'agents' },
    });

    expect(agentReads()).toBeGreaterThan(readsBefore);
  });

  it('refreshes schedule activity for Session changes only while jobs exist', async () => {
    vi.useFakeTimers();
    let jobs = {};
    rpcMock.mockImplementation(
      createAppRpcMock({
        methods: {
          'cron.activity': () => ({ jobs, notification_count: 0 }),
        },
      }),
    );
    const handlers = mountApp();
    const count = () =>
      rpcMock.mock.calls.filter(([method]) => method === 'cron.activity')
        .length;
    const advance = async () => {
      flushSync();
      await vi.advanceTimersByTimeAsync(101);
      flushSync();
    };
    const change = (kind) =>
      handlers.onEvent({ type: 'resource_changed', payload: { kind } });
    await advance();
    expect(count()).toBe(1);
    await change('sessions');
    await advance();
    expect(count()).toBe(1);
    jobs = { scheduled: { notification_count: 0 } };
    await change('cron');
    await advance();
    await change('sessions');
    await advance();
    expect(count()).toBe(3);
  });

  it('re-reads Extension input requests and Computer Use status only on their changes', async () => {
    let pagesUnavailable = false;
    let declarations;
    rpcMock.mockImplementation(
      createAppRpcMock({
        agents: ALPHA,
        methods: {
          'extensions.pages': () => {
            if (pagesUnavailable) throw new Error('test-owned catalog failure');
            return { pages: [] };
          },
          'extensions.requests': () => ({ requests: [] }),
          'extensions.contributions': () =>
            declarations?.promise ?? {
              contributions: [
                {
                  id: 'extension:computer_use:control',
                  local_id: 'control',
                  extension: 'computer_use',
                  epoch: 'fixture-epoch',
                  surface: 'chat.composer.actions',
                  kind: 'status',
                  title: 'Computer Use',
                  source: { operation: 'read', resource: 'control' },
                  operations: ['read'],
                },
              ],
            },
          'extensions.contribution_sources': ({ requests }) => ({
            results: requests.map(() => ({ ok: true, result: { items: [] } })),
          }),
        },
      }),
    );
    const handlers = mountApp();
    sendConnectionReady();
    const calls = (method) =>
      rpcMock.mock.calls.filter(([name]) => name === method).length;
    const counts = () => [
      calls('extensions.requests'),
      calls('extensions.contribution_sources'),
    ];
    await waitForCondition(() => {
      expect(calls('extensions.pages')).toBe(1);
      expect(calls('extensions.contribution_sources')).toBeGreaterThan(0);
    });
    // Startup reads end; nothing polls afterwards.
    let settled;
    await waitForCondition(() => {
      const current = counts().join();
      const stable = current === settled;
      settled = current;
      expect(stable).toBe(true);
    });
    // Each surface read once when it mounted; the first page catalog asks
    // none of them to read again.
    expect([...counts(), calls('extensions.contributions')]).toEqual([1, 1, 1]);
    const [requests, initialStatus] = counts();
    let status = initialStatus;
    const extensionsChanged = (scope) =>
      handlers.onEvent({
        type: 'resource_changed',
        sequence: 1,
        payload: { kind: 'extensions', ...(scope && { scope }) },
      });
    const change = (owner, resource) =>
      extensionsChanged({ owner, resource, ids: ['record'], revision: 1 });

    await handlers.onEvent({
      type: 'extension_resource_changed',
      payload: {
        owner: 'unrelated',
        epoch: 'fixture-epoch',
        resource: 'extension:unrelated:control',
        local_resource: 'control',
        revision: 1,
        services: [],
      },
    });
    expect(counts()).toEqual([requests, status]);
    expect(calls('extensions.pages')).toBe(1);
    expect(calls('extensions.contributions')).toBe(1);
    await change('computer_use', 'page_badges');
    await waitForCondition(() => expect(calls('extensions.pages')).toBe(2));
    expect(calls('extensions.contributions')).toBe(1);
    await handlers.onEvent({
      type: 'extension_resource_changed',
      payload: {
        owner: 'computer_use',
        epoch: 'fixture-epoch',
        resource: 'extension:computer_use:control',
        local_resource: 'control',
        revision: 2,
        services: [],
      },
    });
    await waitForCondition(() =>
      expect(counts()).toEqual([requests, status + 1]),
    );
    status++;
    expect(calls('extensions.pages')).toBe(2);
    expect(calls('extensions.contributions')).toBe(1);

    await change('swarm', 'swarms');
    await change('mcp', 'pending_inputs');
    await waitForCondition(() =>
      expect(counts()).toEqual([requests + 1, status]),
    );
    await change('computer_use', 'control');
    await waitForCondition(() =>
      expect(counts()).toEqual([requests + 1, status + 1]),
    );
    // An Extension reload (or reconnect) can change both.
    await extensionsChanged(null);
    await waitForCondition(() => {
      expect(counts()[0]).toBe(requests + 2);
      expect(counts()[1]).toBeGreaterThan(status + 1);
    });
    const refreshedStatus = counts()[1];
    // A transient page-catalog failure cannot suppress unrelated surfaces'
    // catch-up reads; those keep using their own Extension operations.
    pagesUnavailable = true;
    declarations = Promise.withResolvers();
    const reload = extensionsChanged(null);
    try {
      await waitForCondition(() => {
        expect(counts()[0]).toBe(requests + 3);
        expect(counts()[1]).toBeGreaterThan(refreshedStatus);
      });
    } finally {
      declarations.resolve({ contributions: [] });
      await reload;
    }
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

  it('asks to reload once the server serves a newer WebUI build', async () => {
    const meta = document.createElement('meta');
    meta.name = 'vbot-webui-build';
    meta.content = 'build-loaded';
    document.head.append(meta);
    try {
      const connection = mountApp();
      getServedWebuiBuildMock.mockResolvedValue('build-loaded');
      connection.onEvent({ type: 'connection_ready', active_runs: [] });
      await Promise.resolve();
      flushSync();
      expect(document.querySelector(OUTDATED_NOTICE)).toBeNull();

      getServedWebuiBuildMock.mockResolvedValue('build-served');
      connection.onEvent({
        type: 'connection_ready',
        replay_status: 'epoch_changed',
        active_runs: [],
      });
      await waitForCondition(() =>
        expect(
          buttonWithText(
            `${OUTDATED_NOTICE} button`,
            t('app.webuiOutdatedReload'),
          ),
        ).toBeTruthy(),
      );

      // Closed, it stays away until the next connection.
      document.querySelector(`${OUTDATED_NOTICE} .toast-close`).click();
      flushSync();
      expect(document.querySelector(OUTDATED_NOTICE)).toBeNull();
      connection.onEvent({ type: 'connection_ready', active_runs: [] });
      await waitForCondition(() =>
        expect(document.querySelector(OUTDATED_NOTICE)).toBeTruthy(),
      );
    } finally {
      meta.remove();
    }
  });

  it('holds a recovered data store in a Dialog until it is acknowledged', async () => {
    const incident = {
      incident_id: 'incident-1',
      database: 'sessions',
      restored_snapshot_id: 'snapshot-1',
      possible_loss_interval: { start: 'start', end: 'end' },
    };
    const acknowledged = vi.fn(() => ({ incidents: [] }));
    rpcMock.mockImplementation(
      createAppRpcMock({
        methods: {
          'data_store.status': () => ({ incidents: [incident] }),
          'data_store.incident_acknowledge': acknowledged,
        },
      }),
    );
    mountApp().onEvent({ type: 'connection_ready', active_runs: [] });

    const dialog = () => document.querySelector('[role="dialog"]');
    await waitForCondition(() =>
      expect(dialog()?.textContent).toContain(t('dataStore.recoveredTitle')),
    );
    expect(dialog().textContent).toContain('sessions');
    expect(dialog().querySelector('[aria-label]')?.disabled).toBe(true);

    buttonWithText(
      '[role="dialog"] button',
      t('dataStore.acknowledge'),
    ).click();
    await waitForCondition(() => expect(dialog()).toBeNull());
    expect(acknowledged).toHaveBeenCalledOnce();
  });

  it('names a newer vBot version until its notice is dismissed', async () => {
    const notes = ['0.4.36', '0.4.35'].map((version) => ({
      version,
      date: '2026-11-20',
      before_update: '- Backup ' + version,
      markdown:
        '## Before you update\n- Backup ' +
        version +
        '\n## Fixes\n- Fixed ' +
        version,
    }));
    const available = {
      version_id: 'rel_new',
      version: '0.4.36',
      label: '0.4.36',
    };
    rpcMock.mockImplementation(
      createAppRpcMock({
        methods: {
          'application.update_status': () => ({
            installed: true,
            available,
            channel: 'release',
            notes,
          }),
        },
      }),
    );
    const connection = mountApp();
    connection.onEvent({ type: 'connection_ready', active_runs: [] });

    const notice = t('app.updateAvailable', { version: '0.4.36' });
    await waitForCondition(() =>
      expect(document.querySelector(UPDATE_NOTICE)?.textContent).toContain(
        notice,
      ),
    );
    document.querySelector(`${UPDATE_NOTICE} .toast-actions button`).click();
    await waitForCondition(() =>
      expect(document.querySelector('[role=dialog]')?.textContent).toContain(
        'Fixed 0.4.35',
      ),
    );
    const dialog = document.querySelector('[role=dialog]');
    const content = dialog.textContent;
    expect(content.indexOf('Backup 0.4.36')).toBeLessThan(
      content.indexOf('Backup 0.4.35'),
    );
    expect(content.indexOf('Backup 0.4.35')).toBeLessThan(
      content.indexOf('Fixed 0.4.36'),
    );
    expect(content.match(/Backup 0.4.36/g)).toHaveLength(1);
    expect(content.indexOf('Fixed 0.4.36')).toBeLessThan(
      content.indexOf('Fixed 0.4.35'),
    );
    dialog.querySelector('button').click();
    document.querySelector(`${UPDATE_NOTICE} .toast-close`).click();
    flushSync();
    expect(document.querySelector(UPDATE_NOTICE)).toBeNull();

    // The dismissal holds for this version; it is stored in this browser.
    connection.onEvent({ type: 'connection_ready', active_runs: [] });
    await Promise.resolve();
    flushSync();
    expect(document.querySelector(UPDATE_NOTICE)).toBeNull();
  });

  it('shows bundled changes once after a version change and stays quiet on first visit', async () => {
    const entries = ['1.0.9', '1.0.10', '1.0.11'].map((version) => ({
      version,
      date: '2026-11-20',
      before_update: null,
      markdown: '## Highlights\nRelease ' + version,
    }));
    rpcMock.mockImplementation(
      createAppRpcMock({
        methods: {
          'application.changelog': () => ({
            releases: entries.slice().reverse(),
          }),
        },
      }),
    );
    const connection = mountApp();
    const hello = (version) =>
      connection.onEvent({
        type: 'connection_ready',
        active_runs: [],
        server: { version },
      });
    const notice = () =>
      document.querySelector('[data-toast-id="application-updated"]');
    hello('1.0.9');
    await waitForCondition(() =>
      expect(localStorage.getItem('vbot.lastSeenRelease')).toBe('1.0.9'),
    );
    expect(notice()).toBeNull();
    hello('1.0.11');
    await waitForCondition(() =>
      expect(notice()?.textContent).toContain('1.0.11'),
    );
    notice().querySelector('.toast-actions button').click();
    await waitForCondition(() =>
      expect(document.querySelector('[role=dialog]')?.textContent).toContain(
        'Release 1.0.10',
      ),
    );
    expect(document.querySelector('[role=dialog]').textContent).not.toContain(
      'Release 1.0.9',
    );
    notice().querySelector('.toast-close').click();
    hello('1.0.11');
    await Promise.resolve();
    flushSync();
    expect(notice()).toBeNull();
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
    await vi.dynamicImportSettled();
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

  it('passes live context and targeted Resource changes to an Extension page', async () => {
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
                services: ['extension:provider:read'],
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
    const handlers = mountApp();
    sendConnectionReady();

    await waitForCondition(() => {
      expect(sidebarNavButton('Fixture page')).toBeTruthy();
    });
    sidebarNavButton('Fixture page').click();
    flushSync();
    await vi.dynamicImportSettled();
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
    const reads = () =>
      rpcMock.mock.calls.filter(([method]) =>
        [
          'extensions.pages',
          'extensions.contributions',
          'extensions.requests',
        ].includes(method),
      ).length;
    const readsBefore = reads();
    await handlers.onEvent({
      type: 'extension_resource_changed',
      payload: {
        owner: 'provider',
        epoch: 'fixture-epoch',
        resource: 'extension:provider:items',
        local_resource: 'items',
        revision: 7,
        services: ['extension:provider:read'],
      },
    });
    expect(sent).toHaveBeenCalledWith(
      expect.objectContaining({
        type: 'vbot.extension.invalidate',
        change: { resource: 'extension:provider:items', ids: [], revision: 7 },
      }),
      '*',
    );
    expect(reads()).toBe(readsBefore);
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

  it('keeps a rapidly completed Run output across its timeline and History handoff', async () => {
    const finalHistory = assistantHistory(
      'run-follow-up',
      'assistant-output-sentinel',
    );
    let settled = false;
    await mountChat(
      createAppRpcMock({
        agents: ALPHA,
        history: () => (settled ? finalHistory : {}),
      }),
    );
    await waitForCondition(() => {
      expect(selectedAgentName()).toBe('Alpha');
    });

    // App-wide lifecycle events can finish before the Timeline snapshot arrives.
    // The completed Run is already persisted when its terminal event is sent.
    settled = true;
    await Promise.all([
      handlers.onEvent(
        runServerEvent('run_started', 'run-follow-up', 1, {
          run_event_type: 'run_started',
          status: 'running',
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
    await waitForCondition(() =>
      expect(subscribeRunEventsMock).toHaveBeenCalledWith(
        '/api/runs/run-follow-up/timeline?child_limit=100',
        expect.any(Object),
        {
          eventTypes: ['timeline_snapshot', 'timeline_update'],
          closeOnTerminal: false,
        },
      ),
    );
    const [, stream] = subscribeRunEventsMock.mock.calls.find(
      ([url]) => url === '/api/runs/run-follow-up/timeline?child_limit=100',
    );
    stream.onEvent({
      type: 'timeline_snapshot',
      data: {
        run_id: 'run-follow-up',
        sequence: 3,
        items: finalHistory.items,
        state: { status: 'completed' },
        terminal: true,
      },
    });
    flushSync();

    await waitForCondition(() => {
      expect(document.body.textContent).toContain('assistant-output-sentinel');
    });
    expect(subscribeRunEventsMock).toHaveBeenCalledWith(
      '/api/runs/run-follow-up/timeline?child_limit=100',
      expect.any(Object),
      {
        eventTypes: ['timeline_snapshot', 'timeline_update'],
        closeOnTerminal: false,
      },
    );
  });

  it('streams only the displayed Session’s active Run from the connection_ready snapshot', async () => {
    await mountChat(createAppRpcMock({ agents: ALPHA }));
    await waitForCondition(() => {
      expect(selectedAgentName()).toBe('Alpha');
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
      '/api/runs/run-snapshot-1/timeline?child_limit=100',
      '/api/runs/run-plain/timeline?child_limit=100',
    ]);
  });

  it('completes a background sub-agent row when its child Run completes', async () => {
    await mountChat(createRunningSubAgentRpcMock(ALPHA));
    await waitForCondition(() => {
      expect(
        document.querySelector('.subagent-tool-event [data-status=running]'),
      ).toBeTruthy();
    });

    await handlers.onEvent(
      runServerEvent('run_completed', 'sub-run-running', 1, {
        session_id: 'sub-session-running',
        run_event_type: 'run_completed',
        timing: { duration_ms: 2400 },
        status: 'completed',
      }),
    );
    flushSync();

    await waitForCondition(() => {
      expect(
        document.querySelector('.subagent-tool-event [data-status=success]'),
      ).toBeTruthy();
      expect(
        document.querySelector('.subagent-tool-event [data-status=running]'),
      ).toBeFalsy();
    });
  });
});
