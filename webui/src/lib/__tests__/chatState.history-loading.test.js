import { describe, expect, it, vi } from 'vitest';
import {
  assistantRun,
  messageItem,
  outputItem,
} from '../../components/__tests__/timelineItems.support.js';
import {
  appendRunEvent,
  setAgents,
  selectAgent,
  ensureSessionState,
  startRun,
  visibleTimelineItemsForRender,
} from '../chatState.js';
import {
  applyOutputSnapshot,
  countTimelineTextOccurrences,
  deferred,
  reflectionRun,
  setupController,
} from './chatState.support.js';

describe('History reads', () => {
  it('focuses the selected Run while preserving live History and ignoring an older focus read', async () => {
    const old = deferred();
    const current = deferred();
    const loadChatHistory = vi
      .fn()
      .mockReturnValueOnce(old.promise)
      .mockReturnValueOnce(current.promise);
    const { chatState, controller } = setupController({
      operationOverrides: { loadChatHistory },
    });
    const session = ensureSessionState(chatState, 'alpha', 'session');
    session.messages = [
      { id: 'live', role: 'user', content: 'Current conversation' },
    ];
    const first = controller.focusRun(session, 'first');
    const second = controller.focusRun(session, 'second');
    expect(loadChatHistory).toHaveBeenLastCalledWith({
      agent_id: 'alpha',
      session_id: 'session',
      focus_run_id: 'second',
      child_limit: 100,
    });
    current.resolve({
      messages: [
        { id: 'second-answer', role: 'assistant', content: 'Selected result' },
      ],
      items: [],
    });
    expect(await second).toBe(true);
    old.resolve({
      messages: [
        { id: 'first-answer', role: 'assistant', content: 'Earlier result' },
      ],
      items: [],
    });
    expect(await first).toBe(false);
    expect(session.focusedHistory.focusRunId).toBe('second');
    expect(session.focusedHistory.messages[0].content).toBe('Selected result');
    expect(session.messages[0].content).toBe('Current conversation');
  });

  it('ignores a child page once its Run gap is gone', async () => {
    const pending = deferred();
    const { chatState, controller } = setupController({
      operationOverrides: {
        loadTimelineChildren: vi.fn().mockReturnValue(pending.promise),
        loadChatHistory: vi.fn().mockResolvedValue({
          messages: [],
          history_generation: 'g',
          items: [
            assistantRun('one', [
              outputItem('Fresh', { id: 'fresh', position: 5 }),
            ]),
          ],
        }),
      },
    });
    await controller.loadHistoryForSession('alpha', 'one');
    const session = ensureSessionState(chatState, 'alpha', 'one');
    const reading = controller.loadRunHistory(session, {
      run_id: 'one',
      before: 'fresh',
    });
    await controller.loadHistoryForSession('alpha', 'one');
    pending.resolve({
      run_id: 'one',
      history_generation: 'g',
      before: 'fresh',
      items: [outputItem('Stale', { id: 'stale', position: 4 })],
    });
    expect(await reading).toBe(false);
    expect(session.timelineItems[0].items.map((item) => item.id)).toEqual([
      'fresh',
    ]);
  });

  it('evicts departed histories while retaining their Queue state', async () => {
    const { chatState, controller } = setupController({
      operationOverrides: {
        loadChatHistory: vi.fn().mockImplementation(async ({ session_id }) => ({
          messages: [{ id: session_id, role: 'user', content: 'Saved' }],
          items: [
            messageItem({ id: session_id, role: 'user', content: 'Saved' }),
          ],
        })),
      },
    });
    for (let i = 0; i < 10; i++) {
      await controller.loadHistoryForSession('alpha', `session-${i}`);
      ensureSessionState(chatState, 'alpha', `session-${i}`).queue = [
        { id: `queued-${i}` },
      ];
    }
    const first = chatState.sessions['alpha::session-0'];
    expect(first.timelineItems).toEqual([]);
    expect(first.historyLoaded).toBe(false);
    expect(first.queue).toEqual([{ id: 'queued-0' }]);
    expect(chatState.sessions['alpha::session-9'].timelineItems).toHaveLength(
      1,
    );
  });

  it('releases a departed History load on a draft and ignores its late completion', async () => {
    const old = deferred();
    const next = deferred();
    let displayed = 'alpha';
    const { chatState, controller } = setupController({
      isDisplayedSession: (agent) => agent === displayed,
      operationOverrides: {
        loadChatHistory: vi
          .fn()
          .mockReturnValueOnce(old.promise)
          .mockReturnValueOnce(next.promise),
      },
    });
    setAgents(chatState, [
      { id: 'alpha', current_session_id: 'one' },
      { id: 'beta', current_session_id: '' },
    ]);
    const first = controller.loadHistoryForSession('alpha', 'one');
    expect(chatState.loadingHistory).toBe(true);
    displayed = 'beta';
    selectAgent(chatState, 'beta');
    await controller.loadCurrentHistory();
    expect(chatState.loadingHistory).toBe(false);
    displayed = 'gamma';
    const second = controller.loadHistoryForSession('gamma', 'two');
    old.resolve({ messages: [] });
    await first;
    expect(chatState.loadingHistory).toBe(true);
    next.resolve({ messages: [] });
    await second;
    expect(chatState.loadingHistory).toBe(false);
  });
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
        last_usage: { input_tokens: 8 },
        context_usage: { used_tokens: 5 },
        compaction_policy: policy,
        background_command_statuses: { cmd_one: 'completed' },
        background_commands_still_running: ['cmd_one'],
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
      child_limit: 100,
      after: 'cursor-1',
    });
    expect(source.messages.map((message) => message.id)).toEqual(['reply']);
    expect(source.sessionUsage).toEqual({ input_tokens: 10 });
    expect(source.usage).toEqual({ input_tokens: 8 });
    expect(source.contextUsage).toEqual({ used_tokens: 5 });
    expect(source.compactionPolicy).toBe(policy);
    expect(source.backgroundCommandStatuses).toEqual({
      cmd_one: { status: 'completed', stillRunning: true },
    });

    // A policy refreshed from another window while History is in flight must
    // not be overwritten by that older snapshot; its messages still apply.
    const pendingHistory = deferred();
    loadChatHistory.mockReturnValueOnce(pendingHistory.promise);
    startRun(source, { run_id: 'run-live' });
    const loading = controller.loadHistoryForSession('alpha', 'source');
    const changedPolicy = { ...policy, enabled: false };
    controller.applySessionCompactionPolicy('alpha', 'source', changedPolicy);
    appendRunEvent(source, {
      type: 'model_step_usage',
      run_id: 'run-live',
      sequence: 1,
      payload: {
        usage: { input_tokens: 20 },
        session_usage: { input_tokens: 30 },
        context_usage: { used_tokens: 25, context_window: 100 },
      },
    });
    pendingHistory.resolve({
      messages: [{ id: 'next', role: 'assistant', content: 'Next' }],
      incremental: true,
      compaction_policy: policy,
      last_usage: { input_tokens: 12 },
      session_usage: { input_tokens: 12 },
      context_usage: { used_tokens: 7, context_window: 100 },
      active_run: { run_id: 'run-live' },
    });
    await loading;
    expect(source.compactionPolicy).toBe(changedPolicy);
    expect(source.usage).toEqual({ input_tokens: 20 });
    expect(source.sessionUsage).toEqual({ input_tokens: 30 });
    expect(source.contextUsage).toEqual({
      used_tokens: 25,
      context_window: 100,
    });
    expect(source.messages.at(-1).id).toBe('next');

    const replacement = deferred();
    loadChatHistory.mockReturnValueOnce(replacement.promise);
    const replacing = controller.loadHistoryForSession('alpha', 'source');
    appendRunEvent(source, {
      type: 'model_step_usage',
      run_id: 'run-live',
      sequence: 2,
      payload: {
        usage: { input_tokens: 40 },
        session_usage: { input_tokens: 70 },
        context_usage: { used_tokens: 50 },
      },
    });
    replacement.resolve({
      messages: [],
      history_generation: 'g2',
      last_usage: null,
      session_usage: { input_tokens: 0 },
      context_usage: null,
    });
    await replacing;
    expect(source.usage).toBeNull();
    expect(source.sessionUsage).toEqual({ input_tokens: 0 });
    expect(source.contextUsage).toBeNull();
  });

  it('folds background status deltas, the newest still-running lists and Run records across incremental pages', async () => {
    const loadChatHistory = vi
      .fn()
      .mockResolvedValueOnce({
        messages: [],
        history_generation: 'g1',
        next_after: 'cursor-1',
        incremental: false,
        context_usage: { used_tokens: 5 },
        background_command_statuses: {
          cmd_one: 'running',
          cmd_two: 'running',
        },
        background_commands_still_running: [],
        subagent_background_commands: [
          subAgentCommand('cmd_child', 'session-child'),
        ],
      })
      .mockResolvedValueOnce({
        messages: [],
        history_generation: 'g1',
        next_after: 'cursor-2',
        incremental: true,
        has_newer: true,
        context_usage: { used_tokens: 7 },
        last_usage: { input_tokens: 11 },
        background_command_statuses: { cmd_one: 'completed' },
        background_commands_still_running: ['cmd_one'],
        subagent_background_commands: [
          subAgentCommand('cmd_child', 'session-child'),
        ],
        runs: [{ run_id: 'run-one', status: 'completed' }],
      })
      .mockResolvedValueOnce({
        messages: [],
        history_generation: 'g1',
        next_after: 'cursor-3',
        incremental: true,
        context_usage: null,
        background_command_statuses: { cmd_two: 'failed' },
        background_commands_still_running: [],
        subagent_background_commands: [
          subAgentCommand('cmd_grandchild', 'session-grandchild'),
          { subagent_id: 'sub_child', command_id: 'cmd_without_session' },
        ],
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
    expect(source.usage).toEqual({ input_tokens: 11 });
    expect(source.backgroundCommandStatuses).toEqual({
      cmd_one: { status: 'completed', stillRunning: false },
      cmd_two: { status: 'failed', stillRunning: false },
    });
    // What the Sub-Agents below still run is listed in full each time.
    expect(source.subAgentBackgroundCommands).toEqual([
      {
        subAgentId: 'sub_child',
        sessionId: 'session-grandchild',
        commandId: 'cmd_grandchild',
        label: 'npm run dev',
      },
    ]);
    expect(Object.keys(source.historyRuns).sort()).toEqual([
      'run-one',
      'run-two',
    ]);
    // A page that read the context and found none clears it.
    expect(source.contextUsage).toBeNull();
  });

  it.each([
    ['gap', true],
    ['epoch_changed', true],
    ['resumed', false],
  ])(
    'after a %s reconnect, reads the shown History on only when it may have missed an end',
    async (replayStatus, readOn) => {
      const loadChatHistory = vi
        .fn()
        .mockResolvedValueOnce({
          messages: [{ id: 'reply', role: 'assistant', content: 'Hello' }],
          history_generation: 'g1',
          next_after: 'cursor-1',
          next_before: 'before-1',
          has_more: true,
          incremental: false,
          background_command_statuses: {
            cmd_one: 'running',
            cmd_two: 'running',
            cmd_three: 'completed',
          },
          background_commands_still_running: [],
        })
        .mockResolvedValueOnce({
          messages: [{ id: 'older', role: 'user', content: 'Hi' }],
          has_more: false,
        })
        // vBot restarted while cmd_one ran; it reports every live command.
        .mockResolvedValueOnce({
          messages: [],
          history_generation: 'g1',
          next_after: 'cursor-1',
          incremental: true,
          history_reset: false,
          runs: [],
          background_command_statuses: { cmd_two: 'running' },
          background_commands_still_running: [],
        });
      const loadReflectionRuns = vi
        .fn()
        .mockResolvedValue({ reflection_runs: [] });
      const { chatState, controller } = setupController({
        operationOverrides: { loadChatHistory, loadReflectionRuns },
        isDisplayedSession: () => true,
      });
      await controller.loadHistoryForSession('alpha', 'shown');
      const shown = chatState.sessions['alpha::shown'];
      await controller.loadOlderHistory(shown);

      controller.applyConnectionSnapshot({
        replay_status: replayStatus,
        active_runs: [],
      });
      // A read on carries no reflections; they are read on their own.
      await vi.waitFor(() => expect(loadReflectionRuns).toHaveBeenCalled());
      if (readOn) {
        await vi.waitFor(() =>
          expect(shown.backgroundCommandStatuses.cmd_one.status).toBe('ended'),
        );
        expect(loadChatHistory).toHaveBeenLastCalledWith({
          agent_id: 'alpha',
          session_id: 'shown',
          limit: 500,
          child_limit: 100,
          after: 'cursor-1',
        });
      } else {
        expect(loadChatHistory).toHaveBeenCalledTimes(2);
      }
      expect(shown.backgroundCommandStatuses).toEqual({
        cmd_one: { status: readOn ? 'ended' : 'running', stillRunning: false },
        cmd_two: { status: 'running', stillRunning: false },
        cmd_three: { status: 'completed', stillRunning: false },
      });
      // The older pages the user loaded stay.
      expect(shown.messages.map((message) => message.id)).toEqual([
        'older',
        'reply',
      ]);
    },
  );

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
        items: [
          messageItem({
            id: 'new-user',
            role: 'user',
            content: 'New question',
          }),
        ],
        timeline_sequence: 0,
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
    applyOutputSnapshot(session, 'new-run', 1, 'Fresh answer');
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
      child_limit: 100,
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

      expect(await controller.loadOlderHistory(sessionState)).toBe(true);
      expect(loadChatHistory).toHaveBeenLastCalledWith({
        agent_id: 'alpha',
        session_id: 'session-one',
        limit: 50,
        child_limit: 100,
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
        items: [
          messageItem({
            id: 'user-final',
            role: 'user',
            content: 'Finish the work',
          }),
          assistantRun('run-final', [outputItem('Final answer')], {
            status: 'completed',
            source: 'history',
          }),
        ],
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
    applyOutputSnapshot(sessionState, 'run-final', 2, 'Final answer');
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
      items: [
        messageItem({
          id: 'user-final',
          role: 'user',
          content: 'Finish the work',
        }),
      ],
      timeline_sequence: 1,
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
      child_limit: 100,
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
    applyOutputSnapshot(sessionState, 'run-recovering', 1, 'Recovered answer');
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
    applyOutputSnapshot(session, 'successor', 1, 'New answer');
    resolveHistory({
      active_run: { run_id: 'predecessor', status: 'running' },
      runs: [{ run_id: 'predecessor', status: 'completed', complete: true }],
      items: [
        assistantRun('predecessor', [outputItem('Old final answer')], {
          status: 'completed',
          source: 'history',
        }),
      ],
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
      runs: ['run-one', 'run-two', 'run-stalled'].map((run_id) => ({
        run_id,
        status: 'completed',
        complete: true,
      })),
      items: [
        {
          id: 'compaction-one',
          type: 'compaction_separator',
          status: 'completed',
        },
        messageItem(durableMessages[1]),
        assistantRun('run-one', [outputItem('First answer')], {
          status: 'completed',
          source: 'history',
        }),
        messageItem(durableMessages[4]),
        assistantRun('run-two', [outputItem('Second answer')], {
          status: 'completed',
          source: 'history',
        }),
        messageItem(durableMessages[7]),
        assistantRun('run-stalled', [outputItem('Latest answer')], {
          status: 'completed',
          source: 'history',
        }),
      ],
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

// A command a Sub-Agent below the Session still runs, as `chat.history` lists
// it under the Session's own Sub-Agent above it.
function subAgentCommand(commandId, sessionId) {
  return {
    subagent_id: 'sub_child',
    session_id: sessionId,
    command_id: commandId,
    label: 'npm run dev',
  };
}
