import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  createAppController,
  createAppControllerState,
} from '../appController.js';
import {
  CONNECTION_STATUS_CONNECTED,
  CONNECTION_STATUS_DISCONNECTED,
} from '../connectionState.js';

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

  it('reloads the Session-store projection on connect and invalidation', async () => {
    const onLoadSessionStoreStatus = vi.fn().mockResolvedValue(undefined);
    const { controller } = setup({ onLoadSessionStoreStatus });

    await controller.handleServerEvent({
      type: 'connection_ready',
      replay_status: 'resumed',
      active_runs: [],
    });
    await controller.handleServerEvent({
      type: 'resource_changed',
      payload: { kind: 'session_store' },
    });

    expect(onLoadSessionStoreStatus).toHaveBeenCalledTimes(2);
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
