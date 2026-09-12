import { describe, expect, it, vi } from 'vitest';
import {
  appendRunEvent,
  ensureSessionState,
  startRun,
  visibleTimelineItemsForRender,
} from '../chatState.js';
import { setup } from './chatState.controller.support.js';

describe('chat controller', () => {
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
});
