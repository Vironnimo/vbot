import { describe, expect, it, vi } from 'vitest';
import {
  agentActivityStatus,
  agentUnreadResults,
  appendRunEvent,
  createChatState,
  ensureSessionState,
  isRunActive,
  newestUnreadSessionForAgent,
  startRun,
  visibleTimelineItemsForRender,
} from '../chatState.js';
import { deferred, setupController } from './chatState.support.js';

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
        { name: 'review', type: 'skill', description: 'Review code' },
      ],
    });
    const { chatState, controller } = setupController({
      operationOverrides: { listChatCommands },
    });

    await controller.loadCommands('alpha');

    expect(listChatCommands).toHaveBeenCalledWith({ agent_id: 'alpha' });
    expect(chatState.availableSkills).toMatchObject([
      { name: 'help', type: 'command' },
      { name: 'review', type: 'skill' },
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
              has_unread_completion: true,
              unread_run_id: 'run-one',
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
        hasUnreadCompletion: true,
        unreadRunId: 'run-one',
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
              has_unread_completion: true,
              unread_run_id: 'project-run',
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
      hasUnreadCompletion: true,
      unreadRunId: 'project-run',
      unreadRunStatus: 'failed',
    });
    expect(chatState.sessions['builder::project-session']).toBeUndefined();
  });

  it('acknowledges only the exact completion rendered in the Session', async () => {
    const markSessionRead = vi.fn().mockResolvedValue({
      marked_read: true,
      latest_completion_run_id: 'run-one',
      has_unread_completion: false,
      unread_run_id: null,
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
    sessionState.hasUnreadCompletion = true;
    sessionState.unreadRunId = 'run-one';

    expect(await controller.markSessionCompletionRead(sessionState)).toBe(true);

    expect(markSessionRead).toHaveBeenCalledWith(
      'builder@project-one',
      'session-one',
      'run-one',
    );
    expect(sessionState.hasUnreadCompletion).toBe(false);
    expect(sessionState.unreadRunId).toBe('');
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
      has_unread_completion: false,
      unread_run_id: null,
      unread_run_status: null,
      unread_run_at: null,
    });
    const { chatState, controller } = setupController({
      operationOverrides: { listSessionActivity, markSessionRead },
    });
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    sessionState.hasUnreadCompletion = true;
    sessionState.unreadRunId = 'run-one';
    const refresh = controller.refreshAgentActivity(['alpha']);

    await controller.markSessionCompletionRead(sessionState);
    resolveList({
      agents: [
        {
          agent_id: 'alpha',
          project_id: null,
          sessions: [
            {
              id: 'session-one',
              has_unread_completion: true,
              unread_run_id: 'run-one',
              unread_run_status: 'completed',
              unread_run_at: '2026-07-20T10:00:00+00:00',
            },
          ],
        },
      ],
    });

    await refresh;
    expect(sessionState.hasUnreadCompletion).toBe(false);
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
    sessionState.hasUnreadCompletion = true;
    sessionState.unreadRunId = 'run-one';

    await expect(controller.refreshAgentActivity(['alpha'])).resolves.toBe(
      false,
    );

    expect(sessionState.hasUnreadCompletion).toBe(true);
    expect(sessionState.unreadRunId).toBe('run-one');
    expect(chatState.loadingAgentActivity).toBe(false);
    expect(chatState.agentActivityError).toBe('activity unavailable');
  });
});

