import { describe, expect, it, vi } from 'vitest';
import { applyTimelineEvent } from '../chatTimeline.js';
import { assistantRun } from '../../components/__tests__/timelineItems.support.js';
import {
  agentActivityStatus,
  agentUnreadResults,
  appendRunEvent,
  createChatState,
  ensureSessionState,
  isRunActive,
  newestUnreadSessionForAgent,
  visibleTimelineItemsForRender,
} from '../chatState.js';
import {
  applySessionActivityChange,
  sessionActivityFromSnapshot,
} from '../sessionActivity.js';
import { createRunEventFeed } from '../runEventFeed.svelte.js';
import {
  deferred,
  runServerEvent,
  setupController,
} from './chatState.support.js';

describe('Agent roster', () => {
  it.each(['resolve', 'reject'])(
    'ignores an older roster %s after a silent refresh completes initial loading',
    async (outcome) => {
      const older = deferred();
      const newer = deferred();
      const loadChatHistory = vi
        .fn()
        .mockResolvedValue({ messages: [], active_run: null });
      const { chatState, controller, onAgentsChanged, onAgentSelected } =
        setupController({
          operationOverrides: {
            listAgents: vi
              .fn()
              .mockReturnValueOnce(older.promise)
              .mockReturnValueOnce(newer.promise),
            loadChatHistory,
          },
        });
      const initial = controller.loadAgents();
      const refresh = controller.loadAgents({ silent: true });
      expect(chatState.loadingAgents).toBe(true);
      const agents = [{ id: 'beta', current_session_id: 'beta-session' }];
      newer.resolve({ agents });
      expect(await refresh).toBe(true);
      expect(chatState.loadingAgents).toBe(false);
      expect(loadChatHistory).toHaveBeenCalledWith(
        expect.objectContaining({
          agent_id: 'beta',
          session_id: 'beta-session',
        }),
      );
      if (outcome === 'resolve') older.resolve({ agents: [{ id: 'deleted' }] });
      else older.reject(new Error('stale failure'));
      expect(await initial).toBe(false);
      expect(chatState.agents).toEqual(agents);
      expect(chatState.selectedAgentId).toBe('beta');
      expect(chatState.agentsError).toBeNull();
      expect(onAgentsChanged).toHaveBeenCalledExactlyOnceWith(agents);
      expect(onAgentSelected).toHaveBeenCalledExactlyOnceWith('beta');
      expect(loadChatHistory).toHaveBeenCalledOnce();
    },
  );

  it('does not clear a newer roster loading state when an older request finishes', async () => {
    const older = deferred();
    const newer = deferred();
    const { chatState, controller } = setupController({
      operationOverrides: {
        listAgents: vi
          .fn()
          .mockReturnValueOnce(older.promise)
          .mockReturnValueOnce(newer.promise),
      },
    });
    const initial = controller.loadAgents();
    const refresh = controller.loadAgents({ silent: true });
    older.resolve({ agents: [{ id: 'deleted' }] });
    expect(await initial).toBe(false);
    expect(chatState.loadingAgents).toBe(true);
    newer.resolve({ agents: [] });
    await refresh;
    expect(chatState.loadingAgents).toBe(false);
  });

  it.each(['resolve', 'reject'])(
    'ignores pending roster %s after disposal',
    async (outcome) => {
      const response = deferred();
      const { chatState, controller, onAgentsChanged, onAgentSelected } =
        setupController({
          operationOverrides: { listAgents: vi.fn(() => response.promise) },
        });
      const loading = controller.loadAgents();
      controller.destroy();
      if (outcome === 'resolve')
        response.resolve({ agents: [{ id: 'alpha' }] });
      else response.reject(new Error('late failure'));
      expect(await loading).toBe(false);
      expect(chatState.agents).toEqual([]);
      expect(chatState.loadingAgents).toBe(false);
      expect(chatState.agentsError).toBeNull();
      expect(onAgentsChanged).not.toHaveBeenCalled();
      expect(onAgentSelected).not.toHaveBeenCalled();
    },
  );

  it('reports a failed roster load and clears the error on the next load', async () => {
    const { chatState, controller, onAgentsChanged, onAgentSelected } =
      setupController({
        shouldLoadCurrentHistory: () => false,
        operationOverrides: {
          listAgents: vi
            .fn()
            .mockRejectedValueOnce(new Error('Server unavailable'))
            .mockResolvedValueOnce({ agents: [{ id: 'alpha' }] }),
        },
      });

    expect(await controller.loadAgents()).toBe(false);
    expect(chatState.agentsError).toBe('Server unavailable');
    expect(chatState.loadingAgents).toBe(false);
    expect(chatState.agents).toEqual([]);
    expect(onAgentsChanged).not.toHaveBeenCalled();
    expect(onAgentSelected).not.toHaveBeenCalled();

    expect(await controller.loadAgents()).toBe(true);
    expect(chatState.agentsError).toBeNull();
    expect(chatState.agents).toEqual([{ id: 'alpha' }]);
    expect(onAgentsChanged).toHaveBeenCalledExactlyOnceWith([{ id: 'alpha' }]);
  });

  it('loads the roster, current History, Run truth, and Queue as one lifecycle', async () => {
    const history = deferred();
    const loadChatHistory = vi.fn(() => history.promise);
    const listAgents = vi.fn().mockResolvedValue({
      agents: [
        {
          id: 'alpha',
          name: 'Alpha',
          current_session_id: 'session-one',
        },
      ],
    });
    const { chatState, controller, listQueue, runStream } = setupController({
      isDisplayedSession: (agentId, sessionId) =>
        agentId === 'alpha' && sessionId === 'session-one',
      operationOverrides: { listAgents, loadChatHistory },
    });

    const loading = controller.loadAgents();
    await vi.waitFor(() => expect(loadChatHistory).toHaveBeenCalledOnce());

    // The Agent loading state ends before current History settles.
    expect(chatState.selectedAgentId).toBe('alpha');
    expect(chatState.loadingAgents).toBe(false);
    expect(chatState.loadingHistory).toBe(true);

    history.resolve({
      active_run: null,
      has_more: false,
      messages: [{ id: 'message-one', role: 'user', content: 'Hello' }],
    });
    await expect(loading).resolves.toBe(true);

    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    expect(sessionState.messages).toMatchObject([
      { id: 'message-one', content: 'Hello' },
    ]);
    expect(listQueue).toHaveBeenCalledWith('alpha', 'session-one');
    expect(runStream.attachRunStream).toHaveBeenCalledWith(sessionState, null);
    expect(chatState.loadingHistory).toBe(false);
  });

  it('silent loadAgents skips loadingAgents flag and history reload', async () => {
    const loadChatHistory = vi.fn().mockResolvedValue({
      active_run: null,
      messages: [],
      has_more: false,
    });
    const listAgents = vi.fn().mockResolvedValue({
      agents: [
        {
          id: 'alpha',
          name: 'Alpha',
          current_session_id: 'session-one',
        },
      ],
    });
    const { chatState, controller } = setupController({
      isDisplayedSession: () => true,
      operationOverrides: { listAgents, loadChatHistory },
    });

    await controller.loadAgents({ silent: true });

    expect(chatState.loadingAgents).toBe(false);
    expect(chatState.agents).toHaveLength(1);
    expect(loadChatHistory).not.toHaveBeenCalled();
  });
});

