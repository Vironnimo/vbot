import { describe, expect, it, vi } from 'vitest';
import {
  appendRunEvent,
  ensureSessionState,
  startRun,
  visibleTimelineItemsForRender,
} from '../chatState.js';
import {
  countTimelineTextOccurrences,
  deferred,
  reflectionRun,
  setupController,
} from './chatState.support.js';

describe('History reads', () => {
  it('keeps Session facts and an in-flight reflection read across an unchanged incremental read', async () => {
    const policy = { enabled: true, trigger: { type: 'context_ratio' } };
    const loadChatHistory = vi
      .fn()
      .mockResolvedValueOnce({
        messages: [{ id: 'reply', role: 'assistant', content: 'Hello' }],
        history_generation: 'g1',
        next_after: 'cursor-1',
        incremental: false,
        session_usage: { input_tokens: 10 },
        context_usage: { used_tokens: 5 },
        compaction_policy: policy,
        background_command_statuses: { term_one: 'running' },
        reflection_runs: [],
      })
      .mockResolvedValueOnce({
        messages: [],
        history_generation: 'g1',
        next_after: 'cursor-1',
        incremental: true,
        history_reset: false,
        runs: [],
      });
    const reflections = deferred();
    const { chatState, controller } = setupController({
      operationOverrides: {
        loadChatHistory,
        loadReflectionRuns: vi.fn().mockReturnValue(reflections.promise),
      },
      isDisplayedSession: () => true,
    });
    await controller.loadHistoryForSession('alpha', 'source');
    const source = chatState.sessions['alpha::source'];
    controller.applyConnectionSnapshot({ active_runs: [] });

    await controller.loadHistoryForSession('alpha', 'source');
    reflections.resolve({ reflection_runs: [reflectionRun()] });
    await vi.waitFor(() =>
      expect(source.reflectionTasks['review-one']?.status).toBe('completed'),
    );

    expect(loadChatHistory).toHaveBeenLastCalledWith({
      agent_id: 'alpha',
      session_id: 'source',
      limit: 500,
      after: 'cursor-1',
    });
    expect(source.messages.map((message) => message.id)).toEqual(['reply']);
    expect(source.sessionUsage).toEqual({ input_tokens: 10 });
    expect(source.contextUsage).toEqual({ used_tokens: 5 });
    expect(source.compactionPolicy).toBe(policy);
    expect(source.backgroundCommandStatuses).toEqual({
      term_one: 'running',
    });
  });

  it('folds background status deltas and Run records across incremental pages', async () => {
    const loadChatHistory = vi
      .fn()
      .mockResolvedValueOnce({
        messages: [],
        history_generation: 'g1',
        next_after: 'cursor-1',
        incremental: false,
        context_usage: { used_tokens: 5 },
        background_command_statuses: {
          term_one: 'running',
          term_two: 'running',
        },
      })
      .mockResolvedValueOnce({
        messages: [],
        history_generation: 'g1',
        next_after: 'cursor-2',
        incremental: true,
        has_newer: true,
        context_usage: { used_tokens: 7 },
        background_command_statuses: { term_one: 'completed' },
        runs: [{ run_id: 'run-one', status: 'completed' }],
      })
      .mockResolvedValueOnce({
        messages: [],
        history_generation: 'g1',
        next_after: 'cursor-3',
        incremental: true,
        context_usage: null,
        background_command_statuses: { term_two: 'failed' },
        runs: [{ run_id: 'run-two', status: 'failed' }],
      });
    const { chatState, controller } = setupController({
      operationOverrides: { loadChatHistory },
    });
    await controller.loadHistoryForSession('alpha', 'source');
    await controller.loadHistoryForSession('alpha', 'source');

    const source = chatState.sessions['alpha::source'];
    expect(loadChatHistory).toHaveBeenCalledTimes(3);
    expect(source.historyAfter).toBe('cursor-3');
    expect(source.backgroundCommandStatuses).toEqual({
      term_one: 'completed',
      term_two: 'failed',
    });
    expect(Object.keys(source.historyRuns).sort()).toEqual([
      'run-one',
      'run-two',
    ]);
    // A page that read the context and found none clears it.
    expect(source.contextUsage).toBeNull();
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
    const { controller, chatState } = setupController({
      operationOverrides: { loadChatHistory },
    });
    await controller.loadHistoryForSession('alpha', 'session');
    const session = ensureSessionState(chatState, 'alpha', 'session');
    await controller.loadOlderHistory(session);
    await controller.loadHistoryForSession('alpha', 'session');
    expect(loadChatHistory.mock.calls[2][0]).toMatchObject({
      after: 'after-2',
    });
    expect(loadChatHistory.mock.calls[3][0]).toMatchObject({
      after: 'after-3',
    });
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
    const { controller, chatState } = setupController({
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

  it('reloads a stale active-run snapshot without replacing a newer live Run', async () => {
    const older = deferred();
    const loadChatHistory = vi
      .fn()
      .mockReturnValueOnce(older.promise)
      .mockResolvedValueOnce({
        messages: [{ id: 'new-user', role: 'user', content: 'New question' }],
        active_run: { run_id: 'new-run', status: 'running' },
      });
    const { chatState, controller, runStream } = setupController({
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
    expect(
      countTimelineTextOccurrences(
        visibleTimelineItemsForRender(session),
        'Fresh answer',
      ),
    ).toBe(1);
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
      const { chatState, controller } = setupController({
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

  it.each([
    ['the same Session', 'alpha'],
    // An Identity Agent rename keeps the Session; a read for its old address
    // is older than any read under the new one.
    ['a Session whose Agent was renamed meanwhile', 'gamma'],
  ])('ignores an older History response for %s', async (_case, agentId) => {
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
    const { chatState, controller, runStream } = setupController({
      isDisplayedSession: () => true,
      operationOverrides: { loadChatHistory },
    });

    const olderLoad = controller.loadHistoryForSession('alpha', 'session-one');
    controller.renameAgent('alpha', agentId);
    const newerLoad = controller.loadHistoryForSession(agentId, 'session-one');
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
      ensureSessionState(chatState, agentId, 'session-one').messages,
    ).toEqual(newerHistory.messages);
    expect(Object.keys(chatState.sessions)).toEqual([
      `${agentId}::session-one`,
    ]);
    expect(runStream.attachRunStream).toHaveBeenCalledOnce();
    expect(runStream.attachRunStream).toHaveBeenCalledWith(
      ensureSessionState(chatState, agentId, 'session-one'),
      newerHistory.active_run,
    );
  });

  it('keeps history transport failures separate from Run failures', async () => {
    const loadChatHistory = vi.fn().mockRejectedValue(new Error('offline'));
    const { chatState, controller } = setupController({
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
});

describe('older History pages', () => {
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
    const { chatState, controller } = setupController({
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
      const { chatState, controller } = setupController({
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
          background_command_statuses: { term_old: 'running' },
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
      expect(sessionState.backgroundCommandStatuses).toEqual({});

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
      const { chatState, controller } = setupController({
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
    const { chatState, controller } = setupController({
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
});

describe('Run recovery', () => {
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
        runs: [{ run_id: 'run-final', status: 'completed', complete: true }],
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
    const { chatState, controller, runStream } = setupController({
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
    // History now carries the Run; no live projection remains.
    expect(
      visibleTimelineItemsForRender(sessionState).some(
        (item) => item.source === 'live',
      ),
    ).toBe(false);
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
    const { chatState, controller, runStream } = setupController({
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
    const { chatState, controller, runStream } = setupController({
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
    expect(
      countTimelineTextOccurrences(
        visibleTimelineItemsForRender(sessionState),
        'Recovered answer',
      ),
    ).toBe(1);
    expect(runStream.closeSubscriptionFor).not.toHaveBeenCalled();
  });

  it('reconciles a predecessor without replacing a successor observed during the History read', async () => {
    let resolveHistory;
    const loadChatHistory = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveHistory = resolve;
        }),
    );
    const { chatState, controller, runStream } = setupController({
      isDisplayedSession: () => true,
      operationOverrides: { loadChatHistory },
    });
    const session = ensureSessionState(chatState, 'alpha', 'session-one');
    startRun(session, {
      run_id: 'predecessor',
      status: 'running',
      sse_url: '/old',
    });
    const recovery = controller.reconcileRunSession(session, 'predecessor');
    await vi.waitFor(() => expect(loadChatHistory).toHaveBeenCalledOnce());
    startRun(session, {
      run_id: 'successor',
      status: 'running',
      sse_url: '/new',
    });
    appendRunEvent(session, {
      type: 'assistant_output_delta',
      run_id: 'successor',
      sequence: 1,
      payload: { content_delta: 'New answer' },
    });
    resolveHistory({
      active_run: { run_id: 'predecessor', status: 'running' },
      runs: [{ run_id: 'predecessor', status: 'completed', complete: true }],
      messages: [
        {
          id: 'old-answer',
          role: 'assistant',
          run_id: 'predecessor',
          content: 'Old final answer',
        },
        {
          id: 'old-summary',
          role: 'run_summary',
          run_id: 'predecessor',
          status: 'completed',
        },
      ],
    });
    await expect(recovery).resolves.toBe(true);
    expect(session.currentRun).toMatchObject({
      runId: 'successor',
      status: 'running',
      sseUrl: '/new',
    });
    expect(session.historyRuns.predecessor).toMatchObject({ complete: true });
    const timeline = visibleTimelineItemsForRender(session);
    expect(countTimelineTextOccurrences(timeline, 'Old final answer')).toBe(1);
    expect(countTimelineTextOccurrences(timeline, 'New answer')).toBe(1);
    expect(runStream.closeSubscriptionFor).not.toHaveBeenCalled();
    expect(runStream.attachRunStream).not.toHaveBeenCalled();
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
    const { chatState, controller } = setupController({
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
    const { chatState, controller, runStream } = setupController({
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
    const { chatState, controller } = setupController({
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
      const { chatState, controller } = setupController({
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
});
