import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  createAppController,
  createAppControllerState,
} from '../appController.js';
import {
  CONNECTION_STATUS_CONNECTED,
  CONNECTION_STATUS_DISCONNECTED,
} from '../connectionState.js';
import { MAX_SESSION_INVALIDATIONS } from '../sessionInvalidation.js';

function setup(overrides = {}) {
  const state = createAppControllerState('chat');
  const browserHistory = {
    pushState: vi.fn(),
    replaceState: vi.fn(),
    state: null,
  };
  const browserWindow = {
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    location: { hash: '#chat' },
  };
  const actions = {
    onAppError: vi.fn(),
    onLoadProjects: vi.fn(),
    onReloadAgents: vi.fn(),
    onSetOnboardingAside: vi.fn(),
  };
  const controller = createAppController({
    state,
    knownViewIds: ['chat', 'agents', 'projects', 'settings', 'system-prompt'],
    defaultViewId: 'chat',
    currentNavigationSelection: () => ({
      agentId: 'alpha',
      projectId: '',
      projectAgentId: null,
    }),
    isDebugEnabled: () => false,
    isOperational: () => true,
    browserHistory,
    browserWindow,
    unavailableNoticeDelayMs: 10,
    restoredNoticeDurationMs: 10,
    ...actions,
    ...overrides,
  });
  return { actions, browserHistory, browserWindow, controller, state };
}

afterEach(() => {
  vi.useRealTimers();
});