describe('command suggestions', () => {
  it('normalizes command suggestions inside the controller', async () => {
    const listChatCommands = vi.fn().mockResolvedValue({
      items: [
        { name: '/HELP', type: 'command', description: 'Show help' },
        {
          name: 'goal',
          type: 'command',
          argument: 'optional',
          argument_hint: '<objective>',
        },
        { name: 'review', type: 'skill', description: 'Review code' },
        { name: 'help', type: 'skill', slash: false },
      ],
    });
    const { chatState, controller } = setupController({
      operationOverrides: { listChatCommands },
    });

    await controller.loadCommands('alpha');

    expect(listChatCommands).toHaveBeenCalledWith({ agent_id: 'alpha' });
    expect(chatState.availableSkills).toMatchObject([
      { name: 'help', type: 'command' },
      { name: 'goal', type: 'command', argument_hint: '<objective>' },
      { name: 'review', type: 'skill', slash: true },
      { name: 'help', type: 'skill', slash: false },
    ]);
  });

  it.each([
    ['a Session', { sessionId: 'session-1' }, { session_id: 'session-1' }],
    [
      'the Project a draft chose',
      { workingProjectId: 'vbot' },
      { working_project_id: 'vbot' },
    ],
    [
      'the Workspace a draft chose',
      { workingProjectId: null },
      { working_project_id: null },
    ],
  ])('asks for the commands and Skills of %s', async (_case, scope, params) => {
    const listChatCommands = vi.fn().mockResolvedValue({ items: [] });
    const { controller } = setupController({
      operationOverrides: { listChatCommands },
    });

    await controller.loadCommands('alpha', scope);

    expect(listChatCommands).toHaveBeenCalledWith({
      agent_id: 'alpha',
      ...params,
    });
  });

  it('ignores stale command errors after a newer Agent catalog loads', async () => {
    let rejectOlderRequest;
    const listChatCommands = vi
      .fn()
      .mockImplementationOnce(
        () =>
          new Promise((_, reject) => {
            rejectOlderRequest = reject;
          }),
      )
      .mockResolvedValueOnce({
        items: [{ name: 'review', type: 'skill', description: 'Review code' }],
      });
    const { chatState, controller } = setupController({
      operationOverrides: { listChatCommands },
    });

    const olderLoad = controller.loadCommands('alpha');
    expect(await controller.loadCommands('beta')).toBe(true);
    rejectOlderRequest(new Error('old Agent offline'));
    expect(await olderLoad).toBe(false);

    expect(chatState.commandsError).toBe('');
    expect(chatState.availableSkills).toMatchObject([
      { name: 'review', type: 'skill' },
    ]);
  });
});

