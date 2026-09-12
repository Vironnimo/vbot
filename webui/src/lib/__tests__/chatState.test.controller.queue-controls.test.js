import { describe, expect, it, vi } from 'vitest';
import {
  appendRunEvent,
  ensureSessionState,
  startRun,
  visibleTimelineItemsForRender,
} from '../chatState.js';
import { setup, deferred } from './chatState.controller.support.js';

describe('chat controller', () => {
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
});