describe('App controller', () => {
  it('opens calendar results as ordinary Sessions', () => {
    const { controller, state } = setup();
    expect(
      controller.navigateToSession('builder@project', 'calendar-result'),
    ).toBe(true);
    expect(state.pendingSessionNavigation).toMatchObject({
      agentId: 'builder@project',
      sessionId: 'calendar-result',
      subAgent: false,
    });
  });
  it('owns view navigation and its browser-history entry', () => {
    const onSetOnboardingAside = vi.fn();
    const { browserHistory, controller, state } = setup({
      isOperational: () => false,
      onSetOnboardingAside,
    });

    expect(controller.selectView('agents')).toBe(true);

    expect(state.activeViewId).toBe('agents');
    expect(onSetOnboardingAside).toHaveBeenCalledOnce();
    expect(browserHistory.pushState).toHaveBeenCalledWith(
      expect.objectContaining({ view: 'agents' }),
      '',
      '#agents',
    );
  });

  it('resolves a startup Extension page link after the page catalog loads', () => {
    const knownViewIds = ['chat', 'settings'];
    const { browserHistory, browserWindow, controller, state } = setup({
      knownViewIds: () => knownViewIds,
    });
    // A reload restores the entry's view even without a hash.
    browserWindow.location.hash = '';
    browserHistory.state = {
      marker: 'vbot.navigation',
      view: 'extension:fixture:main',
      session: null,
      selection: null,
    };

    controller.initializeNavigationHistory();
    expect(state.activeViewId).toBe('chat');
    expect(browserHistory.replaceState).not.toHaveBeenCalled();

    knownViewIds.push('extension:fixture:main');
    expect(controller.resolvePendingExtensionView()).toBe(true);
    expect(state.activeViewId).toBe('extension:fixture:main');
    expect(browserHistory.replaceState).toHaveBeenCalledWith(
      expect.objectContaining({ view: 'extension:fixture:main' }),
      '',
      '#extension:fixture:main',
    );
    expect(browserHistory.pushState).not.toHaveBeenCalled();
    // One-shot: later catalog refreshes leave navigation alone.
    expect(controller.resolvePendingExtensionView()).toBe(false);
  });

  it('drops a startup Extension page link the page catalog does not contain', () => {
    const { browserHistory, browserWindow, controller, state } = setup();
    browserWindow.location.hash = '#extension:missing:page';

    controller.initializeNavigationHistory();
    expect(controller.resolvePendingExtensionView()).toBe(false);

    expect(state.activeViewId).toBe('chat');
    expect(browserHistory.replaceState).toHaveBeenCalledWith(
      expect.objectContaining({ view: 'chat' }),
      '',
      '#chat',
    );
  });

  it('forgets a startup Extension page link once the user navigates', () => {
    const knownViewIds = ['chat', 'settings'];
    const { browserHistory, browserWindow, controller, state } = setup({
      knownViewIds: () => knownViewIds,
    });
    browserWindow.location.hash = '#extension:fixture:main';

    controller.initializeNavigationHistory();
    controller.selectView('settings');
    knownViewIds.push('extension:fixture:main');

    expect(controller.resolvePendingExtensionView()).toBe(false);
    expect(state.activeViewId).toBe('settings');
    expect(browserHistory.replaceState).not.toHaveBeenCalled();
  });

  it('marks direct Sub-Agent link navigation for live-tail scrolling only', () => {
    const { browserHistory, controller, state } = setup();

    expect(controller.navigateToSubAgent('subagent', 'child-session')).toBe(
      true,
    );
    expect(state.pendingSessionNavigation).toMatchObject({
      agentId: 'subagent',
      sessionId: 'child-session',
      subAgent: true,
      followSession: true,
    });
    expect(browserHistory.pushState).toHaveBeenLastCalledWith(
      expect.objectContaining({
        session: {
          agentId: 'subagent',
          sessionId: 'child-session',
          subAgent: true,
        },
      }),
      '',
      '#chat',
    );

    controller.applyNavigationState({
      view: 'chat',
      session: {
        agentId: 'subagent',
        sessionId: 'other-child-session',
        subAgent: true,
      },
      selection: { agentId: 'alpha', projectId: '', projectAgentId: null },
    });

    expect(state.pendingSessionNavigation).not.toHaveProperty('followSession');
  });

  it('projects server events into run state and scoped invalidations', async () => {
    const { actions, controller, state } = setup();
    const hello = { type: 'connection_ready', active_runs: [] };

    await controller.handleServerEvent(hello);
    await controller.handleServerEvent({
      type: 'run_started',
      payload: { run_id: 'run-one' },
    });
    await controller.handleServerEvent({
      type: 'resource_changed',
      payload: {
        kind: 'queue',
        scope: { agent_id: 'alpha', session_id: 'session-one' },
      },
    });
    await controller.handleServerEvent({
      type: 'resource_changed',
      payload: { kind: 'agents' },
    });

    expect(state.connectionSnapshot).toBe(hello);
    expect(state.runServerEvents).toHaveLength(1);
    expect(state.backgroundBashStatusEvents).toHaveLength(0);
    expect(state.queueInvalidation).toEqual({
      agentId: 'alpha',
      sessionId: 'session-one',
    });
    expect(actions.onReloadAgents).toHaveBeenCalledOnce();
  });

  it('records each sessions invalidation scope without a full refresh', async () => {
    const { controller, state } = setup();
    const sessionsEvent = (scope) => ({
      type: 'resource_changed',
      payload: { kind: 'sessions', scope },
    });
    const terminalScope = {
      project_id: null,
      agent_id: 'alpha',
      session_id: 'session-one',
      run_id: 'run-one',
    };

    await controller.handleServerEvent(sessionsEvent(terminalScope));
    await controller.handleServerEvent({
      type: 'resource_changed',
      payload: { kind: 'sessions' },
    });

    expect(state.sessionsRefreshToken).toBe(0);
    expect(state.sessionInvalidations).toEqual([
      { id: 1, scope: terminalScope },
      { id: 2, scope: null },
    ]);

    for (let index = 0; index < MAX_SESSION_INVALIDATIONS; index += 1) {
      await controller.handleServerEvent(sessionsEvent(terminalScope));
    }
    expect(state.sessionInvalidations).toHaveLength(MAX_SESSION_INVALIDATIONS);
    expect(state.sessionInvalidations[0].id).toBe(3);
    expect(state.sessionInvalidations.at(-1).id).toBe(
      MAX_SESSION_INVALIDATIONS + 2,
    );
  });

  it('turns a sessions deletion event into a Session deletion for Chat', async () => {
    const { controller, state } = setup();
    const deletionEvent = (scope) => ({
      type: 'resource_changed',
      payload: { kind: 'sessions', scope },
    });

    // A plain list change (create/rename) names no deleted Session.
    await controller.handleServerEvent(
      deletionEvent({ agent_id: 'alpha', session_id: 'session-zero' }),
    );
    expect(state.sessionInvalidations).toHaveLength(1);
    expect(state.sessionDeletion).toBeNull();

    await controller.handleServerEvent(
      deletionEvent({
        agent_id: 'builder',
        project_id: 'vbot',
        deleted_session_id: 'session-one',
        next_session_id: 'session-two',
      }),
    );
    expect(state.sessionInvalidations).toHaveLength(2);
    expect(state.sessionDeletion).toEqual({
      agentAddress: 'builder@vbot',
      deletedSessionId: 'session-one',
      nextSessionId: 'session-two',
    });

    // Each event is a fresh deletion, even for the same identity Session.
    const identityScope = {
      agent_id: 'alpha',
      project_id: null,
      deleted_session_id: 'session-three',
      next_session_id: 'session-four',
    };
    await controller.handleServerEvent(deletionEvent(identityScope));
    const first = state.sessionDeletion;
    await controller.handleServerEvent(deletionEvent(identityScope));
    expect(first).toEqual({
      agentAddress: 'alpha',
      deletedSessionId: 'session-three',
      nextSessionId: 'session-four',
    });
    expect(state.sessionDeletion).not.toBe(first);
  });

  it('keeps the active Run list current beyond the bounded event window', async () => {
    const { controller, state } = setup();
    const lifecycle = (type, runId, extra = {}) => ({
      type,
      payload: {
        run_id: runId,
        agent_id: 'beta',
        project_id: null,
        session_id: `session-${runId}`,
        run_kind: 'chat',
        run_event_type: type,
        run_event_sequence: 1,
        run_event_timestamp: '2026-09-22T10:00:00+00:00',
        ...extra,
      },
    });

    await controller.handleServerEvent({
      type: 'connection_ready',
      active_runs: [{ run_id: 'listed', agent_id: 'beta', session_id: 's' }],
    });
    await controller.handleServerEvent(lifecycle('run_started', 'new'));
    await controller.handleServerEvent(lifecycle('run_completed', 'listed'));
    // The listed Run's terminal event leaves the bounded window...
    for (let index = 0; index < 500; index += 1) {
      await controller.handleServerEvent(lifecycle('run_output', 'new'));
    }

    // ...but the active list still reflects it; the new Run is running.
    expect(state.runServerEvents).toHaveLength(500);
    expect(state.activeRuns).toEqual([
      {
        run_id: 'new',
        agent_id: 'beta',
        project_id: null,
        session_id: 'session-new',
        run_kind: 'chat',
        status: 'running',
        started_at: '2026-09-22T10:00:00+00:00',
      },
    ]);

    const hello = { type: 'connection_ready', active_runs: [] };
    await controller.handleServerEvent(hello);
    expect(state.activeRuns).toEqual([]);
  });

  it('buffers background Bash status events as a bounded accessor list', async () => {
    const { controller, state } = setup();

    for (let index = 0; index < 55; index += 1) {
      await controller.handleServerEvent({
        type: 'bash_process_status_changed',
        payload: { process_id: `process-${index}`, status: 'completed' },
      });
    }

    expect(state.backgroundBashStatusEvents).toHaveLength(50);
    expect(state.backgroundBashStatusEvents[0].payload.process_id).toBe(
      'process-5',
    );
    expect(state.backgroundBashStatusEvents[49].payload.process_id).toBe(
      'process-54',
    );
  });

  it('reloads the data-store projection on connect and invalidation', async () => {
    const onLoadDataStoreStatus = vi.fn().mockResolvedValue(undefined);
    const { controller } = setup({ onLoadDataStoreStatus });

    await controller.handleServerEvent({
      type: 'connection_ready',
      replay_status: 'resumed',
      active_runs: [],
    });
    await controller.handleServerEvent({
      type: 'resource_changed',
      payload: { kind: 'data_store' },
    });

    expect(onLoadDataStoreStatus).toHaveBeenCalledTimes(2);
  });

  it('refreshes Extension page descriptors on each bounded reconnect without replaying mutations', async () => {
    const onReloadExtensionPages = vi.fn().mockResolvedValue(undefined);
    const { controller } = setup({ onReloadExtensionPages });

    await controller.handleServerEvent({
      type: 'connection_ready',
      replay_status: 'resumed',
      active_runs: [],
    });

    expect(onReloadExtensionPages).toHaveBeenCalledOnce();
  });

  it('starts independent replay-gap recovery without waiting for optional projections', async () => {
    let finishStatus;
    let finishPages;
    const onLoadDataStoreStatus = vi.fn(
      () =>
        new Promise((resolve) => {
          finishStatus = resolve;
        }),
    );
    const onReloadExtensionPages = vi.fn(
      () =>
        new Promise((resolve) => {
          finishPages = resolve;
        }),
    );
    const { actions, controller, state } = setup({
      onLoadDataStoreStatus,
      onReloadExtensionPages,
    });

    const recovery = controller.handleServerEvent({
      type: 'connection_ready',
      replay_status: 'gap',
      active_runs: [],
      queues: [],
    });
    await Promise.resolve();

    expect(state.modelsRefreshToken).toBe(1);
    expect(state.sessionsRefreshToken).toBe(1);
    expect(actions.onLoadProjects).toHaveBeenCalledOnce();
    expect(actions.onReloadAgents).toHaveBeenCalledOnce();
    expect(onLoadDataStoreStatus).toHaveBeenCalledOnce();
    expect(onReloadExtensionPages).toHaveBeenCalledOnce();
    controller.destroy();
    finishStatus();
    finishPages();
    await recovery;
    expect(state.modelsRefreshToken).toBe(1);
    expect(actions.onReloadAgents).toHaveBeenCalledOnce();
  });

  it('starts every recovery owner even when another owner fails synchronously', async () => {
    const failure = new Error('optional projection failed');
    const onLoadDataStoreStatus = vi.fn(() => {
      throw failure;
    });
    const onReloadExtensionPages = vi.fn();
    const { actions, controller, state } = setup({
      onLoadDataStoreStatus,
      onReloadExtensionPages,
    });
    await expect(
      controller.handleServerEvent({
        type: 'connection_ready',
        replay_status: 'epoch_changed',
      }),
    ).rejects.toBe(failure);

    expect(state.modelsRefreshToken).toBe(1);
    expect(actions.onLoadProjects).toHaveBeenCalledOnce();
    expect(actions.onReloadAgents).toHaveBeenCalledOnce();
    expect(onReloadExtensionPages).toHaveBeenCalledOnce();
  });

  it('refreshes an Extension page after its owner-qualified invalidation', async () => {
    const onReloadExtensionPages = vi.fn().mockResolvedValue(undefined);
    const { controller } = setup({ onReloadExtensionPages });

    await controller.handleServerEvent({
      type: 'resource_changed',
      payload: {
        kind: 'extensions',
        scope: {
          owner: 'swarm',
          resource: 'board',
          ids: ['swarm-one'],
          revision: 7,
        },
      },
    });

    expect(onReloadExtensionPages).toHaveBeenCalledOnce();
  });

  it('applies an Agent rename mapping before reloading and remaps old history entries', async () => {
    const onAgentIdChanged = vi.fn();
    const { actions, controller, state } = setup({ onAgentIdChanged });
    controller.applyNavigationState({
      view: 'chat',
      session: { agentId: 'alpha', sessionId: 'session-one' },
      selection: { agentId: 'alpha', projectId: '', projectAgentId: null },
    });

    await controller.handleServerEvent({
      type: 'resource_changed',
      payload: {
        kind: 'agents',
        scope: { old_agent_id: 'alpha', new_agent_id: 'researcher' },
      },
    });

    expect(onAgentIdChanged).toHaveBeenCalledWith('alpha', 'researcher');
    expect(actions.onReloadAgents).toHaveBeenCalledOnce();
    expect(state.pendingSessionNavigation).toMatchObject({
      agentId: 'researcher',
      selection: { agentId: 'researcher' },
    });

    controller.applyNavigationState({
      view: 'chat',
      session: { agentId: 'alpha', sessionId: 'older-session' },
      selection: { agentId: 'alpha', projectId: '', projectAgentId: null },
    });

    expect(state.pendingSessionNavigation).toMatchObject({
      agentId: 'researcher',
      sessionId: 'older-session',
      selection: { agentId: 'researcher' },
    });
  });

  it.each(['gap', 'epoch_changed'])(
    'fully invalidates resource-backed projections after replay status %s',
    async (replayStatus) => {
      const { actions, controller, state } = setup();

      await controller.handleServerEvent({
        type: 'connection_ready',
        replay_status: replayStatus,
        active_runs: [],
        queues: [],
      });

      expect(state.modelsRefreshToken).toBe(1);
      expect(state.memoriesRefreshToken).toBe(1);
      expect(state.projectsRefreshToken).toBe(1);
      expect(state.sessionsRefreshToken).toBe(1);
      expect(state.clientsRefreshToken).toBe(1);
      expect(state.channelsRefreshToken).toBe(1);
      expect(state.cronRefreshToken).toBe(1);
      expect(state.debugTracesRefreshToken).toBe(1);
      expect(state.commandsRefreshToken).toBe(1);
      expect(state.terminalsRefreshToken).toBe(1);
      expect(actions.onLoadProjects).toHaveBeenCalledOnce();
      expect(actions.onReloadAgents).toHaveBeenCalledOnce();
    },
  );

  it('does not reload all resources after a complete replay resume', async () => {
    const { actions, controller, state } = setup();

    await controller.handleServerEvent({
      type: 'connection_ready',
      replay_status: 'resumed',
      active_runs: [],
      queues: [],
    });

    expect(state.modelsRefreshToken).toBe(0);
    expect(state.memoriesRefreshToken).toBe(0);
    expect(state.projectsRefreshToken).toBe(0);
    expect(state.sessionsRefreshToken).toBe(0);
    expect(state.clientsRefreshToken).toBe(0);
    expect(state.channelsRefreshToken).toBe(0);
    expect(state.cronRefreshToken).toBe(0);
    expect(state.debugTracesRefreshToken).toBe(0);
    expect(state.commandsRefreshToken).toBe(0);
    expect(state.terminalsRefreshToken).toBe(0);
    expect(actions.onLoadProjects).not.toHaveBeenCalled();
    expect(actions.onReloadAgents).not.toHaveBeenCalled();
  });

  it('bumps the cron refresh token for cron changes', async () => {
    const { controller, state } = setup();

    await controller.handleServerEvent({
      type: 'resource_changed',
      payload: { kind: 'cron' },
    });

    expect(state.cronRefreshToken).toBe(1);
  });

  it('bumps the memories refresh token for Memory changes', async () => {
    const { controller, state } = setup();

    await controller.handleServerEvent({
      type: 'resource_changed',
      payload: { kind: 'memories', scope: { agent_id: 'alpha' } },
    });

    expect(state.memoriesRefreshToken).toBe(1);
  });

  it('bumps the command refresh token for command catalog changes', async () => {
    const { controller, state } = setup();

    await controller.handleServerEvent({
      type: 'resource_changed',
      payload: { kind: 'commands' },
    });

    expect(state.commandsRefreshToken).toBe(1);
  });

  it('bumps the terminals refresh token for Terminal Session changes', async () => {
    const { controller, state } = setup();

    await controller.handleServerEvent({
      type: 'resource_changed',
      payload: { kind: 'terminals' },
    });

    expect(state.terminalsRefreshToken).toBe(1);
  });

  it('owns delayed offline and restored connection notices', async () => {
    vi.useFakeTimers();
    const { controller, state } = setup();

    state.connectionState.status = CONNECTION_STATUS_DISCONNECTED;
    controller.handleConnectionStatusChange();
    await vi.advanceTimersByTimeAsync(10);
    expect(state.serverNoticeState).toBe('offline');

    state.connectionState.status = CONNECTION_STATUS_CONNECTED;
    controller.handleConnectionStatusChange();
    expect(state.serverNoticeState).toBe('restored');
    expect(state.serverRecoveryGeneration).toBe(1);
    await vi.advanceTimersByTimeAsync(10);
    expect(state.serverNoticeState).toBe('');
  });
});
