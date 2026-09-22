import { describe, expect, it, vi } from 'vitest';
import {
  appendRunEvent,
  applyRunControls,
  createChatState,
  ensureSessionState,
  startRun,
} from '../chatState.js';
import { setup, deferred, review } from './chatState.controller.support.js';

it.each(['run_completed', 'run_failed', 'run_cancelled', 'run_interrupted'])(
  'keeps %s authoritative when the send response arrives late',
  async (type) => {
    const response = deferred();
    const { controller, chatState } = setup({
      operationOverrides: { startChatRun: vi.fn(() => response.promise) },
    });
    const session = ensureSessionState(chatState, 'alpha', 'session');
    const sending = controller.sendMessage(session, 'Hello');
    appendRunEvent(session, {
      run_id: 'one',
      sequence: 1,
      type: 'run_started',
    });
    appendRunEvent(session, {
      run_id: 'one',
      sequence: 2,
      type: 'assistant_output_delta',
      payload: { content_delta: 'Reply' },
    });
    const terminal = {
      run_id: 'one',
      sequence: 3,
      type,
      payload: { iteration_count: 2 },
    };
    appendRunEvent(session, terminal);
    const drafts = session.streamingRunEvents;
    response.resolve({
      run_id: 'one',
      status: 'running',
      sse_url: '/one',
      events: [],
    });
    await sending;
    appendRunEvent(session, terminal);
    expect(session.status).toBe(type.slice(4));
    expect(session.currentRun.status).toBe(type.slice(4));
    expect(session.currentRun.iterationCount).toBe(2);
    expect(session.streamingRunEvents).toBe(drafts);
    controller.destroy();
  },
);

it('keeps live Run controls authoritative across stale snapshots and other Runs', () => {
  const session = ensureSessionState(
    createChatState(),
    'agent@project',
    'session',
  );
  startRun(session, { run_id: 'one', status: 'running' });
  appendRunEvent(session, {
    run_id: 'one',
    sequence: 5,
    type: 'run_controls_changed',
    payload: { compaction: 'pending' },
  });
  applyRunControls(session, {
    run_id: 'one',
    controls_sequence: 4,
    controls: { compaction: 'idle' },
  });
  applyRunControls(session, {
    run_id: 'other',
    controls_sequence: 6,
    controls: { compaction: 'idle' },
  });
  expect(session.currentRun.controls.compaction).toBe('pending');
  applyRunControls(session, {
    run_id: 'one',
    controls_sequence: 6,
    controls: { compaction: 'running' },
  });
  expect(session.currentRun.controls.compaction).toBe('running');
});

