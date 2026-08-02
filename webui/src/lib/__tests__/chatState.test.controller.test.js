import { describe, expect, it, vi } from 'vitest';

import {
  createChatController,
  createChatState,
  ensureSessionState,
  startRun,
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
    items: [{ id: 'queued-one', content: 'Next' }],
  });
  const onRestartQueueDiscarded = vi.fn();
  const controller = createChatController({
    chatState,
    runStream,
    operations: { listQueue, ...operationOverrides },
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
      { id: 'queued-one', content: 'Next', created_at: null },
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
          items: [{ id: 'kept', content: 'Server text' }],
        },
      ],
    });

    expect(identitySession.queue).toEqual([]);
    expect(projectSession.queue).toEqual([
      { id: 'kept', content: 'Server text', created_at: null },
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

  it('merges the terminal cancel response into the active Run projection', async () => {
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
    const { chatState, controller, runStream } = setup({
      operationOverrides: { cancelRun },
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
    expect(chatState.cancellingRun).toBe(false);
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
    const listSessions = vi.fn(async (agentId) => ({
      sessions:
        agentId === 'alpha'
          ? [
              {
                id: 'session-one',
                has_unread_completion: true,
                unread_run_id: 'run-one',
                unread_run_status: 'completed',
                unread_run_at: '2026-07-20T10:00:00+00:00',
              },
            ]
          : [],
    }));
    const { chatState, controller } = setup({
      operationOverrides: { listSessions },
    });

    await controller.refreshAgentActivity(['alpha', 'beta', 'alpha']);

    expect(listSessions).toHaveBeenCalledTimes(2);
    expect(ensureSessionState(chatState, 'alpha', 'session-one')).toMatchObject(
      {
        hasUnreadCompletion: true,
        unreadRunId: 'run-one',
        unreadRunStatus: 'completed',
      },
    );
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
    const listSessions = vi.fn(
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
      operationOverrides: { listSessions, markSessionRead },
    });
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    sessionState.hasUnreadCompletion = true;
    sessionState.unreadRunId = 'run-one';
    const refresh = controller.refreshAgentActivity(['alpha']);

    await controller.markSessionCompletionRead(sessionState);
    resolveList({
      sessions: [
        {
          id: 'session-one',
          has_unread_completion: true,
          unread_run_id: 'run-one',
          unread_run_status: 'completed',
          unread_run_at: '2026-07-20T10:00:00+00:00',
        },
      ],
    });

    expect(await refresh).toBe(false);
    expect(sessionState.hasUnreadCompletion).toBe(false);
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
});
