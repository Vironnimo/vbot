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
  const state = createAppControllerState();
  const actions = {
    onAppError: vi.fn(),
    onLoadProjects: vi.fn(),
    onReloadAgents: vi.fn(),
  };
  const controller = createAppController({
    state,
    unavailableNoticeDelayMs: 10,
    restoredNoticeDurationMs: 10,
    ...actions,
    ...overrides,
  });
  return { actions, controller, state };
}

// Every refresh token of the App projection, e.g. { models: 0, cron: 1 }.
function refreshTokens(state) {
  return Object.fromEntries(
    Object.entries(state)
      .filter(([key]) => key.endsWith('RefreshToken'))
      .map(([key, value]) => [key.replace(/RefreshToken$/, ''), value]),
  );
}

const tokensBumped = (state, bumped) =>
  Object.fromEntries(
    Object.keys(refreshTokens(state)).map((name) => [
      name,
      bumped.includes(name) ? 1 : 0,
    ]),
  );

const resourceChanged = (kind, scope) => ({
  type: 'resource_changed',
  payload: scope === undefined ? { kind } : { kind, scope },
});

afterEach(() => {
  vi.useRealTimers();
});

describe('App controller', () => {
  it('projects server events into run state and scoped invalidations', async () => {
    const { controller, state } = setup();
    const listed = { run_id: 'run-listed', agent_id: 'alpha' };
    const hello = { type: 'connection_ready', active_runs: [listed] };
    const started = { type: 'run_started', payload: { run_id: 'run-one' } };

    await controller.handleServerEvent(hello);
    await controller.handleServerEvent(started);
    await controller.handleServerEvent(
      resourceChanged('queue', {
        agent_id: 'alpha',
        session_id: 'session-one',
      }),
    );

    expect(state.connectionSnapshot).toBe(hello);
    // Run events go to the feed, not into reactive App state.
    expect(controller.runEvents.read(0).events).toEqual([started]);
    expect(controller.runEvents.activeRuns.map((run) => run.run_id)).toEqual([
      'run-listed',
      'run-one',
    ]);
    expect(state).not.toHaveProperty('runServerEvents');
    expect(state.commandStatuses).toEqual({});
    expect(state.queueInvalidation).toEqual({
      agentId: 'alpha',
      sessionId: 'session-one',
    });
  });

  it('records each sessions invalidation scope without a full refresh', async () => {
    const { controller, state } = setup();
    const sessionsEvent = (scope) => resourceChanged('sessions', scope);
    const terminalScope = {
      project_id: null,
      agent_id: 'alpha',
      session_id: 'session-one',
      run_id: 'run-one',
    };

    await controller.handleServerEvent(sessionsEvent(terminalScope));
    await controller.handleServerEvent(resourceChanged('sessions'));

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
    const deletionEvent = (scope) => resourceChanged('sessions', scope);

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

    // Each event is a fresh deletion, even for the same identity Session. A
    // deletion without a landing leaves the Agent without a Session.
    const identityScope = {
      agent_id: 'alpha',
      project_id: null,
      deleted_session_id: 'session-three',
      next_session_id: null,
    };
    await controller.handleServerEvent(deletionEvent(identityScope));
    const first = state.sessionDeletion;
    await controller.handleServerEvent(deletionEvent(identityScope));
    expect(first).toEqual({
      agentAddress: 'alpha',
      deletedSessionId: 'session-three',
      nextSessionId: '',
    });
    expect(state.sessionDeletion).not.toBe(first);
  });

  it('keeps the latest state per command in a bounded map until a reconnect may have missed an end', async () => {
    const { controller, state } = setup();
    const statusChanged = (commandId, status, extra = {}) =>
      controller.handleServerEvent({
        type: 'command_status_changed',
        payload: {
          command_id: commandId,
          session_id: 'session-one',
          status,
          still_running: false,
          ...extra,
        },
      });
    const connectionReady = (replayStatus) =>
      controller.handleServerEvent({
        type: 'connection_ready',
        replay_status: replayStatus,
        active_runs: [],
      });

    await statusChanged('cmd_first', 'running');
    for (let index = 0; index < 205; index += 1) {
      await statusChanged(`cmd_${index}`, 'running');
    }
    await statusChanged('cmd_204', 'completed', { still_running: true });
    // A status without its Session cannot be shown in the right one.
    await statusChanged('cmd_unowned', 'running', { session_id: undefined });

    const commandIds = Object.keys(state.commandStatuses);
    expect(commandIds).toHaveLength(200);
    expect(state.commandStatuses.cmd_first).toBeUndefined();
    expect(commandIds[0]).toBe('cmd_5');
    expect(commandIds.at(-1)).toBe('cmd_204');
    expect(state.commandStatuses.cmd_204).toEqual({
      status: 'completed',
      stillRunning: true,
      sessionId: 'session-one',
    });

    await connectionReady('resumed');
    expect(Object.keys(state.commandStatuses)).toHaveLength(200);
    for (const replayStatus of ['gap', 'epoch_changed']) {
      await statusChanged('cmd_204', 'running');
      await connectionReady(replayStatus);
      expect(state.commandStatuses).toEqual({});
    }
  });

  it('keeps the non-idle Session activity of each hello and applies its changes by Session key', async () => {
    const { controller, state } = setup();
    const row = (agentId, projectId, sessionId, activity, extra = {}) => ({
      project_id: projectId,
      agent_id: agentId,
      session_id: sessionId,
      state: activity,
      work_started_at: activity === 'idle' ? null : '2026-10-21T10:00:00+00:00',
      waiting_for: { subagents: 0, commands: 0, results: 0 },
      ...extra,
    });
    const hello = (rows) =>
      controller.handleServerEvent({
        type: 'connection_ready',
        active_runs: [],
        session_activity: rows,
      });
    const changed = (payload) =>
      controller.handleServerEvent({
        type: 'session_activity_changed',
        payload,
      });

    expect(state.sessionActivity).toBeNull();
    await hello([
      row('alpha', null, 'one', 'working', {
        contributes_to_agent_activity: false,
      }),
      row('builder', 'web', 'two', 'waiting', {
        waiting_for: { subagents: 2, commands: 1, results: 0 },
      }),
    ]);
    expect(state.sessionActivity).toEqual({
      'alpha::one': {
        agentAddress: 'alpha',
        sessionId: 'one',
        state: 'working',
        workStartedAt: '2026-10-21T10:00:00+00:00',
        waitingFor: { subagents: 0, commands: 0, results: 0 },
        contributesToAgentActivity: false,
      },
      'builder@web::two': {
        agentAddress: 'builder@web',
        sessionId: 'two',
        state: 'waiting',
        workStartedAt: '2026-10-21T10:00:00+00:00',
        waitingFor: { subagents: 2, commands: 1, results: 0 },
        contributesToAgentActivity: true,
      },
    });

    await changed(row('builder', 'web', 'two', 'working'));
    expect(state.sessionActivity['builder@web::two'].state).toBe('working');
    await changed({
      ...row('alpha', null, 'one', 'idle'),
      finished_work: { duration_ms: 1000 },
    });
    expect(Object.keys(state.sessionActivity)).toEqual(['builder@web::two']);

    // Every hello replaces what the previous connection reported.
    await hello([row('gamma', null, 'three', 'waiting')]);
    expect(Object.keys(state.sessionActivity)).toEqual(['gamma::three']);
    await hello(undefined);
    expect(state.sessionActivity).toEqual({});
  });

  it('keeps the latest pushed Recall index status', async () => {
    const { controller, state } = setup();
    expect(state.recallIndexStatus).toBeNull();

    for (const indexed of [10, 20]) {
      await controller.handleServerEvent({
        type: 'recall_index_status',
        payload: { state: 'indexing', indexed, waiting: 5 },
      });
    }

    expect(state.recallIndexStatus).toEqual({
      state: 'indexing',
      indexed: 20,
      waiting: 5,
    });
  });

  it('loads background activity on every connection and applies its pushes', async () => {
    const onLoadBackgroundActivity = vi.fn().mockResolvedValue(undefined);
    const { controller, state } = setup({ onLoadBackgroundActivity });
    expect(state.backgroundActivity).toEqual([]);

    for (const replayStatus of ['resumed', 'epoch_changed']) {
      await controller.handleServerEvent({
        type: 'connection_ready',
        replay_status: replayStatus,
        active_runs: [],
      });
    }
    expect(onLoadBackgroundActivity).toHaveBeenCalledTimes(2);

    const activities = [{ id: 'recall_index', state: 'running' }];
    await controller.handleServerEvent({
      type: 'activity_status',
      payload: { activities },
    });
    expect(state.backgroundActivity).toEqual(activities);
    await controller.handleServerEvent({
      type: 'activity_status',
      payload: {},
    });
    expect(state.backgroundActivity).toEqual([]);
  });

  it('reloads the data-store projection on connect and invalidation', async () => {
    const onLoadDataStoreStatus = vi.fn().mockResolvedValue(undefined);
    const { controller, state } = setup({ onLoadDataStoreStatus });
    expect(state).toMatchObject({ dataStoreIncident: null });

    await controller.handleServerEvent({
      type: 'connection_ready',
      replay_status: 'resumed',
      active_runs: [],
    });
    await controller.handleServerEvent(resourceChanged('data_store'));

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
    const onCheckWebuiBuild = vi.fn();
    const { actions, controller, state } = setup({
      onLoadDataStoreStatus,
      onReloadExtensionPages,
      onCheckWebuiBuild,
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
    expect(onCheckWebuiBuild).toHaveBeenCalledOnce();
  });

  it('hands an owner-scoped Extension change to its owner without reloading page descriptors', async () => {
    const onReloadExtensionPages = vi.fn().mockResolvedValue(undefined);
    const onExtensionChange = vi.fn();
    const { controller } = setup({
      onReloadExtensionPages,
      onExtensionChange,
    });
    const scope = {
      owner: 'swarm',
      resource: 'board',
      ids: ['swarm-one'],
      revision: 7,
    };

    await controller.handleServerEvent(resourceChanged('extensions', scope));

    expect(onExtensionChange).toHaveBeenCalledExactlyOnceWith(scope);
    expect(onReloadExtensionPages).not.toHaveBeenCalled();

    // An Extension reload carries no scope: its page descriptors may differ.
    await controller.handleServerEvent(resourceChanged('extensions'));

    expect(onReloadExtensionPages).toHaveBeenCalledOnce();
    expect(onExtensionChange).toHaveBeenCalledOnce();
  });

  it('applies an Agent rename mapping before reloading', async () => {
    const onAgentIdChanged = vi.fn();
    const { actions, controller, state } = setup({ onAgentIdChanged });
    await controller.handleServerEvent({
      type: 'connection_ready',
      active_runs: [],
      session_activity: [
        { agent_id: 'alpha', session_id: 'one', state: 'working' },
      ],
    });

    await controller.handleServerEvent(
      resourceChanged('agents', {
        old_agent_id: 'alpha',
        new_agent_id: 'researcher',
      }),
    );
    await controller.handleServerEvent(
      resourceChanged('agents', {
        old_agent_id: 'researcher',
        new_agent_id: 'analyst',
      }),
    );

    // The renamed Agent's working Session moves to its new address.
    expect(Object.keys(state.sessionActivity)).toEqual(['analyst::one']);
    expect(state.sessionActivity['analyst::one'].agentAddress).toBe('analyst');
    expect(onAgentIdChanged).toHaveBeenNthCalledWith(1, 'alpha', 'researcher');
    expect(onAgentIdChanged).toHaveBeenNthCalledWith(
      2,
      'researcher',
      'analyst',
    );
    expect(actions.onReloadAgents).toHaveBeenCalledTimes(2);
  });

  it.each([
    ['gap', 1],
    ['epoch_changed', 1],
    ['resumed', 0],
  ])(
    'after replay status %s refreshes every resource-backed projection %i time(s)',
    async (replayStatus, times) => {
      const { actions, controller, state } = setup();

      await controller.handleServerEvent({
        type: 'connection_ready',
        replay_status: replayStatus,
        active_runs: [],
        queues: [],
      });

      for (const token of Object.values(refreshTokens(state))) {
        expect(token).toBe(times);
      }
      expect(actions.onLoadProjects).toHaveBeenCalledTimes(times);
      expect(actions.onReloadAgents).toHaveBeenCalledTimes(times);
    },
  );

  // resource_changed names a kind of shared state; the controller bumps the
  // refresh tokens its views watch or reloads the owning roster.
  it.each([
    ['models', ['models']],
    ['providers', ['models', 'credentials']],
    ['memories', ['memories']],
    ['projects', ['projects']],
    ['clients', ['clients']],
    ['devices', ['devices']],
    ['channels', ['channels']],
    ['debug_traces', ['debugTraces']],
    ['cron', ['cron']],
    ['calendar', ['calendar']],
    ['commands', ['commands']],
    ['terminals', ['terminals']],
    ['skills', ['skills']],
    ['archive', ['archive']],
    ['agents', []],
    ['queue', []],
    ['data_store', ['credentials']],
    ['agent_secrets', ['agentSecrets']],
    ['mystery', []],
  ])('routes resource_changed(%s) to the tokens %j', async (kind, bumped) => {
    const { actions, controller, state } = setup();

    await controller.handleServerEvent(resourceChanged(kind));

    expect(refreshTokens(state)).toEqual(tokensBumped(state, bumped));
    expect(actions.onLoadProjects).toHaveBeenCalledTimes(
      kind === 'projects' ? 1 : 0,
    );
    expect(actions.onReloadAgents).toHaveBeenCalledTimes(
      kind === 'agents' ? 1 : 0,
    );
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
