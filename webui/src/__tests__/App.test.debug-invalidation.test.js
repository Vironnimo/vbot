// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount } from 'svelte';

import {
  activeAgentTab,
  App,
  cleanupAppHarness,
  createChatRpcMock,
  createEmptyChatRpcMock,
  createSettingsRpcMock,
  debugEnabledToggle,
  debugStatusMock,
  resetAppHarness,
  rpcMock,
  settingsPanelButton,
  sidebarNavButton,
  subscribeServerEventsMock,
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

  it('shows the Debug nav after enabling Debug Mode in Settings without remounting', async () => {
    rpcMock.mockImplementation(
      createSettingsRpcMock({ initialDebugEnabled: false }),
    );

    mountedComponent = mount(App, { target: document.body });
    flushSync();

    await waitForCondition(() => {
      expect(sidebarNavButton('Debug')).toBeFalsy();
    });

    sidebarNavButton('Settings')?.click();
    flushSync();

    await waitForCondition(() => {
      expect(settingsPanelButton('Debug')).toBeTruthy();
    });
    settingsPanelButton('Debug')?.click();
    flushSync();

    await waitForCondition(() => {
      const toggle = debugEnabledToggle();
      expect(toggle).toBeTruthy();
      expect(toggle.getAttribute('aria-checked')).toBe('false');
    });

    const currentMount = mountedComponent;
    const settingsUpdateCallsBefore = rpcMock.mock.calls.filter(
      ([method]) => method === 'settings.update',
    ).length;

    debugEnabledToggle()?.click();
    flushSync();

    await waitForCondition(() => {
      const updateCalls = rpcMock.mock.calls.filter(
        ([method]) => method === 'settings.update',
      );
      expect(updateCalls.length).toBeGreaterThan(settingsUpdateCallsBefore);
      const lastCall = updateCalls[updateCalls.length - 1];
      expect(lastCall[1]?.debug?.enabled).toBe(true);
    });

    await waitForCondition(() => {
      expect(sidebarNavButton('Debug')).toBeTruthy();
    });

    expect(mountedComponent).toBe(currentMount);
  });

  it('hides the Debug nav after disabling Debug Mode in Settings without remounting', async () => {
    rpcMock.mockImplementation(
      createSettingsRpcMock({ initialDebugEnabled: true }),
    );
    debugStatusMock.mockResolvedValue({ enabled: true });

    mountedComponent = mount(App, { target: document.body });
    flushSync();

    await waitForCondition(() => {
      expect(sidebarNavButton('Debug')).toBeTruthy();
    });

    sidebarNavButton('Settings')?.click();
    flushSync();

    await waitForCondition(() => {
      expect(settingsPanelButton('Debug')).toBeTruthy();
    });
    settingsPanelButton('Debug')?.click();
    flushSync();

    await waitForCondition(() => {
      const toggle = debugEnabledToggle();
      expect(toggle).toBeTruthy();
      expect(toggle.getAttribute('aria-checked')).toBe('true');
    });

    const currentMount = mountedComponent;
    const settingsUpdateCallsBefore = rpcMock.mock.calls.filter(
      ([method]) => method === 'settings.update',
    ).length;

    debugEnabledToggle()?.click();
    flushSync();

    await waitForCondition(() => {
      const updateCalls = rpcMock.mock.calls.filter(
        ([method]) => method === 'settings.update',
      );
      expect(updateCalls.length).toBeGreaterThan(settingsUpdateCallsBefore);
      const lastCall = updateCalls[updateCalls.length - 1];
      expect(lastCall[1]?.debug?.enabled).toBe(false);
    });

    await waitForCondition(() => {
      expect(sidebarNavButton('Debug')).toBeFalsy();
    });

    expect(mountedComponent).toBe(currentMount);
  });

  it('bumps the models refresh token on resource_changed(models|providers) and ignores other kinds', () => {
    mountedComponent = mount(App, { target: document.body });
    flushSync();

    const [handlers] = subscribeServerEventsMock.mock.calls[0];
    expect(mountedComponent.getModelsRefreshToken()).toBe(0);

    handlers.onEvent({
      type: 'resource_changed',
      sequence: 1,
      payload: { kind: 'models' },
    });
    flushSync();
    expect(mountedComponent.getModelsRefreshToken()).toBe(1);

    // A provider change also alters which models are selectable → same token.
    handlers.onEvent({
      type: 'resource_changed',
      sequence: 2,
      payload: { kind: 'providers' },
    });
    flushSync();
    expect(mountedComponent.getModelsRefreshToken()).toBe(2);

    // An unknown kind must not touch the models token.
    handlers.onEvent({
      type: 'resource_changed',
      sequence: 3,
      payload: { kind: 'mystery' },
    });
    flushSync();
    expect(mountedComponent.getModelsRefreshToken()).toBe(2);
  });

  it('bumps the clients refresh token on resource_changed(clients)', () => {
    mountedComponent = mount(App, { target: document.body });
    flushSync();

    const [handlers] = subscribeServerEventsMock.mock.calls[0];
    expect(mountedComponent.getClientsRefreshToken()).toBe(0);

    handlers.onEvent({
      type: 'resource_changed',
      sequence: 1,
      payload: { kind: 'clients' },
    });
    flushSync();
    expect(mountedComponent.getClientsRefreshToken()).toBe(1);
    // A clients change is presence-only — it must not touch the models token.
    expect(mountedComponent.getModelsRefreshToken()).toBe(0);
  });

  it('keeps the newest valid Project catalog across stale responses and transient errors', async () => {
    const staleInitial = deferred();
    const baseRpc = createEmptyChatRpcMock();
    let projectListCalls = 0;
    rpcMock.mockImplementation((method, params) => {
      if (method === 'project.list') {
        projectListCalls += 1;
        if (projectListCalls === 1) {
          return staleInitial.promise;
        }
        if (projectListCalls === 2) {
          return Promise.resolve({
            projects: [{ project_id: 'newest-project', name: 'Newest' }],
          });
        }
        return Promise.reject(new Error('temporary project failure'));
      }
      return baseRpc(method, params);
    });
    mountedComponent = mount(App, { target: document.body });
    flushSync();
    await waitForCondition(() => {
      expect(projectListCalls).toBe(1);
    });
    const [handlers] = subscribeServerEventsMock.mock.calls[0];

    handlers.onEvent({
      type: 'resource_changed',
      sequence: 1,
      payload: { kind: 'projects' },
    });
    await waitForCondition(() => {
      expect(
        mountedComponent.getProjects().map((project) => project.project_id),
      ).toEqual(['newest-project']);
    });

    staleInitial.resolve({
      projects: [{ project_id: 'stale-project', name: 'Stale' }],
    });
    await Promise.resolve();
    await Promise.resolve();
    flushSync();
    expect(
      mountedComponent.getProjects().map((project) => project.project_id),
    ).toEqual(['newest-project']);

    handlers.onEvent({
      type: 'resource_changed',
      sequence: 2,
      payload: { kind: 'projects' },
    });
    await waitForCondition(() => {
      expect(projectListCalls).toBe(3);
    });
    await Promise.resolve();
    flushSync();
    expect(
      mountedComponent.getProjects().map((project) => project.project_id),
    ).toEqual(['newest-project']);
  });

  it('routes channel and debug trace invalidations to their views', () => {
    mountedComponent = mount(App, { target: document.body });
    flushSync();

    const [handlers] = subscribeServerEventsMock.mock.calls[0];
    expect(mountedComponent.getChannelsRefreshToken()).toBe(0);
    expect(mountedComponent.getDebugTracesRefreshToken()).toBe(0);

    handlers.onEvent({
      type: 'resource_changed',
      sequence: 1,
      payload: { kind: 'channels' },
    });
    handlers.onEvent({
      type: 'resource_changed',
      sequence: 2,
      payload: { kind: 'debug_traces' },
    });
    flushSync();

    expect(mountedComponent.getChannelsRefreshToken()).toBe(1);
    expect(mountedComponent.getDebugTracesRefreshToken()).toBe(1);
  });

  it('bumps the sessions refresh token on resource_changed(sessions)', () => {
    mountedComponent = mount(App, { target: document.body });
    flushSync();

    const [handlers] = subscribeServerEventsMock.mock.calls[0];
    expect(mountedComponent.getSessionsRefreshToken()).toBe(0);

    handlers.onEvent({
      type: 'resource_changed',
      sequence: 1,
      payload: { kind: 'sessions', scope: { agent_id: 'alpha' } },
    });
    flushSync();
    expect(mountedComponent.getSessionsRefreshToken()).toBe(1);
    // Sessions invalidation must not touch the models token.
    expect(mountedComponent.getModelsRefreshToken()).toBe(0);
  });

  it('forwards the affected session scope on resource_changed(queue)', () => {
    mountedComponent = mount(App, { target: document.body });
    flushSync();

    const [handlers] = subscribeServerEventsMock.mock.calls[0];
    expect(mountedComponent.getQueueInvalidation()).toBeNull();

    handlers.onEvent({
      type: 'resource_changed',
      sequence: 1,
      payload: {
        kind: 'queue',
        scope: { agent_id: 'alpha', session_id: 's1' },
      },
    });
    flushSync();
    expect(mountedComponent.getQueueInvalidation()).toEqual({
      agentId: 'alpha',
      sessionId: 's1',
    });
  });

  it('re-fetches the agent roster on resource_changed(agents)', async () => {
    const agents = [
      { id: 'alpha', name: 'Alpha', current_session_id: 'session-alpha' },
    ];
    rpcMock.mockImplementation(createChatRpcMock(agents));

    mountedComponent = mount(App, { target: document.body });
    flushSync();

    await waitForAssertion(() => {
      expect(activeAgentTab()?.textContent).toContain('Alpha');
    });

    const agentListCallsBefore = rpcMock.mock.calls.filter(
      ([method]) => method === 'agent.list',
    ).length;

    const [handlers] = subscribeServerEventsMock.mock.calls[0];
    await handlers.onEvent({
      type: 'resource_changed',
      sequence: 1,
      payload: { kind: 'agents' },
    });
    flushSync();

    // The migrated agent-CRUD reload re-fetches agent.list (the channel carries
    // no agent data); the old agent.created/updated/deleted branch is gone.
    const agentListCallsAfter = rpcMock.mock.calls.filter(
      ([method]) => method === 'agent.list',
    ).length;
    expect(agentListCallsAfter).toBeGreaterThan(agentListCallsBefore);
  });
});

function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}
