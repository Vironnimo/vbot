import { describe, expect, it } from 'vitest';
import {
  appendRunEvent,
  createChatState,
  ensureSessionState,
  loadHistory,
  startRun,
  visibleTimelineItemsForRender,
} from '../chatState.js';
import { pruneRunEventsPersistedInHistory } from '../chatTimeline.js';
import {
  CHAT_STATUS_RUNNING,
  CHAT_STATUS_COMPLETED,
} from './chatTimeline.support.js';

describe('manual compaction run dedup against persisted history', () => {
  const COMPACTION_TIMESTAMP = '2026-08-27T13:48:00Z';

  function manualCompactionRunEvents(runId) {
    return [
      {
        type: 'run_started',
        run_id: runId,
        sequence: 1,
        timestamp: '2026-08-27T13:47:00Z',
        payload: { status: CHAT_STATUS_RUNNING },
      },
      {
        type: 'compaction_started',
        run_id: runId,
        sequence: 2,
        timestamp: '2026-08-27T13:47:01Z',
        payload: { context_tokens_before: 245_114 },
      },
      {
        type: 'compaction_completed',
        run_id: runId,
        sequence: 3,
        timestamp: COMPACTION_TIMESTAMP,
        payload: {
          context_tokens_before: 245_114,
          context_tokens_after: 40_289,
          message: {
            id: 'checkpoint-1',
            role: 'compaction_checkpoint',
            timestamp: COMPACTION_TIMESTAMP,
          },
        },
      },
      {
        type: 'run_completed',
        run_id: runId,
        sequence: 4,
        timestamp: '2026-08-27T13:48:01Z',
        payload: { status: CHAT_STATUS_COMPLETED },
      },
    ];
  }

  it('prunes every event of a finished compaction run once its checkpoint is persisted', () => {
    const messages = [
      { id: 'user-1', role: 'user', content: 'Earlier turn' },
      { id: 'assistant-1', role: 'assistant', content: 'Earlier answer' },
      {
        id: 'checkpoint-1',
        role: 'compaction_checkpoint',
        timestamp: COMPACTION_TIMESTAMP,
      },
    ];

    const pruned = pruneRunEventsPersistedInHistory(
      manualCompactionRunEvents('run-compaction'),
      messages,
      'run-next',
    );

    expect(pruned).toEqual([]);
  });

  it('renders the persisted checkpoint once while a newer run streams', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-compaction-dedup',
    );
    loadHistory(sessionState, [
      {
        id: 'user-1',
        role: 'user',
        content: 'Earlier turn',
        timestamp: '2026-08-27T13:40:00Z',
      },
      {
        id: 'assistant-1',
        role: 'assistant',
        content: 'Earlier answer',
        timestamp: '2026-08-27T13:46:00Z',
      },
      {
        id: 'summary-prev',
        role: 'run_summary',
        run_id: 'run-prev',
        status: 'completed',
        timestamp: '2026-08-27T13:46:01Z',
        timing: { duration_ms: 87_000 },
        iteration_count: 1,
      },
      {
        id: 'checkpoint-1',
        role: 'compaction_checkpoint',
        timestamp: COMPACTION_TIMESTAMP,
      },
      {
        id: 'user-2',
        role: 'user',
        content: 'ja, klingt gut. setz das mal um.',
        timestamp: '2026-08-27T13:48:05Z',
      },
    ]);
    for (const event of manualCompactionRunEvents('run-compaction')) {
      appendRunEvent(sessionState, event);
    }
    startRun(sessionState, {
      run_id: 'run-next',
      sse_url: '/api/runs/run-next/events',
      status: CHAT_STATUS_RUNNING,
    });
    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-next',
      sequence: 1,
      payload: {
        message: {
          id: 'user-2',
          role: 'user',
          content: 'ja, klingt gut. setz das mal um.',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-next',
      sequence: 2,
      payload: {
        message: {
          id: 'assistant-2',
          role: 'assistant',
          content: 'Umsetzung läuft.',
        },
      },
    });

    const items = visibleTimelineItemsForRender(sessionState);

    expect(
      items.some(
        (item) =>
          item.type === 'assistant_run' && item.runId === 'run-compaction',
      ),
    ).toBe(false);
    expect(
      items.filter((item) => item.type === 'compaction_separator'),
    ).toHaveLength(1);
  });
});