it('submits controls to the captured Session and coalesces clicks', async () => {
  let resolve;
  const controlRun = vi.fn(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  const { controller, chatState, runStream } = setup({
    operationOverrides: { controlRun },
  });
  const session = ensureSessionState(chatState, 'agent@project', 'session');
  startRun(session, { run_id: 'one', status: 'running' });
  const pending = controller.controlRun(session, 'compact');
  await controller.controlRun(session, 'compact');
  expect(controlRun).toHaveBeenCalledTimes(1);
  expect(controlRun).toHaveBeenCalledWith({
    agentId: 'agent@project',
    sessionId: 'session',
    runId: 'one',
    action: 'compact',
    toolCallId: undefined,
  });
  const response = { run_id: 'one', controls: { compaction: 'pending' } };
  resolve(response);
  await pending;
  expect(runStream.mergeRunResponse).toHaveBeenCalledWith(session, response);
  expect(session.pendingRunControls).toEqual({});
  controller.destroy();
});

describe('chat controller', () => {
  it('restores completed reflections on fresh history load without touching another Session', async () => {
    const { chatState, controller } = setup({
      operationOverrides: {
        loadChatHistory: vi.fn().mockResolvedValue({
          messages: [],
          reflection_runs: [review()],
        }),
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
    response.resolve({
      messages: [],
      reflection_runs: [review('running')],
    });
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
});

it('keeps a successor Run when an older send response arrives', async () => {
  const response = deferred();
  const { controller, chatState, runStream } = setup({
    operationOverrides: { startChatRun: vi.fn(() => response.promise) },
  });
  const session = ensureSessionState(chatState, 'alpha', 'session');
  const sending = controller.sendMessage(session, 'Hello');
  appendRunEvent(session, { run_id: 'one', sequence: 1, type: 'run_started' });
  appendRunEvent(session, {
    run_id: 'one',
    sequence: 2,
    type: 'run_completed',
  });
  appendRunEvent(session, { run_id: 'two', sequence: 1, type: 'run_started' });
  appendRunEvent(session, {
    run_id: 'two',
    sequence: 2,
    type: 'assistant_output_delta',
    payload: { content_delta: 'New' },
  });
  const drafts = session.streamingRunEvents;
  response.resolve({
    run_id: 'one',
    status: 'running',
    sse_url: '/one',
    events: [],
  });
  await sending;
  expect(session.currentRun.runId).toBe('two');
  expect(session.streamingRunEvents).toBe(drafts);
  expect(runStream.subscribeToRun).not.toHaveBeenCalled();
  controller.destroy();
});

it('preserves accepted edit events that arrived before its response', async () => {
  const response = deferred();
  const { controller, chatState } = setup({
    operationOverrides: { editChatMessage: vi.fn(() => response.promise) },
  });
  const session = ensureSessionState(chatState, 'alpha', 'session');
  session.messages = [{ id: 'target', role: 'user', content: 'Old' }];
  const editing = controller.editMessage(session, 'target', 'New');
  appendRunEvent(session, { run_id: 'edit', sequence: 1, type: 'run_started' });
  appendRunEvent(session, {
    run_id: 'edit',
    sequence: 2,
    type: 'assistant_output_delta',
    payload: { content_delta: 'Reply' },
  });
  const drafts = session.streamingRunEvents;
  response.resolve({
    run_id: 'edit',
    status: 'running',
    sse_url: '/edit',
    events: [],
  });
  await editing;
  expect(
    session.runEvents.some(
      (event) => event.run_id === 'edit' && event.sequence === 1,
    ),
  ).toBe(true);
  expect(session.streamingRunEvents).toBe(drafts);
  expect(session.messages).toEqual([]);
  controller.destroy();
});

it('reconciles an accepted edit without replacing its live successor Run', async () => {
  const response = deferred();
  const history = deferred();
  const loadChatHistory = vi.fn(() => history.promise);
  const { controller, chatState, runStream } = setup({
    isDisplayedSession: () => true,
    operationOverrides: {
      editChatMessage: vi.fn(() => response.promise),
      loadChatHistory,
    },
  });
  const session = ensureSessionState(chatState, 'alpha', 'session');
  session.messages = [{ id: 'target', role: 'user', content: 'Old' }];
  const editing = controller.editMessage(session, 'target', 'New');
  appendRunEvent(session, { run_id: 'edit', sequence: 1, type: 'run_started' });
  appendRunEvent(session, {
    run_id: 'edit',
    sequence: 2,
    type: 'run_completed',
  });
  appendRunEvent(session, { run_id: 'next', sequence: 1, type: 'run_started' });
  appendRunEvent(session, {
    run_id: 'next',
    sequence: 2,
    type: 'assistant_output_delta',
    payload: { content_delta: 'Successor output' },
  });
  const drafts = session.streamingRunEvents;
  response.resolve({
    run_id: 'edit',
    status: 'running',
    sse_url: '/edit',
    events: [],
  });
  await vi.waitFor(() => expect(loadChatHistory).toHaveBeenCalledOnce());
  expect(session.currentRun.runId).toBe('next');
  expect(session.streamingRunEvents).toBe(drafts);
  history.resolve({
    messages: [{ id: 'replacement', role: 'user', content: 'New' }],
    active_run: { run_id: 'next', status: 'running', sse_url: '/next' },
  });
  await editing;
  expect(session.messages).toMatchObject([{ id: 'replacement' }]);
  expect(session.currentRun.runId).toBe('next');
  expect(runStream.subscribeToRun).not.toHaveBeenCalled();
  expect(runStream.attachRunStream).toHaveBeenCalledWith(
    session,
    expect.objectContaining({ run_id: 'next' }),
  );
  controller.destroy();
});

it('invalidates a pending Queue read when a newer connection snapshot arrives', async () => {
  const response = deferred();
  const { controller, chatState } = setup({
    operationOverrides: { listQueue: vi.fn(() => response.promise) },
  });
  const session = ensureSessionState(chatState, 'alpha', 'session');
  const syncing = controller.syncSessionQueue(session);
  controller.applyConnectionSnapshot({ active_runs: [], queues: [] });
  response.resolve({ items: [{ id: 'obsolete', content: 'old' }] });
  await syncing;
  expect(session.queue).toEqual([]);
  controller.destroy();
});

it('syncs from the cursor without discarding previously loaded older pages', async () => {
  const loadChatHistory = vi
    .fn()
    .mockResolvedValueOnce({
      history_generation: 'g',
      next_after: 'after-2',
      messages: [
        { id: 'two', role: 'user', content: 'Two', history_sequence: 1 },
      ],
      has_more: true,
      next_before: 'before-1',
    })
    .mockResolvedValueOnce({
      history_generation: 'g',
      messages: [
        { id: 'one', role: 'user', content: 'One', history_sequence: 0 },
      ],
      has_more: false,
    })
    .mockResolvedValueOnce({
      history_generation: 'g',
      next_after: 'after-3',
      incremental: true,
      has_newer: true,
      messages: [
        { id: 'three', role: 'user', content: 'Three', history_sequence: 2 },
      ],
    })
    .mockResolvedValueOnce({
      history_generation: 'g',
      next_after: 'after-4',
      incremental: true,
      has_newer: false,
      messages: [
        { id: 'four', role: 'user', content: 'Four', history_sequence: 3 },
      ],
    });
  const { controller, chatState } = setup({
    operationOverrides: { loadChatHistory },
  });
  await controller.loadHistoryForSession('alpha', 'session');
  const session = ensureSessionState(chatState, 'alpha', 'session');
  await controller.loadOlderHistory(session);
  await controller.loadHistoryForSession('alpha', 'session');
  expect(loadChatHistory.mock.calls[2][0]).toMatchObject({ after: 'after-2' });
  expect(loadChatHistory.mock.calls[3][0]).toMatchObject({ after: 'after-3' });
  expect(session.messages.map((message) => message.id)).toEqual([
    'one',
    'two',
    'three',
    'four',
  ]);
  expect(session.historyAfter).toBe('after-4');
  expect(session.hasOlderHistory).toBe(false);
  controller.destroy();
});

it('replaces a collected delta when a concurrent edit invalidates its cursor', async () => {
  const loadChatHistory = vi
    .fn()
    .mockResolvedValueOnce({
      history_generation: 'g',
      next_after: 'a',
      messages: [
        { id: 'old', role: 'user', content: 'Old', history_sequence: 0 },
      ],
    })
    .mockResolvedValueOnce({
      history_generation: 'g',
      next_after: 'b',
      incremental: true,
      has_newer: true,
      messages: [
        {
          id: 'stale',
          role: 'assistant',
          content: 'Stale',
          history_sequence: 1,
        },
      ],
    })
    .mockResolvedValueOnce({
      history_generation: 'g',
      next_after: 'c',
      history_reset: true,
      incremental: false,
      messages: [
        { id: 'new', role: 'user', content: 'New', history_sequence: 3 },
      ],
    });
  const { controller, chatState } = setup({
    operationOverrides: { loadChatHistory },
  });
  await controller.loadHistoryForSession('alpha', 'session');
  await controller.loadHistoryForSession('alpha', 'session');
  expect(
    ensureSessionState(chatState, 'alpha', 'session').messages.map(
      (message) => message.id,
    ),
  ).toEqual(['new']);
  controller.destroy();
});
