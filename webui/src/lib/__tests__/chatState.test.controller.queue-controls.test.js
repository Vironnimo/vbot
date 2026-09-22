import { describe, expect, it, vi } from 'vitest';
import {
  appendRunEvent,
  ensureSessionState,
  startRun,
  visibleTimelineItemsForRender,
} from '../chatState.js';
import { setup, deferred } from './chatState.controller.support.js';

describe('chat controller', () => {
  it('steers the exact active Run and reconciles a consumed item without starting another Run', async () => {
    const steerQueueItem = vi.fn().mockResolvedValue({ run_id: 'r' });
    const listQueue = vi.fn().mockResolvedValue({ items: [] });
    const { chatState, controller, runStream } = setup({
      operationOverrides: { steerQueueItem, listQueue },
    });
    const session = ensureSessionState(chatState, 'coder@project', 'one');
    session.currentRun = { runId: 'r', status: 'running' };
    session.queue = [{ id: 'q', content: 'Correction', steerable: true }];
    expect(await controller.steerQueued(session, 'q')).toBe(true);
    expect(steerQueueItem).toHaveBeenCalledWith(
      'coder@project',
      'one',
      'q',
      'r',
    );
    expect(session.queue).toEqual([]);
    expect(session.currentRun.runId).toBe('r');
    expect(runStream.attachRunStream).not.toHaveBeenCalled();
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
      {
        id: 'queued-one',
        content: 'Next',
        editable: true,
        created_at: null,
        steerable: false,
        steering: false,
      },
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
      {
        id: 'kept',
        content: 'Server text',
        editable: true,
        created_at: null,
        steerable: false,
        steering: false,
      },
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
        steerable: false,
        steering: false,
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

  it.each(['edit', 'remove', 'enqueue'])(
    'keeps an accepted Queue %s when an earlier list response arrives later',
    async (mutation) => {
      const stale = deferred();
      const { chatState, controller } = setup({
        operationOverrides: {
          listQueue: vi.fn().mockReturnValue(stale.promise),
          updateQueueItem: vi.fn().mockResolvedValue({ ok: true }),
          removeFromQueue: vi.fn().mockResolvedValue({ ok: true }),
          startChatRun: vi.fn().mockResolvedValue({
            queued: true,
            item: { id: 'new', content: 'New queued text', editable: true },
          }),
        },
      });
      const session = ensureSessionState(chatState, 'alpha', 'one');
      const original = { id: 'old', content: 'Original', editable: true };
      session.queue = [structuredClone(original)];
      const pendingSync = controller.syncSessionQueue(session);
      if (mutation === 'edit')
        await controller.updateQueued(session, 'old', 'Edited');
      if (mutation === 'remove') await controller.removeQueued(session, 'old');
      if (mutation === 'enqueue')
        await controller.sendMessage(session, 'New queued text');
      const accepted = structuredClone(session.queue);

      stale.resolve({ items: [original] });
      await pendingSync;

      expect(session.queue).toEqual(accepted);
    },
  );

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
      runs: [{ run_id: 'run-cancelled', status: 'cancelled', complete: true }],
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
    expect(sessionState.cancellingRunIds).toEqual([]);
  });

  it('keeps pending cancellation on its exact Session and Run across navigation and successors', async () => {
    const first = deferred();
    const second = deferred();
    const successor = deferred();
    const cancelRun = vi
      .fn()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)
      .mockReturnValueOnce(successor.promise);
    const { chatState, controller } = setup({
      operationOverrides: { cancelRun },
    });
    const a = ensureSessionState(chatState, 'alpha', 'one');
    const b = ensureSessionState(chatState, 'beta', 'two');
    a.currentRun = { runId: 'a', status: 'running' };
    b.currentRun = { runId: 'b', status: 'running' };
    const cancellingA = controller.cancelActiveRun(a);
    await controller.cancelActiveRun(a);
    expect(cancelRun).toHaveBeenCalledTimes(1);
    expect(a.cancellingRunIds).toEqual(['a']);
    expect(b.cancellingRunIds).toEqual([]);
    const cancellingB = controller.cancelActiveRun(b);
    a.currentRun = { runId: 'a-next', status: 'running' };
    expect(a.cancellingRunIds).not.toContain('a-next');
    const cancellingNext = controller.cancelActiveRun(a);
    first.reject(new Error('old cancellation failed'));
    await cancellingA;
    expect(a.actionError).toBe('');
    expect(a.cancellingRunIds).toEqual(['a-next']);
    expect(b.cancellingRunIds).toEqual(['b']);
    second.reject(new Error('cancel b failed'));
    successor.reject(new Error('cancel successor failed'));
    await Promise.all([cancellingB, cancellingNext]);
    expect(a.cancellingRunIds).toEqual([]);
    expect(b.cancellingRunIds).toEqual([]);
    expect(cancelRun.mock.calls.map(([id]) => id)).toEqual([
      'a',
      'b',
      'a-next',
    ]);
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
      isDisplayedSession: () => true,
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
      isDisplayedSession: () => true,
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

  it.each(['send', 'edit'])(
    'does not reopen a hidden Session stream when %s admission returns after navigation',
    async (action) => {
      const response = deferred();
      let displayedSessionId = 'one';
      const { chatState, controller, runStream } = setup({
        isDisplayedSession: (_agentId, sessionId) =>
          sessionId === displayedSessionId,
        operationOverrides: {
          startChatRun: vi.fn(() => response.promise),
          editChatMessage: vi.fn(() => response.promise),
        },
      });
      const session = ensureSessionState(chatState, 'alpha', 'one');
      session.messages = [{ id: 'target', role: 'user', content: 'Old' }];
      const admission =
        action === 'send'
          ? controller.sendMessage(session, 'New')
          : controller.editMessage(session, 'target', 'New');
      displayedSessionId = 'two';
      response.resolve({ run_id: 'run-one', sse_url: '/events/run-one' });
      await expect(admission).resolves.toEqual({
        kind: 'started',
        runId: 'run-one',
      });
      expect(session.currentRun.runId).toBe('run-one');
      expect(runStream.subscribeToRun).not.toHaveBeenCalled();
    },
  );
});