describe('scoped completion activity', () => {
  const unreadRow = (id, runId, at = '2026-07-20T10:00:00+00:00') => ({
    id,
    latest_completion_run_id: runId,
    has_unread_completion: true,
    unread_run_id: runId,
    unread_run_status: 'completed',
    unread_run_at: at,
  });

  const activityResponse = (addresses, rows = {}) => ({
    agents: addresses.map((address) => ({
      agent_id: address,
      project_id: null,
      sessions: rows[address] ?? [],
    })),
  });

  const entry = (id, scope) => ({ id, scope });

  function activitySetup(rows = {}) {
    const listSessionActivity = vi.fn(async (addresses) =>
      activityResponse(addresses, rows),
    );
    return {
      listSessionActivity,
      ...setupController({ operationOverrides: { listSessionActivity } }),
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

  it('applies terminal and read facts it already holds without reading', async () => {
    vi.useFakeTimers();
    try {
      const { chatState, controller, listSessionActivity } = activitySetup();
      controller.syncAgentActivity(['alpha'], 'full');
      await vi.runAllTimersAsync();
      controller.applySessionInvalidations([], []);
      listSessionActivity.mockClear();
      const sessionState = ensureSessionState(chatState, 'alpha', 'one');
      sessionState.latestCompletionRunId = 'run-one';
      sessionState.hasUnreadCompletion = true;
      sessionState.unreadRunId = 'run-one';
      const terminalScope = {
        project_id: null,
        agent_id: 'alpha',
        session_id: 'one',
        run_id: 'run-one',
      };
      const runServerEvents = [
        { type: 'run_completed', payload: { run_id: 'run-one' } },
      ];

      controller.applySessionInvalidations(
        [
          entry(1, terminalScope),
          entry(2, { agent_id: 'alpha', session_id: 'one' }),
          entry(3, {
            project_id: null,
            agent_id: 'alpha',
            session_id: 'one',
            read_run_id: 'run-one',
          }),
        ],
        runServerEvents,
      );
      await vi.runAllTimersAsync();

      expect(listSessionActivity).not.toHaveBeenCalled();
      expect(sessionState).toMatchObject({
        latestCompletionRunId: 'run-one',
        hasUnreadCompletion: false,
        unreadRunId: '',
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

      controller.applySessionInvalidations([entry(7, null)], []);
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
      controller.applySessionInvalidations([], []);
      listSessionActivity.mockClear();
      const held = ensureSessionState(chatState, 'alpha', 'one');
      held.latestCompletionRunId = 'run-newer';
      held.hasUnreadCompletion = true;
      held.unreadRunId = 'run-newer';
      held.unreadRunAt = '2026-07-20T12:00:00+00:00';

      controller.applySessionInvalidations(
        [
          // The terminal event left the retained window: read Beta.
          entry(1, { agent_id: 'beta', session_id: 'two', run_id: 'run-two' }),
          // A read of another Run than the held one: read Alpha.
          entry(2, {
            agent_id: 'alpha',
            session_id: 'one',
            read_run_id: 'run-old',
          }),
          // Not displayed here: nothing to read.
          entry(3, { agent_id: 'delta', session_id: 'x', run_id: 'run-x' }),
        ],
        [],
      );
      expect(listSessionActivity).not.toHaveBeenCalled();
      await vi.runAllTimersAsync();

      expect(listSessionActivity.mock.calls).toEqual([[['beta', 'alpha']]]);
      // Acknowledging an older Run leaves the newer unread result.
      expect(held.hasUnreadCompletion).toBe(true);
      expect(held.unreadRunId).toBe('run-newer');
      expect(ensureSessionState(chatState, 'beta', 'two')).toMatchObject({
        hasUnreadCompletion: true,
        unreadRunId: 'run-two',
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
      controller.applySessionInvalidations([entry(1, null)], []);
      listSessionActivity.mockClear();

      controller.applySessionInvalidations(
        [entry(1, null), entry(2, null)],
        [],
      );
      await vi.runAllTimersAsync();
      controller.applySessionInvalidations(
        [entry(9, { agent_id: 'alpha', session_id: 'one' })],
        [],
      );
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
    controller.applySessionInvalidations([], []);
    const sessionState = ensureSessionState(chatState, 'builder@vbot', 'gone');
    sessionState.latestCompletionRunId = 'run-one';
    sessionState.hasUnreadCompletion = true;
    sessionState.unreadRunId = 'run-one';

    controller.applySessionInvalidations(
      [
        entry(1, {
          agent_id: 'builder',
          project_id: 'vbot',
          deleted_session_id: 'gone',
          next_session_id: 'next',
        }),
      ],
      [],
    );

    expect(sessionState.hasUnreadCompletion).toBe(false);
    expect(sessionState.latestCompletionRunId).toBe('');
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
    fresh.hasUnreadCompletion = true;
    fresh.unreadRunId = 'run-live';
    pending.resolve(activityResponse(['alpha']));
    await refresh;

    // The server snapshot predates Run live; an omitted Session whose
    // completion did not change has none.
    expect(fresh.hasUnreadCompletion).toBe(true);
    expect(fresh.unreadRunId).toBe('run-live');
    expect(stale.latestCompletionRunId).toBe('');
  });

  // A listed completion and a local terminal event can report the same or
  // different Runs in either order; the exact read state and the newer
  // completion win.
  describe('merging listed and local completions', () => {
    const readRow = (runId, at = '2026-07-20T10:00:00+00:00') => ({
      id: 'one',
      last_active_at: at,
      latest_completion_run_id: runId,
      has_unread_completion: false,
      unread_run_id: null,
      unread_run_status: null,
      unread_run_at: null,
    });
    const listed = (row) => ({ listed: row });
    const completed = (runId, at = '2026-07-20T10:00:00+00:00') => ({
      completed: { runId, at },
    });
    const later = '2026-07-20T10:05:00+00:00';

    it.each([
      {
        name: 'accepts the exact backend read state after a local terminal event',
        steps: [completed('run-one'), listed(readRow('run-one'))],
        unreadRunId: '',
      },
      {
        name: 'keeps the exact backend read state when its terminal event replays',
        steps: [listed(readRow('run-one')), completed('run-one')],
        unreadRunId: '',
      },
      {
        name: 'keeps the exact backend read state against a stale unread listing',
        steps: [
          listed(readRow('run-one')),
          listed(unreadRow('one', 'run-one')),
        ],
        unreadRunId: '',
      },
      {
        name: 'keeps a newer local completion against an older read listing',
        steps: [completed('run-new', later), listed(readRow('run-old'))],
        unreadRunId: 'run-new',
      },
      {
        name: 'keeps a local completion against a different listing with the same time',
        steps: [
          completed('run-local', later),
          listed(unreadRow('one', 'run-listing', later)),
        ],
        unreadRunId: 'run-local',
      },
      {
        name: 'accepts a genuinely newer unread listing',
        steps: [
          listed(readRow('run-old')),
          listed(unreadRow('one', 'run-new', later)),
        ],
        unreadRunId: 'run-new',
      },
    ])('$name', async ({ steps, unreadRunId }) => {
      const rows = { alpha: [] };
      const { chatState, controller } = activitySetup(rows);
      const sessionState = ensureSessionState(chatState, 'alpha', 'one');
      let sequence = 0;
      for (const step of steps) {
        if (step.listed) {
          rows.alpha = [step.listed];
          await controller.refreshAgentActivity(['alpha']);
        } else {
          sequence += 1;
          appendRunEvent(sessionState, {
            type: 'run_completed',
            run_id: step.completed.runId,
            sequence,
            timestamp: step.completed.at,
            payload: { status: 'completed' },
          });
        }
      }

      expect(sessionState.hasUnreadCompletion).toBe(Boolean(unreadRunId));
      expect(sessionState.unreadRunId).toBe(unreadRunId);
      controller.destroy();
    });
  });
});

describe('Agent activity projection', () => {
  it('prioritizes a running Session over unread results and finds the newest unread Session', () => {
    const chatState = createChatState();
    const runningSession = ensureSessionState(
      chatState,
      'alpha',
      'session-running',
    );
    const olderUnread = ensureSessionState(chatState, 'alpha', 'session-older');
    const newerUnread = ensureSessionState(chatState, 'alpha', 'session-newer');
    olderUnread.hasUnreadCompletion = true;
    olderUnread.unreadRunId = 'run-old';
    olderUnread.unreadRunAt = '2026-07-20T10:00:00+00:00';
    newerUnread.hasUnreadCompletion = true;
    newerUnread.unreadRunId = 'run-new';
    newerUnread.unreadRunAt = '2026-07-20T10:05:00+00:00';
    startRun(runningSession, {
      run_id: 'run-live',
      status: 'running',
    });

    expect(agentActivityStatus(chatState, 'alpha')).toBe('running');
    expect(newestUnreadSessionForAgent(chatState, 'alpha')).toBe(newerUnread);
  });

  it('does not project the displayed Session unread while preserving other unread results', () => {
    const chatState = createChatState();
    const displayed = ensureSessionState(chatState, 'alpha', 'session-shown');
    const background = ensureSessionState(
      chatState,
      'alpha',
      'session-background',
    );
    displayed.hasUnreadCompletion = true;

    expect(agentActivityStatus(chatState, 'alpha', displayed.key)).toBe('idle');

    background.hasUnreadCompletion = true;
    expect(agentActivityStatus(chatState, 'alpha', displayed.key)).toBe(
      'unread',
    );
  });

  it('counts an Agent unread results with the newest result time, excluding the displayed Session', () => {
    const chatState = createChatState();
    const displayed = ensureSessionState(chatState, 'alpha', 'session-shown');
    const older = ensureSessionState(chatState, 'alpha', 'session-older');
    const newer = ensureSessionState(chatState, 'alpha', 'session-newer');
    const withoutRun = ensureSessionState(chatState, 'alpha', 'session-norun');
    const otherAgent = ensureSessionState(chatState, 'beta', 'session-beta');
    for (const [sessionState, runId, at] of [
      [displayed, 'run-shown', '2026-07-20T10:09:00+00:00'],
      [older, 'run-old', '2026-07-20T10:00:00+00:00'],
      [newer, 'run-new', '2026-07-20T10:05:00+00:00'],
      [otherAgent, 'run-beta', '2026-07-20T10:07:00+00:00'],
    ]) {
      sessionState.hasUnreadCompletion = true;
      sessionState.unreadRunId = runId;
      sessionState.unreadRunAt = at;
    }
    // Navigation cannot land on an unread flag without its Run id.
    withoutRun.hasUnreadCompletion = true;

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
    'keeps an excluded Run out of Agent activity through %s while retaining its timeline',
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
      expect(agentActivityStatus(chatState, 'alpha')).toBe('idle');

      appendRunEvent(sessionState, {
        type: terminalType,
        run_id: 'run-system',
        sequence: 2,
        contributes_to_agent_activity: false,
        payload: { status: terminalStatus },
      });

      expect(sessionState.status).toBe(terminalStatus);
      expect(sessionState.hasUnreadCompletion).toBe(false);
      expect(sessionState.latestCompletionRunId).toBe('');
      expect(agentActivityStatus(chatState, 'alpha')).toBe('idle');
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