describe('completion activity', () => {
  it('refreshes durable completion activity for every listed Agent Session', async () => {
    const listSessionActivity = vi.fn(async () => ({
      agents: [
        {
          agent_id: 'alpha',
          project_id: null,
          sessions: [
            {
              id: 'session-one',
              notification_revision: 1,
              notification_count: 1,
              local_unread_count: 1,
              latest_completion_run_id: 'run-one',
              unread_run_status: 'completed',
              unread_run_at: '2026-07-20T10:00:00+00:00',
            },
          ],
        },
        { agent_id: 'beta', project_id: null, sessions: [] },
      ],
    }));
    const { chatState, controller } = setupController({
      operationOverrides: { listSessionActivity },
    });

    await controller.refreshAgentActivity(['alpha', 'beta', 'alpha']);

    expect(listSessionActivity).toHaveBeenCalledOnce();
    expect(listSessionActivity).toHaveBeenCalledWith(['alpha', 'beta']);
    expect(chatState.loadingAgentActivity).toBe(false);
    expect(chatState.agentActivityError).toBe('');
    expect(ensureSessionState(chatState, 'alpha', 'session-one')).toMatchObject(
      {
        notificationCount: 1,
        unreadRunStatus: 'completed',
      },
    );
  });

  it('keeps Project Agent activity scoped to its qualified address', async () => {
    const listSessionActivity = vi.fn(async () => ({
      agents: [
        {
          agent_id: 'builder',
          project_id: 'project-one',
          sessions: [
            {
              id: 'project-session',
              notification_revision: 1,
              notification_count: 1,
              local_unread_count: 1,
              latest_completion_run_id: 'project-run',
              unread_run_status: 'failed',
              unread_run_at: '2026-07-20T11:00:00+00:00',
            },
          ],
        },
      ],
    }));
    const { chatState, controller } = setupController({
      operationOverrides: { listSessionActivity },
    });

    await controller.refreshAgentActivity(['builder@project-one']);

    expect(listSessionActivity).toHaveBeenCalledWith(['builder@project-one']);
    expect(
      ensureSessionState(chatState, 'builder@project-one', 'project-session'),
    ).toMatchObject({
      notificationCount: 1,
      unreadRunStatus: 'failed',
    });
    expect(chatState.sessions['builder::project-session']).toBeUndefined();
  });

  it('uses the displayed completion as the read boundary', async () => {
    const markSessionRead = vi.fn().mockResolvedValue({
      marked_read: true,
      latest_completion_run_id: 'run-one',
      notification_revision: 2,
      notification_count: 0,
      local_unread_count: 0,
      unread_run_status: null,
      unread_run_at: null,
    });
    const { chatState, controller } = setupController({
      operationOverrides: { markSessionRead },
    });
    const sessionState = ensureSessionState(
      chatState,
      'builder@project-one',
      'session-one',
    );
    sessionState.notificationCount = 1;

    expect(
      await controller.markSessionCompletionRead(sessionState, 'run-one'),
    ).toBe(true);

    expect(markSessionRead).toHaveBeenCalledWith(
      'builder@project-one',
      'session-one',
      'run-one',
    );
    expect(sessionState.notificationCount).toBe(0);
  });

  it('does not let a stale Session list resurrect an acknowledged completion', async () => {
    let resolveList;
    const listSessionActivity = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveList = resolve;
        }),
    );
    const markSessionRead = vi.fn().mockResolvedValue({
      marked_read: true,
      latest_completion_run_id: 'run-one',
      notification_revision: 2,
      notification_count: 0,
      local_unread_count: 0,
      unread_run_status: null,
      unread_run_at: null,
    });
    const { chatState, controller } = setupController({
      operationOverrides: { listSessionActivity, markSessionRead },
    });
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    sessionState.notificationCount = 1;
    const refresh = controller.refreshAgentActivity(['alpha']);

    await controller.markSessionCompletionRead(sessionState, 'run-one');
    resolveList({
      agents: [
        {
          agent_id: 'alpha',
          project_id: null,
          sessions: [
            {
              id: 'session-one',
              notification_revision: 1,
              notification_count: 1,
              local_unread_count: 1,
              latest_completion_run_id: 'run-one',
              unread_run_status: 'completed',
              unread_run_at: '2026-07-20T10:00:00+00:00',
            },
          ],
        },
      ],
    });

    await refresh;
    expect(sessionState.notificationCount).toBe(0);
    expect(sessionState.latestCompletionRunId).toBe('run-one');
  });

  it('retains known activity when the batched refresh fails', async () => {
    const listSessionActivity = vi
      .fn()
      .mockRejectedValue(new Error('activity unavailable'));
    const { chatState, controller } = setupController({
      operationOverrides: { listSessionActivity },
    });
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    sessionState.latestCompletionRunId = 'run-one';
    sessionState.notificationCount = 1;

    await expect(controller.refreshAgentActivity(['alpha'])).resolves.toBe(
      false,
    );

    expect(sessionState.notificationCount).toBe(1);
    expect(chatState.loadingAgentActivity).toBe(false);
    expect(chatState.agentActivityError).toBe('activity unavailable');
  });
});

