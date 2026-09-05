import { describe, expect, it, vi } from 'vitest';

import {
  appendRunEvent,
  createChatController,
  createChatState,
  ensureSessionState,
  startRun,
  visibleTimelineItemsForRender,
} from '../chatState.js';

function setup({
  operationOverrides = {},
  isDisplayedSession = () => false,
  shouldLoadCurrentHistory = () => true,
} = {}) {
  const chatState = createChatState();
  const runStream = {
    applyConnectionSnapshot: vi.fn(),
    attachRunStream: vi.fn(),
    closeSubscriptionFor: vi.fn(),
    closeSubscriptions: vi.fn(),
    closeSubscriptionsExcept: vi.fn(),
    handleServerEvents: vi.fn(),
    mergeRunResponse: vi.fn(),
    subscribeToRun: vi.fn(),
  };
  const listQueue = vi.fn().mockResolvedValue({
    items: [{ id: 'queued-one', content: 'Next', editable: true }],
  });
  const onRestartQueueDiscarded = vi.fn();
  const controller = createChatController({
    chatState,
    runStream,
    operations: {
      listQueue,
      loadReflectionRuns: vi.fn().mockResolvedValue({ reflection_runs: [] }),
      ...operationOverrides,
    },
    translate: (_key, fallback) => fallback,
    isDisplayedSession,
    shouldLoadCurrentHistory,
    onRestartQueueDiscarded,
  });
  return {
    chatState,
    controller,
    listQueue,
    onRestartQueueDiscarded,
    runStream,
  };
}

