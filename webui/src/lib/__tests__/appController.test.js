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
    onClearOutageErrors: vi.fn(),
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
    expect(state.queueInvalidation).toEqual({
      agentId: 'alpha',
      sessionId: 'session-one',
    });
    expect(actions.onReloadAgents).toHaveBeenCalledOnce();
  });

  it('owns delayed offline and restored connection notices', async () => {
    vi.useFakeTimers();
    const { actions, controller, state } = setup();

    state.connectionState.status = CONNECTION_STATUS_DISCONNECTED;
    controller.handleConnectionStatusChange();
    await vi.advanceTimersByTimeAsync(10);
    expect(state.serverNoticeState).toBe('offline');
    expect(actions.onClearOutageErrors).toHaveBeenCalledOnce();

    state.connectionState.status = CONNECTION_STATUS_CONNECTED;
    controller.handleConnectionStatusChange();
    expect(state.serverNoticeState).toBe('restored');
    expect(state.serverRecoveryGeneration).toBe(1);
    await vi.advanceTimersByTimeAsync(10);
    expect(state.serverNoticeState).toBe('');
  });
});