describe('scoped completion activity', () => {
  it('retries failed displayed activity without reconnecting and stops after disposal', async () => {
    vi.useFakeTimers();
    const { chatState, controller, listSessionActivity } = activitySetup();
    const state = ensureSessionState(chatState, 'alpha', 'session-one');
    state.notificationCount = 1;
    state.latestCompletionRunId = 'run-one';
    listSessionActivity.mockRejectedValueOnce(new Error('unavailable'));
    try {
      controller.syncAgentActivity(['alpha'], 'same-connection');
      await vi.advanceTimersByTimeAsync(0);
      expect(state.notificationCount).toBe(1);
      expect(chatState.agentActivityError).toBe('unavailable');
      await vi.advanceTimersByTimeAsync(1000);
      expect(listSessionActivity).toHaveBeenCalledTimes(2);
      expect(state.notificationCount).toBe(0);
      expect(chatState.agentActivityError).toBe('');
      controller.syncAgentActivity(['alpha'], 'same-connection');
      await vi.advanceTimersByTimeAsync(30000);
      expect(listSessionActivity).toHaveBeenCalledTimes(2);

      listSessionActivity.mockRejectedValue(new Error('unavailable'));
      await controller.refreshAgentActivity(['alpha']);
      await vi.advanceTimersByTimeAsync(1000);
      expect(listSessionActivity).toHaveBeenCalledTimes(4);
      await vi.advanceTimersByTimeAsync(1999);
      expect(listSessionActivity).toHaveBeenCalledTimes(4);
      controller.syncAgentActivity([], 'same-connection');
      expect(chatState.agentActivityError).toBe('');
      await vi.advanceTimersByTimeAsync(30000);
      expect(listSessionActivity).toHaveBeenCalledTimes(4);
      controller.syncAgentActivity(['alpha'], 'same-connection');
      await vi.advanceTimersByTimeAsync(0);
      expect(listSessionActivity).toHaveBeenCalledTimes(5);
      controller.destroy();
      await vi.advanceTimersByTimeAsync(30000);
      expect(listSessionActivity).toHaveBeenCalledTimes(5);
    } finally {
      controller.destroy();
      vi.useRealTimers();
    }
  });

  const unreadRow = (id, runId, at = '2026-07-20T10:00:00+00:00') => ({
    id,
    latest_completion_run_id: runId,
    notification_revision: 1,
    notification_count: 1,
    local_unread_count: 1,
    unread_run_status: 'completed',
    unread_run_at: at,
    notification_run_at: at,
  });

  const activityResponse = (addresses, rows = {}) => ({
    agents: addresses.map((address) => ({
      agent_id: address,
      project_id: null,
      sessions: rows[address] ?? [],
    })),
  });

  const entry = (id, scope) => ({ id, scope });

  function activitySetup(rows = {}, options = {}) {
    const listSessionActivity = vi.fn(async (addresses) =>
      activityResponse(addresses, rows),
    );
    return {
      listSessionActivity,
      ...setupController({
        ...options,
        operationOverrides: { listSessionActivity },
      }),
    };
  }

  it('reads every displayed address once and only joining addresses later', async () => {
    const { controller, listSessionActivity } = activitySetup();

    controller.syncAgentActivity(['alpha', 'beta'], 'full-1');
    await vi.waitFor(() => expect(listSessionActivity).toHaveBeenCalledOnce());
    controller.syncAgentActivity(['alpha', 'beta'], 'full-1');
    controller.syncAgentActivity(['alpha', 'beta', 'gamma'], 'full-1');
    await vi.waitFor(() =>
      expect(listSessionActivity).toHaveBeenLastCalledWith(['gamma']),
    );
    controller.syncAgentActivity(['alpha', 'beta', 'gamma'], 'full-2');
    await vi.waitFor(() =>
      expect(listSessionActivity).toHaveBeenLastCalledWith([
        'alpha',
        'beta',
        'gamma',
      ]),
    );

    expect(listSessionActivity.mock.calls).toEqual([
      [['alpha', 'beta']],
      [['gamma']],
      [['alpha', 'beta', 'gamma']],
    ]);
  });

  it('applies retained terminal facts and refreshes counts after read and settings invalidations', async () => {
    vi.useFakeTimers();
    try {
      const runEvents = createRunEventFeed();
      const { chatState, controller, listSessionActivity } = activitySetup(
        {},
        { runEvents },
      );
      controller.syncAgentActivity(['alpha'], 'full');
      await vi.runAllTimersAsync();
      controller.applySessionInvalidations([]);
      listSessionActivity.mockClear();
      const sessionState = ensureSessionState(chatState, 'alpha', 'one');
      sessionState.latestCompletionRunId = 'run-one';
      sessionState.notificationCount = 1;
      const terminalScope = {
        project_id: null,
        agent_id: 'alpha',
        session_id: 'one',
        run_id: 'run-one',
      };
      runEvents.append({
        type: 'run_completed',
        payload: { run_id: 'run-one' },
      });

      listSessionActivity.mockResolvedValue({
        agents: [
          {
            agent_id: 'alpha',
            sessions: [
              {
                id: 'one',
                latest_completion_run_id: 'run-one',
                notification_revision: 2,
                notification_count: 0,
                local_unread_count: 0,
              },
            ],
          },
        ],
      });
      controller.applySessionInvalidations([
        entry(1, terminalScope),
        entry(2, { agent_id: 'alpha', session_id: 'one' }),
        entry(3, {
          project_id: null,
          agent_id: 'alpha',
          session_id: 'one',
          read_run_id: 'run-one',
        }),
      ]);
      await vi.runAllTimersAsync();

      expect(listSessionActivity).toHaveBeenCalledExactlyOnceWith(['alpha']);
      expect(sessionState).toMatchObject({
        latestCompletionRunId: 'run-one',
        notificationCount: 0,
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it('adopts the window it was created with instead of replaying it', async () => {
    vi.useFakeTimers();
    try {
      const { controller, listSessionActivity } = activitySetup();
      controller.syncAgentActivity(['alpha'], 'full');
      await vi.runAllTimersAsync();
      listSessionActivity.mockClear();

      controller.applySessionInvalidations([entry(7, null)]);
      await vi.runAllTimersAsync();

      expect(listSessionActivity).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it('coalesces unmatched facts into one read of the named displayed Agents', async () => {
    vi.useFakeTimers();
    try {
      const { chatState, controller, listSessionActivity } = activitySetup({
        alpha: [unreadRow('one', 'run-newer', '2026-07-20T12:00:00+00:00')],
        beta: [unreadRow('two', 'run-two')],
      });
      controller.syncAgentActivity(['alpha', 'beta', 'gamma'], 'full');
      await vi.runAllTimersAsync();
      controller.applySessionInvalidations([]);
      listSessionActivity.mockClear();
      const held = ensureSessionState(chatState, 'alpha', 'one');
      held.latestCompletionRunId = 'run-newer';
      held.notificationCount = 1;
      held.unreadRunAt = '2026-07-20T12:00:00+00:00';

      controller.applySessionInvalidations([
        // App's feed no longer holds the terminal event: read Beta.
        entry(1, { agent_id: 'beta', session_id: 'two', run_id: 'run-two' }),
        // A read of another Run than the held one: read Alpha.
        entry(2, {
          agent_id: 'alpha',
          session_id: 'one',
          read_run_id: 'run-old',
        }),
        // Not displayed here: nothing to read.
        entry(3, { agent_id: 'delta', session_id: 'x', run_id: 'run-x' }),
      ]);
      expect(listSessionActivity).not.toHaveBeenCalled();
      await vi.runAllTimersAsync();

      expect(listSessionActivity.mock.calls).toEqual([[['beta', 'alpha']]]);
      // Acknowledging an older Run leaves the newer unread result.
      expect(held.notificationCount).toBe(1);
      expect(ensureSessionState(chatState, 'beta', 'two')).toMatchObject({
        notificationCount: 1,
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it('reads every displayed address after an unscoped signal or a dropped window', async () => {
    vi.useFakeTimers();
    try {
      const { controller, listSessionActivity } = activitySetup();
      controller.syncAgentActivity(['alpha', 'beta'], 'full');
      await vi.runAllTimersAsync();
      controller.applySessionInvalidations([entry(1, null)]);
      listSessionActivity.mockClear();

      controller.applySessionInvalidations([entry(1, null), entry(2, null)]);
      await vi.runAllTimersAsync();
      controller.applySessionInvalidations([
        entry(9, { agent_id: 'alpha', session_id: 'one' }),
      ]);
      await vi.runAllTimersAsync();

      expect(listSessionActivity.mock.calls).toEqual([
        [['alpha', 'beta']],
        [['alpha', 'beta']],
      ]);
    } finally {
      vi.useRealTimers();
    }
  });

  it('drops the unread marker of a deleted Session', () => {
    const { chatState, controller } = activitySetup();
    controller.applySessionInvalidations([]);
    const sessionState = ensureSessionState(chatState, 'builder@vbot', 'gone');
    sessionState.latestCompletionRunId = 'run-one';
    sessionState.notificationCount = 1;

    controller.applySessionInvalidations([
      entry(1, {
        agent_id: 'builder',
        project_id: 'vbot',
        deleted_session_id: 'gone',
        next_session_id: 'next',
      }),
    ]);

    expect(sessionState.notificationCount).toBe(0);
    expect(sessionState.latestCompletionRunId).toBe('');
  });

  it.each([
    {
      fact: 'a deletion',
      scope: { agent_id: 'worker', deleted_session_id: 'gone' },
      applied: true,
    },
    {
      fact: 'a read acknowledgement',
      scope: { agent_id: 'worker', session_id: 'one', read_run_id: 'run-one' },
      applied: true,
    },
    {
      fact: 'a finished Run of a displayed Agent',
      scope: { agent_id: 'alpha', session_id: 'one', run_id: 'run-one' },
      applied: true,
    },
    {
      fact: 'a finished Run of another Agent',
      scope: { agent_id: 'worker', session_id: 'one', run_id: 'run-one' },
      applied: false,
    },
  ])(
    'applies deferred Run events before $fact only where it matters',
    async ({ scope, applied }) => {
      vi.useFakeTimers();
      try {
        const runEvents = createRunEventFeed();
        const { controller, runStream } = activitySetup({}, { runEvents });
        controller.syncAgentActivity(['alpha'], 'full');
        await vi.runAllTimersAsync();
        controller.applySessionInvalidations([]);
        const terminal = runServerEvent('run_completed', {
          run_id: 'run-one',
          agent_id: 'worker',
          session_id: 'one',
        });
        runEvents.append(terminal);
        controller.handleRunEvents({ deferred: true });

        controller.applySessionInvalidations([entry(1, scope)]);

        expect(runStream.handleServerEvent.mock.calls).toEqual(
          applied ? [[terminal]] : [],
        );
      } finally {
        vi.useRealTimers();
      }
    },
  );

  it('applies deferred Run events before reading activity', async () => {
    const runEvents = createRunEventFeed();
    const { controller, listSessionActivity, runStream } = activitySetup(
      {},
      { runEvents },
    );
    runEvents.append(runServerEvent('run_completed', { run_id: 'run-one' }));
    controller.handleRunEvents({ deferred: true });

    await controller.refreshAgentActivity(['alpha']);

    const [applied] = runStream.handleServerEvent.mock.invocationCallOrder;
    const [read] = listSessionActivity.mock.invocationCallOrder;
    expect(applied).toBeLessThan(read);
  });

  it('keeps a completion that arrived while the read was in flight', async () => {
    const pending = deferred();
    const listSessionActivity = vi.fn(() => pending.promise);
    const { chatState, controller } = setupController({
      operationOverrides: { listSessionActivity },
    });
    const stale = ensureSessionState(chatState, 'alpha', 'stale');
    stale.latestCompletionRunId = 'run-archived';
    const fresh = ensureSessionState(chatState, 'alpha', 'fresh');

    const refresh = controller.refreshAgentActivity(['alpha']);
    await Promise.resolve();
    fresh.latestCompletionRunId = 'run-live';
    fresh.notificationCount = 1;
    fresh.notificationRevision = 1;
    pending.resolve(activityResponse(['alpha']));
    await refresh;

    // The server snapshot predates Run live; an omitted Session whose
    // completion did not change has none.
    expect(fresh.notificationCount).toBe(1);
    expect(stale.latestCompletionRunId).toBe('');
  });

  it('uses the persisted revision to order terminal events and activity reads', async () => {
    const rows = {
      alpha: [{ ...unreadRow('one', 'run-new'), notification_revision: 3 }],
    };
    const { chatState, controller } = activitySetup(rows);
    await controller.refreshAgentActivity(['alpha']);
    const sessionState = ensureSessionState(chatState, 'alpha', 'one');
    appendRunEvent(sessionState, {
      type: 'run_completed',
      run_id: 'run-old',
      sequence: 1,
      payload: {
        status: 'completed',
        notification_activity: {
          ...unreadRow('one', 'run-old'),
          notification_revision: 2,
          notification_count: 0,
          local_unread_count: 0,
        },
      },
    });
    expect(sessionState.notificationCount).toBe(1);
    expect(sessionState.latestCompletionRunId).toBe('run-new');
    controller.destroy();
  });

  it('accepts a replacement Session and rejects outstanding results from its prior generation', async () => {
    const previous = {
      ...unreadRow('one', 'run-previous'),
      notification_generation_id: 'previous',
      notification_revision: 90,
    };
    const replacement = {
      ...unreadRow('one', 'run-replacement'),
      notification_generation_id: 'replacement',
    };
    const pending = deferred();
    const rows = { alpha: [previous] };
    const listSessionActivity = vi.fn(async () =>
      activityResponse(['alpha'], rows),
    );
    const { chatState, controller } = setupController({
      operationOverrides: {
        listSessionActivity,
        markSessionRead: () => pending.promise,
      },
    });
    await controller.refreshAgentActivity(['alpha']);
    const state = ensureSessionState(chatState, 'alpha', 'one');
    const receipt = controller.markSessionCompletionRead(state, 'run-previous');

    rows.alpha = [replacement];
    await controller.refreshAgentActivity(['alpha']);
    pending.resolve({
      ...previous,
      notification_revision: 91,
      notification_count: 0,
      local_unread_count: 0,
      marked_read: true,
    });
    await receipt;
    appendRunEvent(state, {
      type: 'run_completed',
      run_id: 'run-late-previous',
      sequence: 2,
      payload: {
        notification_activity: {
          ...previous,
          notification_revision: 92,
          notification_count: 2,
        },
      },
    });

    expect(state).toMatchObject({
      notificationGeneration: 'replacement',
      notificationRevision: 1,
      notificationCount: 1,
    });
    controller.destroy();
  });
});

describe('Agent activity projection', () => {
  it('derives Agent status from Session activity and counts unread results of idle Sessions only', () => {
    const chatState = createChatState();
    const olderUnread = ensureSessionState(chatState, 'alpha', 'session-older');
    const newerUnread = ensureSessionState(chatState, 'alpha', 'session-newer');
    olderUnread.notificationCount = 1;
    olderUnread.notificationRunAt = '2026-07-20T10:00:00+00:00';
    newerUnread.notificationCount = 1;
    newerUnread.notificationRunAt = '2026-07-20T10:05:00+00:00';
    const row = (sessionId, state, extra = {}) => ({
      project_id: null,
      agent_id: 'alpha',
      session_id: sessionId,
      state,
      work_started_at: '2026-07-20T10:06:00+00:00',
      ...extra,
    });

    expect(agentActivityStatus(chatState, 'alpha')).toBe('unread');
    expect(newestUnreadSessionForAgent(chatState, 'alpha')).toBe(newerUnread);

    // The newer result's Session waits for its Sub-Agent: not finished yet.
    chatState.sessionActivity = sessionActivityFromSnapshot([
      row('session-newer', 'waiting', { waiting_for: { subagents: 1 } }),
    ]);
    expect(agentActivityStatus(chatState, 'alpha')).toBe('waiting');
    expect(agentUnreadResults(chatState, 'alpha').count).toBe(1);
    expect(newestUnreadSessionForAgent(chatState, 'alpha')).toBe(olderUnread);

    // Working wins over waiting, unless the work leaves Agent activity out.
    const excluded = applySessionActivityChange(
      chatState.sessionActivity,
      row('session-system', 'working', {
        contributes_to_agent_activity: false,
      }),
    );
    chatState.sessionActivity = excluded;
    expect(agentActivityStatus(chatState, 'alpha')).toBe('waiting');
    chatState.sessionActivity = applySessionActivityChange(
      excluded,
      row('session-running', 'working'),
    );
    expect(agentActivityStatus(chatState, 'alpha')).toBe('running');

    // Idle removes a Session's entry; its result counts again.
    chatState.sessionActivity = applySessionActivityChange(
      applySessionActivityChange(
        chatState.sessionActivity,
        row('session-running', 'idle'),
      ),
      row('session-newer', 'idle'),
    );
    expect(agentActivityStatus(chatState, 'alpha')).toBe('unread');
    expect(agentUnreadResults(chatState, 'alpha').count).toBe(2);
  });

  it('does not project the displayed Session unread while preserving other unread results', () => {
    const chatState = createChatState();
    const displayed = ensureSessionState(chatState, 'alpha', 'session-shown');
    const background = ensureSessionState(
      chatState,
      'alpha',
      'session-background',
    );
    displayed.notificationCount = 1;

    expect(agentActivityStatus(chatState, 'alpha', displayed.key)).toBe('idle');

    background.notificationCount = 1;
    expect(agentActivityStatus(chatState, 'alpha', displayed.key)).toBe(
      'unread',
    );
  });

  it('counts an Agent unread results with the newest result time, excluding the displayed Session', () => {
    const chatState = createChatState();
    const displayed = ensureSessionState(chatState, 'alpha', 'session-shown');
    const older = ensureSessionState(chatState, 'alpha', 'session-older');
    const newer = ensureSessionState(chatState, 'alpha', 'session-newer');
    const otherAgent = ensureSessionState(chatState, 'beta', 'session-beta');
    for (const [sessionState, at] of [
      [displayed, '2026-07-20T10:09:00+00:00'],
      [older, '2026-07-20T10:00:00+00:00'],
      [newer, '2026-07-20T10:05:00+00:00'],
      [otherAgent, '2026-07-20T10:07:00+00:00'],
    ]) {
      sessionState.notificationCount = 1;
      sessionState.notificationRunAt = at;
    }

    expect(agentUnreadResults(chatState, 'alpha', displayed.key)).toEqual({
      count: 2,
      latestAt: Date.parse('2026-07-20T10:05:00+00:00'),
    });
    expect(agentUnreadResults(chatState, 'alpha').count).toBe(3);
    expect(agentUnreadResults(chatState, 'gamma')).toEqual({
      count: 0,
      latestAt: 0,
    });
  });

  it.each([
    ['run_completed', 'completed'],
    ['run_interrupted', 'interrupted'],
  ])(
    'keeps an excluded Run out of unread results through %s while retaining its timeline',
    (terminalType, terminalStatus) => {
      const chatState = createChatState();
      const sessionState = ensureSessionState(
        chatState,
        'alpha',
        'session-system',
      );

      appendRunEvent(sessionState, {
        type: 'run_started',
        run_id: 'run-system',
        sequence: 1,
        contributes_to_agent_activity: false,
        payload: { status: 'running' },
      });

      expect(isRunActive(sessionState)).toBe(true);
      expect(sessionState.currentRun?.contributesToAgentActivity).toBe(false);

      appendRunEvent(sessionState, {
        type: terminalType,
        run_id: 'run-system',
        sequence: 2,
        contributes_to_agent_activity: false,
        payload: { status: terminalStatus },
      });
      applyTimelineEvent(sessionState, 'timeline_snapshot', {
        run_id: 'run-system',
        sequence: 2,
        items: [assistantRun('run-system', [], { status: terminalStatus })],
      });

      expect(sessionState.status).toBe(terminalStatus);
      expect(sessionState.notificationCount).toBe(0);
      expect(sessionState.latestCompletionRunId).toBe('');
      expect(visibleTimelineItemsForRender(sessionState)).toEqual([
        expect.objectContaining({
          type: 'assistant_run',
          runId: 'run-system',
          status: terminalStatus,
        }),
      ]);
    },
  );
});

describe('Session History invalidations', () => {
  it('re-reads a displayed Session once when records were appended outside a Run', async () => {
    const loadChatHistory = vi
      .fn()
      .mockResolvedValue({ messages: [], active_run: null });
    const { controller } = setupController({
      operationOverrides: { loadChatHistory },
      isDisplayedSession: (agentId, sessionId) =>
        agentId === 'alpha' && sessionId === 'one',
    });
    await controller.loadHistoryForSession('alpha', 'one');
    controller.applySessionInvalidations([]);
    loadChatHistory.mockClear();

    const appended = {
      agent_id: 'alpha',
      session_id: 'one',
      history: 'appended',
    };
    controller.applySessionInvalidations([
      { id: 1, scope: appended },
      { id: 2, scope: appended },
      // Not displayed, or no appended records: nothing to read.
      { id: 3, scope: { ...appended, session_id: 'two' } },
      { id: 4, scope: { agent_id: 'alpha', session_id: 'one' } },
    ]);

    await vi.waitFor(() => expect(loadChatHistory).toHaveBeenCalledOnce());
    expect(loadChatHistory).toHaveBeenCalledWith(
      expect.objectContaining({ agent_id: 'alpha', session_id: 'one' }),
    );
  });
});