describe('chat controller', () => {
  const review = (status = 'completed', runId = 'review-one') => ({
    run_id: runId,
    session_id: 'review-session',
    run_kind: 'memory_reflection',
    status,
    started_at: '2026-09-05T10:00:00Z',
  });

  it('restores completed reflections on fresh history load without touching another Session', async () => {
    const { chatState, controller } = setup({
      operationOverrides: {
        loadChatHistory: vi
          .fn()
          .mockResolvedValue({ messages: [], reflection_runs: [review()] }),
      },
    });
    const other = ensureSessionState(chatState, 'alpha', 'other');
    await controller.loadHistoryForSession('alpha', 'source');
    expect(
      chatState.sessions['alpha::source'].reflectionTasks['review-one'],
    ).toEqual({
      sessionId: 'review-session',
      runKind: 'memory_reflection',
      status: 'completed',
      startedAt: '2026-09-05T10:00:00Z',
    });
    expect(other.reflectionTasks).toEqual({});
  });

  it('recovers a reflection completed while disconnected and deduplicates connection snapshots', async () => {
    const loadReflectionRuns = vi
      .fn()
      .mockResolvedValue({ reflection_runs: [review()] });
    const { chatState, controller } = setup({
      operationOverrides: { loadReflectionRuns },
      isDisplayedSession: (agent, session) =>
        agent === 'alpha@project' && session === 'source',
    });
    const source = ensureSessionState(chatState, 'alpha@project', 'source');
    const snapshot = { active_runs: [], queues: [] };
    controller.applyConnectionSnapshot(snapshot);
    controller.applyConnectionSnapshot(snapshot);
    await vi.waitFor(() =>
      expect(source.reflectionTasks['review-one']?.status).toBe('completed'),
    );
    expect(loadReflectionRuns).toHaveBeenCalledExactlyOnceWith({
      agent_id: 'alpha@project',
      session_id: 'source',
    });
  });

  it('keeps newer live results when a reflection restore response arrives late', async () => {
    const response = deferred();
    const { chatState, controller } = setup({
      operationOverrides: {
        loadChatHistory: vi.fn().mockReturnValue(response.promise),
      },
    });
    const source = ensureSessionState(chatState, 'alpha', 'source');
    const loading = controller.loadHistoryForSession('alpha', 'source');
    const terminal = {
      sessionId: 'review-session',
      runKind: 'memory_reflection',
      status: 'failed',
      startedAt: '2026-09-05T10:00:00Z',
    };
    source.reflectionTasks = {
      'review-one': terminal,
      'new-review': { ...terminal, status: 'running' },
    };
    response.resolve({ messages: [], reflection_runs: [review('running')] });
    await loading;
    expect(source.reflectionTasks['review-one']).toBe(terminal);
    expect(source.reflectionTasks['new-review'].status).toBe('running');
  });

  it('ignores an obsolete reconnect response and removes deleted review rows', async () => {
    const response = deferred();
    const { chatState, controller } = setup({
      operationOverrides: {
        loadReflectionRuns: vi.fn().mockReturnValue(response.promise),
        loadChatHistory: vi
          .fn()
          .mockResolvedValue({ messages: [], reflection_runs: [] }),
      },
      isDisplayedSession: () => true,
    });
    const source = ensureSessionState(chatState, 'alpha', 'source');
    source.reflectionTasks = {
      stale: { sessionId: 'deleted', status: 'completed' },
    };
    controller.applyConnectionSnapshot({ active_runs: [] });
    await controller.loadHistoryForSession('alpha', 'source');
    response.resolve({ reflection_runs: [review('running')] });
    await Promise.resolve();
    expect(source.reflectionTasks).toEqual({});
  });

  it('reloads a stale active-run snapshot without replacing a newer live Run', async () => {
    const older = deferred();
    const loadChatHistory = vi
      .fn()
      .mockReturnValueOnce(older.promise)
      .mockResolvedValueOnce({
        messages: [{ id: 'new-user', role: 'user', content: 'New question' }],
        active_run: { run_id: 'new-run', status: 'running' },
      });
    const { chatState, controller, runStream } = setup({
      operationOverrides: { loadChatHistory },
      isDisplayedSession: () => true,
    });
    const session = ensureSessionState(chatState, 'alpha', 'session-one');
    startRun(session, { run_id: 'old-run' });
    const loading = controller.loadHistoryForSession('alpha', 'session-one');
    startRun(session, { run_id: 'new-run' });
    appendRunEvent(session, {
      run_id: 'new-run',
      sequence: 1,
      type: 'assistant_output_delta',
      payload: { content_delta: 'Fresh answer' },
    });
    older.resolve({
      messages: [],
      active_run: { run_id: 'old-run', status: 'running' },
    });

    await loading;

    expect(loadChatHistory).toHaveBeenCalledTimes(2);
    expect(runStream.attachRunStream).toHaveBeenCalledExactlyOnceWith(session, {
      run_id: 'new-run',
      status: 'running',
    });
    expect(session.streamingRunEvents[0].payload.content_delta).toBe(
      'Fresh answer',
    );
    expect(session.messages[0].id).toBe('new-user');
  });

  it.each(['session-one', 'session-two'])(
    'releases the displayed load before an older request finishes (%s)',
    async (destination) => {
      const older = deferred();
      let displayed = 'session-one';
      const loadChatHistory = vi
        .fn()
        .mockReturnValueOnce(older.promise)
        .mockResolvedValueOnce({ messages: [] });
      const { chatState, controller } = setup({
        operationOverrides: { loadChatHistory },
        isDisplayedSession: (_agent, session) => session === displayed,
      });
      const first = controller.loadHistoryForSession('alpha', 'session-one');
      displayed = destination;

      await controller.loadHistoryForSession('alpha', destination);

      expect(chatState.loadingHistory).toBe(false);
      older.resolve({ messages: [] });
      await first;
      expect(chatState.loadingHistory).toBe(false);
    },
  );

  it.each(['refresh', 'recovery'])(
    'does not let an older recovery overwrite a newer %s',
    async (replacement) => {
      const older = deferred();
      const freshMessage = {
        id: 'fresh',
        role: 'assistant',
        content: 'Fresh saved output',
      };
      const loadChatHistory = vi
        .fn()
        .mockReturnValueOnce(older.promise)
        .mockResolvedValueOnce({
          messages: [freshMessage],
          active_run: { run_id: 'run-one', status: 'running' },
        });
      const { chatState, controller } = setup({
        operationOverrides: { loadChatHistory },
        isDisplayedSession: () => true,
      });
      const session = ensureSessionState(chatState, 'alpha', 'session-one');
      startRun(session, { run_id: 'run-one' });
      const recovering = controller.reconcileRunSession(session, 'run-one');
      if (replacement === 'refresh') {
        await controller.loadHistoryForSession('alpha', 'session-one');
      } else {
        await controller.reconcileRunSession(session, 'run-one');
      }
      older.resolve({
        messages: [],
        active_run: { run_id: 'run-one', status: 'running' },
      });

      await recovering;

      expect(session.messages).toEqual([freshMessage]);
    },
  );

  it('uses the opaque server cursor when loading older History', async () => {
    const loadChatHistory = vi
      .fn()
      .mockResolvedValueOnce({
        messages: [{ id: 'duplicate', role: 'user', content: 'newer' }],
        has_more: true,
        next_before: 'vh1.opaque',
      })
      .mockResolvedValueOnce({
        messages: [{ id: 'duplicate', role: 'user', content: 'older' }],
        has_more: false,
      });
    const { chatState, controller } = setup({
      operationOverrides: { loadChatHistory },
    });

    await controller.loadHistoryForSession('alpha', 'session-one');
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    await controller.loadOlderHistory(sessionState);

    expect(loadChatHistory).toHaveBeenNthCalledWith(2, {
      agent_id: 'alpha',
      session_id: 'session-one',
      limit: 50,
      before: 'vh1.opaque',
    });
    expect(sessionState.historyBefore).toBe('');
  });

  it.each([
    ['refresh', 'success'],
    ['refresh', 'failure'],
    ['recovery', 'success'],
    ['recovery', 'failure'],
  ])(
    'discards stale older-page %s/%s results without skipping history',
    async (replacement, outcome) => {
      const older = deferred();
      const message = (id) => ({ id, role: 'user', content: id });
      const loadChatHistory = vi
        .fn()
        .mockResolvedValueOnce({
          messages: [message('middle'), message('recent')],
          has_more: true,
          next_before: 'cursor-middle',
        })
        .mockReturnValueOnce(older.promise)
        .mockResolvedValueOnce({
          messages: [message('recent'), message('newest')],
          has_more: true,
          next_before: 'cursor-recent',
        })
        .mockResolvedValueOnce({
          messages: [message('oldest'), message('middle')],
          has_more: false,
        });
      const { chatState, controller } = setup({
        operationOverrides: { loadChatHistory },
      });
      await controller.loadHistoryForSession('alpha', 'session-one');
      const sessionState = ensureSessionState(
        chatState,
        'alpha',
        'session-one',
      );
      startRun(sessionState, { run_id: 'run-one', status: 'running' });

      const pendingOlder = controller.loadOlderHistory(sessionState);
      if (replacement === 'recovery') {
        await controller.reconcileRunSession(sessionState, 'run-one');
      } else {
        await controller.loadHistoryForSession('alpha', 'session-one');
      }
      sessionState.actionError = 'newer-action-error';
      if (outcome === 'failure') {
        older.reject(new Error('stale-page-error'));
      } else {
        older.resolve({
          messages: [message('oldest')],
          has_more: false,
          background_bash_statuses: { 'old-process': 'running' },
        });
      }

      expect(await pendingOlder).toBe(false);
      expect(sessionState.messages.map((item) => item.id)).toEqual([
        'recent',
        'newest',
      ]);
      expect(sessionState.historyBefore).toBe('cursor-recent');
      expect(sessionState.hasOlderHistory).toBe(true);
      expect(sessionState.loadingOlderHistory).toBe(false);
      expect(sessionState.actionError).toBe('newer-action-error');
      expect(sessionState.backgroundBashStatuses).toEqual({});

      expect(await controller.loadOlderHistory(sessionState)).toBe(true);
      expect(loadChatHistory).toHaveBeenLastCalledWith({
        agent_id: 'alpha',
        session_id: 'session-one',
        limit: 50,
        before: 'cursor-recent',
      });
      expect(sessionState.messages.map((item) => item.id)).toEqual([
        'oldest',
        'middle',
        'recent',
        'newest',
      ]);
      expect(sessionState.hasOlderHistory).toBe(false);
    },
  );

  it.each(['failed refresh', 'other Session refresh'])(
    'keeps a pending older page usable after a %s',
    async (replacement) => {
      const older = deferred();
      const loadChatHistory = vi
        .fn()
        .mockResolvedValueOnce({
          messages: [{ id: 'recent', role: 'user', content: 'Recent' }],
          has_more: true,
          next_before: 'cursor-recent',
        })
        .mockReturnValueOnce(older.promise);
      const { chatState, controller } = setup({
        operationOverrides: { loadChatHistory },
      });
      await controller.loadHistoryForSession('alpha', 'session-one');
      const sessionState = ensureSessionState(
        chatState,
        'alpha',
        'session-one',
      );
      const pendingOlder = controller.loadOlderHistory(sessionState);
      if (replacement === 'failed refresh') {
        loadChatHistory.mockRejectedValueOnce(new Error('refresh-error'));
        await controller.loadHistoryForSession('alpha', 'session-one');
      } else {
        loadChatHistory.mockResolvedValueOnce({
          messages: [],
          has_more: false,
        });
        await controller.loadHistoryForSession('alpha', 'session-two');
      }
      older.resolve({
        messages: [{ id: 'oldest', role: 'user', content: 'Oldest' }],
        has_more: false,
      });

      expect(await pendingOlder).toBe(true);
      expect(sessionState.messages.map((item) => item.id)).toEqual([
        'oldest',
        'recent',
      ]);
      expect(sessionState.loadingOlderHistory).toBe(false);
      expect(sessionState.hasOlderHistory).toBe(false);
    },
  );

  it('discards an older page after an accepted history edit', async () => {
    const older = deferred();
    const loadChatHistory = vi
      .fn()
      .mockResolvedValueOnce({
        messages: [{ id: 'edited', role: 'user', content: 'Original' }],
        has_more: true,
        next_before: 'cursor-edited',
      })
      .mockReturnValueOnce(older.promise);
    const editChatMessage = vi.fn().mockResolvedValue({
      run_id: 'run-edited',
      status: 'running',
    });
    const { chatState, controller } = setup({
      operationOverrides: { loadChatHistory, editChatMessage },
    });
    await controller.loadHistoryForSession('alpha', 'session-one');
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    const pendingOlder = controller.loadOlderHistory(sessionState);

    await controller.editMessage(sessionState, 'edited', 'Replacement');
    older.resolve({
      messages: [{ id: 'oldest', role: 'user', content: 'Oldest' }],
      has_more: false,
    });

    expect(await pendingOlder).toBe(false);
    expect(sessionState.messages).toEqual([]);
    expect(sessionState.currentRun.runId).toBe('run-edited');
    expect(sessionState.hasOlderHistory).toBe(true);
  });

  it('applies each connection snapshot object only once', () => {
    const { controller, runStream } = setup();
    const snapshot = { active_runs: [] };

    expect(controller.applyConnectionSnapshot(snapshot)).toBe(true);
    expect(controller.applyConnectionSnapshot(snapshot)).toBe(false);
    expect(runStream.applyConnectionSnapshot).toHaveBeenCalledOnce();
  });

  it('re-syncs only held sessions matching a scoped Queue invalidation', async () => {
    const { chatState, controller, listQueue } = setup();
    const target = ensureSessionState(
      chatState,
      'builder@project-one',
      'session-one',
    );
    ensureSessionState(chatState, 'reviewer@project-one', 'session-two');
    const scope = { agentId: 'builder', sessionId: 'session-one' };

    expect(controller.applyQueueInvalidation(scope)).toBe(true);
    expect(controller.applyQueueInvalidation(scope)).toBe(false);
    await vi.waitFor(() => expect(listQueue).toHaveBeenCalledOnce());

    expect(listQueue).toHaveBeenCalledWith(
      'builder@project-one',
      'session-one',
    );
    expect(target.queue).toEqual([
      { id: 'queued-one', content: 'Next', editable: true, created_at: null },
    ]);
  });

  it('authoritatively replaces held Queue projections from a connection snapshot', () => {
    const { chatState, controller, onRestartQueueDiscarded } = setup();
    const identitySession = ensureSessionState(
      chatState,
      'alpha',
      'session-one',
    );
    const projectSession = ensureSessionState(
      chatState,
      'builder@project-one',
      'session-two',
    );
    identitySession.queue = [
      { id: 'lost-one', content: 'Lost one' },
      { id: 'lost-two', content: 'Lost two' },
    ];
    projectSession.queue = [{ id: 'kept', content: 'Old text' }];

    controller.applyConnectionSnapshot({
      replay_status: 'epoch_changed',
      active_runs: [],
      queues: [
        {
          project_id: 'project-one',
          agent_id: 'builder',
          session_id: 'session-two',
          items: [{ id: 'kept', content: 'Server text', editable: true }],
        },
      ],
    });

    expect(identitySession.queue).toEqual([]);
    expect(projectSession.queue).toEqual([
      { id: 'kept', content: 'Server text', editable: true, created_at: null },
    ]);
    expect(onRestartQueueDiscarded).toHaveBeenCalledWith(2);
  });

  it('does not report Queue loss for a same-epoch replay gap', () => {
    const { chatState, controller, onRestartQueueDiscarded } = setup();
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    sessionState.queue = [{ id: 'stale', content: 'May have run' }];

    controller.applyConnectionSnapshot({
      replay_status: 'gap',
      active_runs: [],
      queues: [],
    });

    expect(sessionState.queue).toEqual([]);
    expect(onRestartQueueDiscarded).not.toHaveBeenCalled();
  });

  it('reports Queue sync failures and closes subscriptions on destroy', async () => {
    const { chatState, controller, listQueue, runStream } = setup();
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    const error = new Error('offline');
    listQueue.mockRejectedValueOnce(error);

    await controller.syncSessionQueue(sessionState);
    controller.destroy();

    expect(sessionState.actionError).toContain('offline');
    expect(chatState.actionError).toBe('');
    expect(runStream.closeSubscriptions).toHaveBeenCalledOnce();
  });

  it('ignores an older Queue response that settles after a newer sync', async () => {
    const older = deferred();
    const newer = deferred();
    const listQueue = vi
      .fn()
      .mockReturnValueOnce(older.promise)
      .mockReturnValueOnce(newer.promise);
    const { chatState, controller } = setup({
      operationOverrides: { listQueue },
    });
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');

    const olderSync = controller.syncSessionQueue(sessionState);
    const newerSync = controller.syncSessionQueue(sessionState);
    newer.resolve({
      items: [{ id: 'new', content: 'Newest', editable: true }],
    });
    await newerSync;
    older.resolve({
      items: [{ id: 'old', content: 'Stale', editable: true }],
    });
    await olderSync;

    expect(sessionState.queue).toEqual([
      {
        id: 'new',
        content: 'Newest',
        editable: true,
        created_at: null,
      },
    ]);
  });

  it('returns whether a Queue edit was saved and preserves failed edits', async () => {
    const updateQueueItem = vi
      .fn()
      .mockResolvedValueOnce({ ok: true })
      .mockRejectedValueOnce(new Error('offline'));
    const { chatState, controller } = setup({
      operationOverrides: { updateQueueItem },
    });
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    sessionState.queue = [
      { id: 'queued-one', content: 'Original', editable: true },
    ];

    expect(
      await controller.updateQueued(sessionState, 'queued-one', 'With file', [
        'notes.md',
      ]),
    ).toBe(true);
    expect(sessionState.queue[0]).toMatchObject({
      content: 'With file',
      editable: false,
    });

    expect(
      await controller.updateQueued(sessionState, 'queued-one', 'Unsaved', []),
    ).toBe(false);
    expect(sessionState.queue[0].content).toBe('With file');
    expect(sessionState.actionError).toContain('offline');
  });

  it('merges a terminal cancel response and reconciles durable Tool history', async () => {
    const cancelledRun = {
      run_id: 'run-cancelled',
      status: 'cancelled',
      events: [
        {
          type: 'run_cancelled',
          run_id: 'run-cancelled',
          sequence: 2,
          payload: { status: 'cancelled' },
        },
      ],
    };
    const cancelRun = vi.fn().mockResolvedValue(cancelledRun);
    const loadChatHistory = vi.fn().mockResolvedValue({
      active_run: null,
      messages: [
        {
          id: 'assistant-tools',
          role: 'assistant',
          run_id: 'run-cancelled',
          tool_calls: [
            {
              id: 'call-one',
              name: 'bash',
              arguments: { command: 'first command' },
            },
            {
              id: 'call-two',
              name: 'bash',
              arguments: { command: 'second command' },
            },
          ],
        },
        {
          id: 'run-summary',
          role: 'run_summary',
          run_id: 'run-cancelled',
          status: 'cancelled',
        },
      ],
    });
    const { chatState, controller, runStream } = setup({
      operationOverrides: { cancelRun, loadChatHistory },
    });
    runStream.mergeRunResponse.mockImplementation((state, run) => {
      for (const event of run.events) {
        appendRunEvent(state, event);
      }
      return true;
    });
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    startRun(sessionState, {
      run_id: 'run-cancelled',
      status: 'running',
      sse_url: '/api/runs/run-cancelled/events',
    });

    await controller.cancelActiveRun(sessionState);

    expect(cancelRun).toHaveBeenCalledWith('run-cancelled', {
      reason: 'user',
    });
    expect(runStream.mergeRunResponse).toHaveBeenCalledWith(
      sessionState,
      cancelledRun,
    );
    expect(loadChatHistory).toHaveBeenCalledWith({
      agent_id: 'alpha',
      session_id: 'session-one',
      limit: 100,
    });
    expect(sessionState.currentRun).toBeNull();
    expect(visibleTimelineItemsForRender(sessionState)[0].tools).toEqual([
      expect.objectContaining({
        toolCallId: 'call-one',
        status: 'cancelled',
      }),
      expect.objectContaining({
        toolCallId: 'call-two',
        status: 'cancelled',
      }),
    ]);
    expect(chatState.cancellingRun).toBe(false);
  });

  it('cancels a background Process and settles its status projection', async () => {
    const cancelProcess = vi.fn().mockResolvedValue({
      process_id: 'process-one',
      status: 'cancelled',
    });
    const { chatState, controller } = setup({
      operationOverrides: { cancelProcess },
    });
    const sessionState = ensureSessionState(
      chatState,
      'builder@project-one',
      'session-one',
    );

    await expect(
      controller.cancelBackgroundProcess({
        sessionState,
        agentId: 'builder',
        processId: 'process-one',
        projectId: 'project-one',
      }),
    ).resolves.toBe(true);

    expect(cancelProcess).toHaveBeenCalledWith({
      agentId: 'builder@project-one',
      processId: 'process-one',
    });
    expect(sessionState.backgroundBashStatuses).toEqual({
      'process-one': 'cancelled',
    });
  });

  it('keeps history transport failures separate from Run failures', async () => {
    const loadChatHistory = vi.fn().mockRejectedValue(new Error('offline'));
    const { chatState, controller } = setup({
      isDisplayedSession: () => true,
      operationOverrides: { loadChatHistory },
    });

    expect(await controller.loadHistoryForSession('alpha', 'session-one')).toBe(
      false,
    );

    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    expect(chatState.historyError).toBe('offline');
    expect(sessionState.error).toBeNull();
    expect(sessionState.status).toBe('idle');
  });

  it('ignores an older History response for the same Session', async () => {
    let resolveOlderHistory;
    let resolveNewerHistory;
    const loadChatHistory = vi
      .fn()
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveOlderHistory = resolve;
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveNewerHistory = resolve;
          }),
      );
    const { chatState, controller, runStream } = setup({
      isDisplayedSession: () => true,
      operationOverrides: { loadChatHistory },
    });

    const olderLoad = controller.loadHistoryForSession('alpha', 'session-one');
    const newerLoad = controller.loadHistoryForSession('alpha', 'session-one');
    resolveOlderHistory({
      active_run: { run_id: 'run-old', status: 'running' },
      has_more: false,
      messages: [{ id: 'user-old', role: 'user', content: 'Old snapshot' }],
    });

    expect(await olderLoad).toBe(false);
    expect(chatState.loadingHistory).toBe(true);
    expect(runStream.attachRunStream).not.toHaveBeenCalled();

    const newerHistory = {
      active_run: { run_id: 'run-new', status: 'running' },
      has_more: false,
      messages: [{ id: 'user-new', role: 'user', content: 'New snapshot' }],
    };
    resolveNewerHistory(newerHistory);

    expect(await newerLoad).toBe(true);
    expect(chatState.loadingHistory).toBe(false);
    expect(
      ensureSessionState(chatState, 'alpha', 'session-one').messages,
    ).toEqual(newerHistory.messages);
    expect(runStream.attachRunStream).toHaveBeenCalledOnce();
    expect(runStream.attachRunStream).toHaveBeenCalledWith(
      ensureSessionState(chatState, 'alpha', 'session-one'),
      newerHistory.active_run,
    );
  });

  it('keeps terminal live output when an in-flight History request returns a pre-completion snapshot', async () => {
    let resolveStaleHistory;
    const loadChatHistory = vi
      .fn()
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveStaleHistory = resolve;
          }),
      )
      .mockResolvedValueOnce({
        active_run: null,
        has_more: false,
        messages: [
          { id: 'user-final', role: 'user', content: 'Finish the work' },
          {
            id: 'assistant-final',
            role: 'assistant',
            content: 'Final answer',
          },
          {
            id: 'summary-final',
            role: 'run_summary',
            run_id: 'run-final',
            status: 'completed',
          },
        ],
      });
    const { chatState, controller, runStream } = setup({
      isDisplayedSession: () => true,
      operationOverrides: { loadChatHistory },
    });
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    startRun(sessionState, {
      run_id: 'run-final',
      status: 'running',
      sse_url: '/api/runs/run-final/events',
    });

    const staleLoad = controller.loadHistoryForSession('alpha', 'session-one');
    await vi.waitFor(() => expect(loadChatHistory).toHaveBeenCalledOnce());
    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-final',
      sequence: 1,
      payload: {
        message: {
          id: 'user-final',
          role: 'user',
          content: 'Finish the work',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-final',
      sequence: 2,
      payload: { content_delta: 'Final answer' },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-final',
      sequence: 3,
      payload: {
        message: {
          id: 'assistant-final',
          role: 'assistant',
          content: 'Final answer',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'run_completed',
      run_id: 'run-final',
      sequence: 4,
      payload: { status: 'completed' },
    });

    resolveStaleHistory({
      active_run: {
        run_id: 'run-final',
        status: 'running',
        sse_url: '/api/runs/run-final/events',
      },
      has_more: false,
      messages: [
        { id: 'user-final', role: 'user', content: 'Finish the work' },
      ],
    });
    await expect(staleLoad).resolves.toBe(true);

    const visibleOutput = () =>
      visibleTimelineItemsForRender(sessionState)
        .flatMap((item) => item.outputs ?? [])
        .map((item) => item.content);
    expect(sessionState.status).toBe('completed');
    expect(sessionState.runEvents.map((event) => event.type)).toContain(
      'assistant_output',
    );
    expect(sessionState.streamingRunEvents).toHaveLength(1);
    expect(visibleOutput()).toEqual(['Final answer']);
    expect(sessionState.currentRun?.status).toBe('completed');
    expect(runStream.attachRunStream).toHaveBeenLastCalledWith(
      sessionState,
      null,
    );

    await expect(
      controller.loadHistoryForSession('alpha', 'session-one'),
    ).resolves.toBe(true);

    expect(sessionState.status).toBe('idle');
    expect(sessionState.runEvents).toEqual([]);
    expect(sessionState.streamingRunEvents).toEqual([]);
    expect(visibleOutput()).toEqual(['Final answer']);
  });

  it('reconciles a stalled Run to its durable final assistant answer without re-executing it', async () => {
    const loadChatHistory = vi.fn().mockResolvedValue({
      active_run: null,
      has_more: false,
      messages: [
        { id: 'message-user', role: 'user', content: 'Do the work' },
        {
          id: 'message-final',
          role: 'assistant',
          content: 'The work is complete.',
        },
      ],
    });
    const { chatState, controller, runStream } = setup({
      isDisplayedSession: () => true,
      operationOverrides: { loadChatHistory },
    });
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    startRun(sessionState, {
      run_id: 'run-stalled',
      status: 'running',
      sse_url: '/api/runs/run-stalled/events',
    });

    expect(
      await controller.reconcileRunSession(sessionState, 'run-stalled'),
    ).toBe(true);

    expect(loadChatHistory).toHaveBeenCalledWith({
      agent_id: 'alpha',
      session_id: 'session-one',
      limit: 100,
    });
    expect(sessionState.messages.at(-1)).toMatchObject({
      id: 'message-final',
      content: 'The work is complete.',
    });
    expect(sessionState.status).toBe('idle');
    expect(sessionState.currentRun).toBeNull();
    expect(runStream.closeSubscriptionFor).toHaveBeenCalledWith(
      sessionState.key,
    );
  });

  it('does not reset terminal output when stalled-Run recovery returns an older snapshot', async () => {
    let resolveHistory;
    const loadChatHistory = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveHistory = resolve;
        }),
    );
    const { chatState, controller, runStream } = setup({
      isDisplayedSession: () => true,
      operationOverrides: { loadChatHistory },
    });
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    startRun(sessionState, {
      run_id: 'run-recovering',
      status: 'running',
      sse_url: '/api/runs/run-recovering/events',
    });

    const recovery = controller.reconcileRunSession(
      sessionState,
      'run-recovering',
    );
    await vi.waitFor(() => expect(loadChatHistory).toHaveBeenCalledOnce());
    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-recovering',
      sequence: 1,
      payload: { content_delta: 'Recovered answer' },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-recovering',
      sequence: 2,
      payload: {
        message: {
          id: 'assistant-recovering',
          role: 'assistant',
          content: 'Recovered answer',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'run_completed',
      run_id: 'run-recovering',
      sequence: 3,
      payload: { status: 'completed' },
    });
    resolveHistory({ active_run: null, has_more: false, messages: [] });

    await expect(recovery).resolves.toBe(true);
    expect(sessionState.status).toBe('completed');
    expect(sessionState.currentRun?.runId).toBe('run-recovering');
    expect(sessionState.runEvents.map((event) => event.type)).toContain(
      'assistant_output',
    );
    expect(sessionState.streamingRunEvents).toHaveLength(1);
    expect(runStream.closeSubscriptionFor).not.toHaveBeenCalled();
  });

  it('drops sparse live replay after history proves every Run is finished', async () => {
    const durableMessages = [
      {
        id: 'compaction-one',
        role: 'compaction_checkpoint',
        content: 'Earlier context',
      },
      { id: 'user-one', role: 'user', content: 'First question' },
      { id: 'assistant-one', role: 'assistant', content: 'First answer' },
      {
        id: 'summary-one',
        role: 'run_summary',
        run_id: 'run-one',
        status: 'completed',
      },
      { id: 'user-two', role: 'user', content: 'Second question' },
      { id: 'assistant-two', role: 'assistant', content: 'Second answer' },
      {
        id: 'summary-two',
        role: 'run_summary',
        run_id: 'run-two',
        status: 'completed',
      },
      { id: 'user-three', role: 'user', content: 'Latest question' },
      { id: 'assistant-three', role: 'assistant', content: 'Latest answer' },
      {
        id: 'summary-three',
        role: 'run_summary',
        run_id: 'run-stalled',
        status: 'completed',
      },
    ];
    const loadChatHistory = vi.fn().mockResolvedValue({
      active_run: null,
      has_more: false,
      messages: durableMessages,
    });
    const { chatState, controller } = setup({
      isDisplayedSession: () => true,
      operationOverrides: { loadChatHistory },
    });
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');

    for (const [index, run] of [
      ['run-one', durableMessages[1]],
      ['run-two', durableMessages[4]],
      ['run-stalled', durableMessages[7]],
    ].entries()) {
      appendRunEvent(sessionState, {
        type: 'run_started',
        run_id: run[0],
        sequence: 1,
        payload: { status: 'running' },
      });
      appendRunEvent(sessionState, {
        type: 'user_message_persisted',
        run_id: run[0],
        sequence: 2,
        payload: { message: run[1] },
        timestamp: `2026-08-02T20:0${index}:00+00:00`,
      });
    }

    expect(
      await controller.reconcileRunSession(sessionState, 'run-stalled'),
    ).toBe(true);

    const timelineItems = visibleTimelineItemsForRender(sessionState);
    const visibleUserTexts = timelineItems.flatMap((item) => {
      if (item.type === 'message' && item.message?.role === 'user') {
        return [item.message.content];
      }
      if (
        item.type === 'event' &&
        item.event?.type === 'user_message_persisted'
      ) {
        return [item.event.payload?.message?.content];
      }
      return [];
    });

    expect(visibleUserTexts).toEqual([
      'First question',
      'Second question',
      'Latest question',
    ]);
    expect(
      timelineItems.filter(
        (item) => item.type === 'assistant_run' && item.source === 'live',
      ),
    ).toEqual([]);
    expect(sessionState.runEvents).toEqual([]);
  });

  it('reattaches a Run that durable history still reports as active', async () => {
    const activeRun = {
      run_id: 'run-active',
      status: 'running',
      sse_url: '/api/runs/run-active/events',
      events: [],
    };
    const loadChatHistory = vi.fn().mockResolvedValue({
      active_run: activeRun,
      has_more: false,
      messages: [],
    });
    const { chatState, controller, runStream } = setup({
      isDisplayedSession: () => true,
      operationOverrides: { loadChatHistory },
    });
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    startRun(sessionState, activeRun);

    expect(
      await controller.reconcileRunSession(sessionState, 'run-active'),
    ).toBe(true);
    expect(runStream.attachRunStream).toHaveBeenCalledWith(
      sessionState,
      activeRun,
    );
    expect(runStream.closeSubscriptionFor).not.toHaveBeenCalled();
  });

  it('keeps failed background reconciliation silent for the next retry', async () => {
    const loadChatHistory = vi.fn().mockRejectedValue(new Error('offline'));
    const { chatState, controller } = setup({
      isDisplayedSession: () => true,
      operationOverrides: { loadChatHistory },
    });
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    startRun(sessionState, {
      run_id: 'run-stalled',
      status: 'running',
      sse_url: '/api/runs/run-stalled/events',
    });
    chatState.historyError = 'existing visible error';

    expect(
      await controller.reconcileRunSession(sessionState, 'run-stalled'),
    ).toBe(false);
    expect(chatState.historyError).toBe('existing visible error');
    expect(sessionState.status).toBe('running');
    expect(sessionState.currentRun?.runId).toBe('run-stalled');
  });

  it('loads the roster, current history, Run truth, and Queue as one lifecycle', async () => {
    const loadChatHistory = vi.fn().mockResolvedValue({
      active_run: null,
      has_more: false,
      messages: [{ id: 'message-one', role: 'user', content: 'Hello' }],
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
    const { chatState, controller, listQueue, runStream } = setup({
      isDisplayedSession: (agentId, sessionId) =>
        agentId === 'alpha' && sessionId === 'session-one',
      operationOverrides: { listAgents, loadChatHistory },
    });

    await controller.loadAgents();

    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    expect(chatState.selectedAgentId).toBe('alpha');
    expect(sessionState.messages).toMatchObject([
      { id: 'message-one', content: 'Hello' },
    ]);
    expect(listQueue).toHaveBeenCalledWith('alpha', 'session-one');
    expect(runStream.attachRunStream).toHaveBeenCalledWith(sessionState, null);
    expect(chatState.loadingHistory).toBe(false);
  });

  it('stops the Agent loading state before current History settles', async () => {
    let resolveHistory;
    const loadChatHistory = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveHistory = resolve;
        }),
    );
    const listAgents = vi.fn().mockResolvedValue({
      agents: [
        {
          id: 'alpha',
          name: 'Alpha',
          current_session_id: 'session-one',
        },
      ],
    });
    const { chatState, controller } = setup({
      isDisplayedSession: () => true,
      operationOverrides: { listAgents, loadChatHistory },
    });

    const loading = controller.loadAgents();
    await vi.waitFor(() => expect(loadChatHistory).toHaveBeenCalledOnce());

    expect(chatState.loadingAgents).toBe(false);
    expect(chatState.loadingHistory).toBe(true);

    resolveHistory({ active_run: null, messages: [], has_more: false });
    await expect(loading).resolves.toBe(true);
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
    const { chatState, controller } = setup({
      isDisplayedSession: () => true,
      operationOverrides: { listAgents, loadChatHistory },
    });

    await controller.loadAgents({ silent: true });

    expect(chatState.loadingAgents).toBe(false);
    expect(chatState.agents).toHaveLength(1);
    expect(loadChatHistory).not.toHaveBeenCalled();
  });

  it('normalizes command suggestions inside the controller', async () => {
    const listChatCommands = vi.fn().mockResolvedValue({
      items: [
        { name: '/HELP', type: 'command', description: 'Show help' },
        { name: 'review', type: 'skill', description: 'Review code' },
      ],
    });
    const { chatState, controller } = setup({
      operationOverrides: { listChatCommands },
    });

    await controller.loadCommands('alpha');

    expect(listChatCommands).toHaveBeenCalledWith({ agent_id: 'alpha' });
    expect(chatState.availableSkills).toMatchObject([
      { name: 'help', type: 'command' },
      { name: 'review', type: 'skill' },
    ]);
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
    const { chatState, controller } = setup({
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
    const { chatState, controller } = setup({
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
    const { chatState, controller } = setup({
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
    const { chatState, controller } = setup({
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
    const { chatState, controller } = setup({
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

    expect(await refresh).toBe(false);
    expect(sessionState.hasUnreadCompletion).toBe(false);
  });

  it('retains known activity when the batched refresh fails', async () => {
    const listSessionActivity = vi
      .fn()
      .mockRejectedValue(new Error('activity unavailable'));
    const { chatState, controller } = setup({
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

  it('reconciles queued and started send outcomes into Session state', async () => {
    const startChatRun = vi
      .fn()
      .mockResolvedValueOnce({
        queued: true,
        item: { id: 'queued-two', content: 'Later' },
      })
      .mockResolvedValueOnce({
        run_id: 'run-one',
        sse_url: '/events/run-one',
      });
    const { chatState, controller, runStream } = setup({
      operationOverrides: { startChatRun },
    });
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');

    expect(await controller.sendMessage(sessionState, 'Later')).toMatchObject({
      kind: 'queued',
    });
    expect(sessionState.queue).toMatchObject([
      { id: 'queued-two', content: 'Later' },
    ]);

    expect(await controller.sendMessage(sessionState, 'Now')).toMatchObject({
      kind: 'started',
      runId: 'run-one',
    });
    expect(sessionState.currentRun?.runId).toBe('run-one');
    expect(runStream.subscribeToRun).toHaveBeenCalledWith(
      sessionState,
      '/events/run-one',
      { afterSequence: 0 },
    );
  });

  it('attaches send admission errors only to the addressed Session', async () => {
    const startChatRun = vi.fn().mockRejectedValue(new Error('provider down'));
    const { chatState, controller } = setup({
      operationOverrides: { startChatRun },
    });
    const addressed = ensureSessionState(chatState, 'alpha', 'session-one');
    const other = ensureSessionState(chatState, 'alpha', 'session-two');

    expect(await controller.sendMessage(addressed, 'Hello')).toEqual({
      kind: 'failed',
    });

    expect(addressed.actionError).toContain('provider down');
    expect(addressed.error).toBeNull();
    expect(addressed.status).toBe('idle');
    expect(other.actionError).toBe('');
    expect(chatState.actionError).toBe('');
  });

  it('restarts from an edited message only after server admission succeeds', async () => {
    const editChatMessage = vi.fn().mockResolvedValue({
      run_id: 'run-edit',
      sse_url: '/events/run-edit',
    });
    const { chatState, controller, runStream } = setup({
      operationOverrides: { editChatMessage },
    });
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    sessionState.messages = [
      { id: 'kept', role: 'assistant', content: 'Earlier context' },
      { id: 'target', role: 'user', content: 'Original request' },
      { id: 'discarded', role: 'assistant', content: 'Old answer' },
    ];
    sessionState.sessionUsage = { input_tokens: 100, output_tokens: 20 };
    sessionState.contextUsage = { input_tokens: 80 };

    await expect(
      controller.editMessage(sessionState, 'target', 'Edited request'),
    ).resolves.toEqual({ kind: 'started', runId: 'run-edit' });

    expect(editChatMessage).toHaveBeenCalledWith({
      agent_id: 'alpha',
      session_id: 'session-one',
      message_id: 'target',
      content: 'Edited request',
    });
    expect(sessionState.messages).toEqual([
      { id: 'kept', role: 'assistant', content: 'Earlier context' },
    ]);
    expect(sessionState.sessionUsage).toEqual({
      input_tokens: 100,
      output_tokens: 20,
    });
    expect(sessionState.contextUsage).toBeNull();
    expect(sessionState.currentRun?.runId).toBe('run-edit');
    expect(runStream.subscribeToRun).toHaveBeenCalledWith(
      sessionState,
      '/events/run-edit',
      { afterSequence: 0 },
    );
  });

  it('keeps the visible lineage untouched when edit admission fails', async () => {
    const editChatMessage = vi.fn().mockRejectedValue(new Error('busy'));
    const { chatState, controller } = setup({
      operationOverrides: { editChatMessage },
    });
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    const messages = [
      { id: 'target', role: 'user', content: 'Original request' },
      { id: 'answer', role: 'assistant', content: 'Old answer' },
    ];
    sessionState.messages = messages;

    await expect(
      controller.editMessage(sessionState, 'target', 'Edited request'),
    ).resolves.toEqual({ kind: 'failed' });

    expect(sessionState.messages).toBe(messages);
    expect(sessionState.currentRun).toBeNull();
    expect(sessionState.actionError).toContain('busy');
  });

  it('reconciles a persisted Subagent row through its exact durable work id', async () => {
    const inspectSubAgentWork = vi.fn().mockResolvedValue({
      id: 'sub-old-work',
      agent_id: 'worker',
      project_id: 'project-one',
      session_id: 'reused-child',
      run_id: 'old-child-run',
      status: 'completed',
      result: 'Exact older result',
      timing: { duration_ms: 4200 },
    });
    const { chatState, controller } = setup({
      operationOverrides: { inspectSubAgentWork },
    });
    const tool = {
      type: 'tool_call',
      name: 'subagent',
      status: 'success',
      arguments: {
        action: 'run',
        agent_id: 'worker',
        content: 'Inspect the old state',
        background: true,
      },
      result: {
        ok: true,
        data: {
          id: 'sub-old-work',
          agent_id: 'worker',
          session_id: 'reused-child',
          run_id: 'old-child-run',
          status: 'running',
          delivery: 'automatic',
        },
      },
    };

    controller.reconcileSubAgentRows(
      [{ type: 'assistant_run', items: [tool] }],
      { projectId: 'project-one' },
    );
    await vi.waitFor(() =>
      expect(chatState.subAgentResults['work:sub-old-work']).toMatchObject({
        loading: false,
        result: 'Exact older result',
      }),
    );

    expect(inspectSubAgentWork).toHaveBeenCalledWith({
      id: 'sub-old-work',
      agent_id: 'worker@project-one',
      session_id: 'reused-child',
    });
    expect(chatState.subAgentStatuses).toMatchObject({
      'run:old-child-run': 'completed',
      'runDuration:old-child-run': 4200,
      'workRun:sub-old-work': 'old-child-run',
    });
  });

  it('settles a live Subagent row after inspection discovers its child Run', async () => {
    const inspectSubAgentWork = vi
      .fn()
      .mockResolvedValueOnce({
        id: 'sub-live-work',
        agent_id: 'worker',
        session_id: 'child-session',
        run_id: 'child-run',
        status: 'running',
        result: null,
      })
      .mockResolvedValueOnce({
        id: 'sub-live-work',
        agent_id: 'worker',
        session_id: 'child-session',
        run_id: 'child-run',
        status: 'completed',
        result: 'Live child result',
      });
    const { chatState, controller } = setup({
      operationOverrides: { inspectSubAgentWork },
    });
    const tool = {
      type: 'tool_call',
      name: 'subagent',
      status: 'success',
      arguments: {
        action: 'run',
        agent_id: 'worker',
        content: 'Run in the background',
      },
      result: {
        ok: true,
        data: {
          id: 'sub-live-work',
          agent_id: 'worker',
          session_id: 'child-session',
          status: 'running',
          delivery: 'automatic',
        },
      },
    };
    const items = [{ type: 'assistant_run', items: [tool] }];

    controller.reconcileSubAgentRows(items);
    await vi.waitFor(() =>
      expect(chatState.subAgentStatuses).toMatchObject({
        'run:child-run': 'running',
        'workRun:sub-live-work': 'child-run',
      }),
    );

    controller.applySubAgentStatusUpdates({
      'run:child-run': 'completed',
    });
    controller.reconcileSubAgentRows(items);

    await vi.waitFor(() =>
      expect(chatState.subAgentResults['work:sub-live-work']).toMatchObject({
        loading: false,
        result: 'Live child result',
      }),
    );
    expect(inspectSubAgentWork).toHaveBeenCalledTimes(2);
  });

  it('cancels a consumed queued Subagent through exact work inspection', async () => {
    const removeFromQueue = vi.fn().mockRejectedValue(
      Object.assign(new Error('already started'), {
        code: 'queue_item_not_found',
      }),
    );
    const inspectSubAgentWork = vi.fn().mockResolvedValue({
      id: 'sub-queued-work',
      agent_id: 'worker',
      project_id: 'project-one',
      session_id: 'child-session',
      run_id: 'admitted-child-run',
      status: 'running',
      result: null,
    });
    const cancelRun = vi.fn().mockResolvedValue({ status: 'cancelled' });
    const { chatState, controller } = setup({
      operationOverrides: {
        cancelRun,
        inspectSubAgentWork,
        removeFromQueue,
      },
    });
    const sessionState = ensureSessionState(
      chatState,
      'parent',
      'parent-session',
    );
    const tool = {
      type: 'tool_call',
      name: 'subagent',
      status: 'success',
      arguments: {
        action: 'run',
        agent_id: 'worker',
        content: 'Queued work',
      },
      result: {
        ok: true,
        data: {
          id: 'sub-queued-work',
          agent_id: 'worker',
          session_id: 'child-session',
          queue_item_id: 'queue-item-one',
          status: 'queued',
        },
      },
    };

    await expect(
      controller.cancelSubAgent({
        tool,
        sessionState,
        projectId: 'project-one',
      }),
    ).resolves.toBe(true);

    expect(inspectSubAgentWork).toHaveBeenCalledWith({
      id: 'sub-queued-work',
      agent_id: 'worker@project-one',
      session_id: 'child-session',
    });
    expect(cancelRun).toHaveBeenCalledWith('admitted-child-run', {
      reason: 'user',
    });
    expect(chatState.subAgentStatuses).toMatchObject({
      'run:admitted-child-run': 'cancelled',
      'queue:queue-item-one': 'cancelled',
      'queueRun:queue-item-one': 'admitted-child-run',
    });
  });

  it('merges background Bash status events into the bounded process map', () => {
    const { chatState, controller } = setup();

    controller.applyBackgroundBashStatusEvents([
      {
        type: 'bash_process_status_changed',
        payload: {
          process_id: 'process-one',
          status: 'completed',
          exit_code: 0,
          cancelled_by_user: false,
          started_at: '2026-09-04T12:00:00Z',
          finished_at: '2026-09-04T12:04:12Z',
          output: 'build finished',
          truncated: false,
          log_file: 'C:/logs/bash/process-one.log',
        },
      },
    ]);
    controller.applyBackgroundBashStatusEvents([
      {
        type: 'bash_process_status_changed',
        payload: { process_id: 'process-two', status: 'failed', exit_code: 1 },
      },
    ]);
    // Re-applying the same event is idempotent.
    controller.applyBackgroundBashStatusEvents([
      {
        type: 'bash_process_status_changed',
        payload: { process_id: 'process-two', status: 'failed', exit_code: 1 },
      },
    ]);

    expect(chatState.backgroundBashProcesses['process-one']).toEqual({
      status: 'completed',
      exitCode: 0,
      cancelledByUser: false,
      startedAt: '2026-09-04T12:00:00Z',
      finishedAt: '2026-09-04T12:04:12Z',
      output: 'build finished',
      truncated: false,
      logFile: 'C:/logs/bash/process-one.log',
    });
    expect(chatState.backgroundBashProcesses['process-two']).toEqual({
      status: 'failed',
      exitCode: 1,
      cancelledByUser: false,
      startedAt: '',
      finishedAt: '',
      output: '',
      truncated: false,
      logFile: '',
    });
    expect(Object.keys(chatState.backgroundBashProcesses)).toHaveLength(2);
  });

  it('ignores background Bash status events without a process id', () => {
    const { chatState, controller } = setup();

    controller.applyBackgroundBashStatusEvents([
      { type: 'bash_process_status_changed', payload: { status: 'completed' } },
    ]);
    controller.applyBackgroundBashStatusEvents([]);

    expect(chatState.backgroundBashProcesses).toEqual({});
  });
});

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}
