import { describe, expect, it } from 'vitest';
import {
  createChatState,
  ensureSessionState,
  loadHistory,
  prependHistory,
  startRun,
  appendRunEvent,
  visibleTimelineItemsForRender,
} from '../chatState.js';
import { runProjectionPersistedInHistory } from '../chatTimeline.js';

const state = () =>
  ensureSessionState(createChatState(), 'agent@project', 'session');
const saved = (seq, run, role, content) => ({
  id: `message-${seq}`,
  history_sequence: seq,
  history_run_id: run,
  role,
  content,
});
const append = (session, run, sequence, type, payload = {}) =>
  appendRunEvent(session, {
    run_id: run,
    sequence,
    type,
    payload,
  });
const outputs = (session) =>
  visibleTimelineItemsForRender(session).flatMap((item) =>
    item.type === 'message'
      ? [item.message.content]
      : (item.outputs ?? []).map((output) => output.content),
  );

describe('authoritative Timeline synchronization', () => {
  it('joins a bounded replay to its canonical prefix without a User or overlapping event', () => {
    const session = state();
    loadHistory(
      session,
      [
        saved(0, 'older', 'assistant', 'Previous Run'),
        saved(1, 'automatic', 'assistant', 'Persisted prefix'),
      ],
      { generation: 'g' },
    );
    startRun(session, { run_id: 'automatic' });
    append(session, 'automatic', 900, 'assistant_output_delta', {
      content_delta: 'New output',
    });
    const timeline = visibleTimelineItemsForRender(session);
    expect(outputs(session)).toEqual([
      'Previous Run',
      'Persisted prefix',
      'New output',
    ]);
    expect(timeline.at(-1).runId).toBe('automatic');
    expect(timeline.at(-1).outputs.map((item) => item.content)).toEqual([
      'Persisted prefix',
      'New output',
    ]);
  });

  it('does not treat matching stable output IDs as proof that a terminal Run is fully persisted', () => {
    const session = state();
    const prefix = saved(0, 'first', 'assistant', 'Persisted prefix');
    startRun(session, { run_id: 'first' });
    append(session, 'first', 1, 'assistant_output', { message: prefix });
    append(session, 'first', 2, 'assistant_output_delta', {
      content_delta: 'Still not durable',
    });
    append(session, 'first', 3, 'run_completed', { status: 'completed' });
    startRun(session, { run_id: 'second' });
    loadHistory(session, [prefix], { generation: 'g' });
    expect(outputs(session)).toContain('Still not durable');
    expect(session.runEvents.some((event) => event.run_id === 'first')).toBe(
      true,
    );
    expect(
      runProjectionPersistedInHistory(
        session.runEvents,
        session.messages,
        'first',
      ),
    ).toBe(false);
  });

  it('retains older pages and distinct occurrences of the same checkpoint on an incremental read', () => {
    const session = state();
    const checkpoint = {
      id: 'same-checkpoint',
      role: 'compaction_checkpoint',
      content: 'Summary',
    };
    loadHistory(session, [{ ...checkpoint, history_sequence: 2 }], {
      generation: 'g',
      nextAfter: 'cursor-3',
      hasMore: true,
      nextBefore: 'before-2',
    });
    prependHistory(session, [
      saved(0, null, 'user', 'Earlier'),
      { ...checkpoint, history_sequence: 1 },
    ]);
    loadHistory(session, [saved(3, 'new', 'user', 'New')], {
      generation: 'g',
      incremental: true,
      nextAfter: 'cursor-4',
    });
    expect(session.messages.map((message) => message.history_sequence)).toEqual(
      [0, 1, 2, 3],
    );
    const ids = visibleTimelineItemsForRender(session).map((item) => item.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('a lineage reset removes edited-away live Runs while retaining the new active Run', () => {
    const session = state();
    loadHistory(session, [saved(0, 'old', 'user', 'Old')], {
      generation: 'g',
      nextAfter: 'old-cursor',
    });
    startRun(session, { run_id: 'old' });
    append(session, 'old', 1, 'assistant_output_delta', {
      content_delta: 'Removed output',
    });
    append(session, 'old', 2, 'run_completed', { status: 'completed' });
    startRun(session, { run_id: 'new' });
    append(session, 'new', 1, 'assistant_output_delta', {
      content_delta: 'Current output',
    });
    loadHistory(session, [saved(3, 'new', 'user', 'Replacement')], {
      generation: 'g',
      reset: true,
      activeRunId: 'new',
      nextAfter: 'new-cursor',
    });
    expect(outputs(session)).toEqual(['Replacement', 'Current output']);
    expect(session.runEvents.every((event) => event.run_id === 'new')).toBe(
      true,
    );
  });

  it('does not attach an identically worded User Message from another Run', () => {
    const session = state();
    const user = saved(0, 'previous', 'user', 'Again');
    loadHistory(session, [user]);
    startRun(session, { run_id: 'new' });
    append(session, 'new', 1, 'user_message_persisted', {
      message: { id: 'different', role: 'user', content: 'Again' },
    });
    expect(
      visibleTimelineItemsForRender(session).filter(
        (item) =>
          item.type === 'message' ||
          item.event?.type === 'user_message_persisted',
      ),
    ).toHaveLength(2);
  });

  it('does not apply a successor summary to an older Run with missing terminal persistence', () => {
    const session = state();
    loadHistory(session, [
      saved(0, 'older', 'user', 'First'),
      {
        ...saved(1, 'older', 'assistant', 'First output'),
        tool_calls: [{ id: 'call', name: 'read', arguments: {} }],
      },
      { ...saved(2, 'older', 'tool', 'Result'), tool_call_id: 'call' },
      {
        ...saved(3, 'new', 'run_summary', null),
        run_id: 'new',
        status: 'cancelled',
      },
    ]);
    const runs = visibleTimelineItemsForRender(session).filter(
      (item) => item.type === 'assistant_run',
    );
    expect(runs).toHaveLength(2);
    expect(runs[0].runId).toBe('older');
    expect(runs[0].outputs.map((item) => item.content)).toEqual([
      'First output',
    ]);
    expect(runs[1].runId).toBe('new');
    expect(runs[1].status).toBe('cancelled');
    expect(runs[1].items).toEqual([]);
  });

  it('only retires manual Compaction at the confirmed record in the same generation', () => {
    const events = [
      {
        run_id: 'compact',
        type: 'run_completed',
        payload: { history_checkpoint: { generation_id: 'g', sequence: 5 } },
      },
    ];
    const checkpoint = {
      id: 'same-id',
      role: 'compaction_checkpoint',
      history_sequence: 4,
    };
    expect(
      runProjectionPersistedInHistory(events, [checkpoint], 'compact', 'g'),
    ).toBe(false);
    expect(
      runProjectionPersistedInHistory(
        events,
        [{ ...checkpoint, history_sequence: 5 }],
        'compact',
        'other',
      ),
    ).toBe(false);
    expect(
      runProjectionPersistedInHistory(
        events,
        [{ ...checkpoint, history_sequence: 5 }],
        'compact',
        'g',
      ),
    ).toBe(true);
  });
});