describe('standalone compaction run renders bare', () => {
  function standaloneCompactionRunEvents(runId) {
    return [
      {
        type: 'run_started',
        run_id: runId,
        sequence: 1,
        timestamp: '2026-08-27T14:00:00Z',
        payload: { status: CHAT_STATUS_RUNNING },
      },
      {
        type: 'compaction_started',
        run_id: runId,
        sequence: 2,
        timestamp: '2026-08-27T14:00:01Z',
        payload: { context_tokens_before: 120_000 },
      },
      {
        type: 'compaction_completed',
        run_id: runId,
        sequence: 3,
        timestamp: '2026-08-27T14:00:55Z',
        payload: {
          context_tokens_before: 120_000,
          context_tokens_after: 30_000,
          duration_ms: 54_000,
          message: {
            id: 'checkpoint-live',
            role: 'compaction_checkpoint',
            timestamp: '2026-08-27T14:00:55Z',
          },
        },
      },
      {
        type: 'run_completed',
        run_id: runId,
        sequence: 4,
        timestamp: '2026-08-27T14:00:56Z',
        payload: { status: CHAT_STATUS_COMPLETED },
      },
    ];
  }

  it('renders a finished standalone compaction run as a bare separator, not a run block', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-compaction-bare',
    );
    loadHistory(sessionState, [
      {
        id: 'user-1',
        role: 'user',
        content: 'Earlier turn',
        timestamp: '2026-08-27T13:50:00Z',
      },
    ]);
    for (const event of standaloneCompactionRunEvents('run-compaction')) {
      appendRunEvent(sessionState, event);
    }

    const items = visibleTimelineItemsForRender(sessionState);

    expect(
      items.some(
        (item) =>
          item.type === 'assistant_run' && item.runId === 'run-compaction',
      ),
    ).toBe(false);
    const separators = items.filter(
      (item) => item.type === 'compaction_separator',
    );
    expect(separators).toHaveLength(1);
    expect(separators[0].status).toBe(CHAT_STATUS_COMPLETED);
    expect(separators[0].message?.id).toBe('checkpoint-live');
    expect(separators[0].durationMs).toBe(54_000);
  });

  it('renders a reloaded checkpoint with the duration stamped in its usage', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-compaction-bare-history',
    );
    loadHistory(sessionState, [
      {
        id: 'user-1',
        role: 'user',
        content: 'Earlier turn',
        timestamp: '2026-08-27T13:50:00Z',
      },
      {
        id: 'checkpoint-1',
        role: 'compaction_checkpoint',
        timestamp: '2026-08-27T14:00:55Z',
        usage: {
          context_tokens_before: 120_000,
          context_tokens_after: 30_000,
          compaction_duration_ms: 54_000,
        },
      },
    ]);

    const items = visibleTimelineItemsForRender(sessionState);
    const separators = items.filter(
      (item) => item.type === 'compaction_separator',
    );

    expect(separators).toHaveLength(1);
    expect(separators[0].durationMs).toBe(54_000);
  });

  it('keeps the run block for an in-run auto compaction beside its other children', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-compaction-auto-inline',
    );
    loadHistory(sessionState, [
      {
        id: 'user-1',
        role: 'user',
        content: 'Question',
        timestamp: '2026-08-27T13:50:00Z',
      },
    ]);
    startRun(sessionState, {
      run_id: 'run-chat',
      sse_url: '/api/runs/run-chat/events',
      status: CHAT_STATUS_RUNNING,
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-chat',
      sequence: 1,
      timestamp: '2026-08-27T13:51:00Z',
      payload: {
        message: {
          id: 'assistant-1',
          role: 'assistant',
          content: 'Working on it.',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'compaction_started',
      run_id: 'run-chat',
      sequence: 2,
      timestamp: '2026-08-27T13:51:05Z',
      payload: { context_tokens_before: 200_000 },
    });
    appendRunEvent(sessionState, {
      type: 'compaction_completed',
      run_id: 'run-chat',
      sequence: 3,
      timestamp: '2026-08-27T13:51:40Z',
      payload: {
        context_tokens_before: 200_000,
        context_tokens_after: 45_000,
        duration_ms: 35_000,
        message: {
          id: 'checkpoint-auto',
          role: 'compaction_checkpoint',
          timestamp: '2026-08-27T13:51:40Z',
        },
      },
    });

    const items = visibleTimelineItemsForRender(sessionState);
    const runBlock = items.find(
      (item) => item.type === 'assistant_run' && item.runId === 'run-chat',
    );

    expect(runBlock).toBeTruthy();
    expect(
      runBlock.items.filter((child) => child.type === 'compaction_separator'),
    ).toHaveLength(1);
    expect(
      items.filter((item) => item.type === 'compaction_separator'),
    ).toHaveLength(0);
  });

  it('keeps the failed run block visible when a standalone compaction aborts', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-compaction-failed',
    );
    loadHistory(sessionState, [
      {
        id: 'user-1',
        role: 'user',
        content: 'Earlier turn',
        timestamp: '2026-08-27T13:50:00Z',
      },
    ]);
    for (const event of [
      {
        type: 'run_started',
        run_id: 'run-compaction-failed',
        sequence: 1,
        timestamp: '2026-08-27T14:00:00Z',
        payload: { status: CHAT_STATUS_RUNNING },
      },
      {
        type: 'compaction_started',
        run_id: 'run-compaction-failed',
        sequence: 2,
        timestamp: '2026-08-27T14:00:01Z',
        payload: { context_tokens_before: 120_000 },
      },
      {
        type: 'compaction_aborted',
        run_id: 'run-compaction-failed',
        sequence: 3,
        timestamp: '2026-08-27T14:00:20Z',
        payload: { reason: 'failed' },
      },
      {
        type: 'run_failed',
        run_id: 'run-compaction-failed',
        sequence: 4,
        timestamp: '2026-08-27T14:00:21Z',
        payload: { status: 'failed', error: 'Provider request failed' },
      },
    ]) {
      appendRunEvent(sessionState, event);
    }

    const items = visibleTimelineItemsForRender(sessionState);

    expect(
      items.filter((item) => item.type === 'compaction_separator'),
    ).toHaveLength(0);
    const runBlock = items.find(
      (item) =>
        item.type === 'assistant_run' && item.runId === 'run-compaction-failed',
    );
    expect(runBlock).toBeTruthy();
    expect(runBlock.status).toBe('failed');
  });
});
