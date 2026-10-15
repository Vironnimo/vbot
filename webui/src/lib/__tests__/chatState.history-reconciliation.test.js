import { describe, expect, it } from 'vitest';
import {
  appendRunEvent,
  createChatState,
  ensureSessionState,
  loadHistory,
  startRun,
} from '../chatState.js';
import { prependHistory } from '../chatState/history.js';
import { applyTimelineEvent } from '../chatTimeline.js';
import {
  assistantRun,
  messageItem,
  outputItem,
} from '../../components/__tests__/timelineItems.support.js';

const state = () => ensureSessionState(createChatState(), 'agent', 'session');
const projection = (session, runId, sequence, items) =>
  applyTimelineEvent(session, 'timeline_snapshot', {
    run_id: runId,
    sequence,
    items,
  });

describe('History and server Timeline synchronization', () => {
  it('keeps the newer active projection while merging an older History page', () => {
    const session = state();
    startRun(session, { run_id: 'active' });
    projection(session, 'active', 20, [
      assistantRun('active', [outputItem('Latest')]),
    ]);
    const live = session.timelineItems[0];
    loadHistory(session, [{ id: 'older', role: 'user', content: 'Earlier' }], {
      items: [
        messageItem({ id: 'older', role: 'user', content: 'Earlier' }),
        assistantRun('active', [outputItem('Stale')]),
      ],
      activeRunId: 'active',
      timelineSequence: 10,
    });
    expect(session.timelineItems[1]).toBe(live);
    expect(session.timelineItems[0].id).toBe('older');
    expect(session.timelineSequences.active).toBe(20);
  });

  it('replaces a completed Run atomically on an incremental History read and retires its events', () => {
    const session = state();
    const old = assistantRun('older', [outputItem('Earlier')], {
      source: 'history',
      status: 'completed',
    });
    loadHistory(session, [], { items: [old], generation: 'generation' });
    const loadedOld = session.timelineItems[0];
    startRun(session, { run_id: 'completed' });
    appendRunEvent(session, {
      run_id: 'completed',
      sequence: 1,
      type: 'run_completed',
    });
    projection(session, 'completed', 1, [
      assistantRun('completed', [outputItem('Provisional')]),
      messageItem({ id: 'provisional-error' }, { runId: 'completed' }),
    ]);
    const canonical = assistantRun('completed', [outputItem('Canonical')], {
      source: 'history',
      status: 'completed',
    });
    loadHistory(session, [], {
      generation: 'generation',
      incremental: true,
      runs: [{ run_id: 'completed', status: 'completed', complete: true }],
      items: [canonical],
      replaceRunIds: ['completed'],
    });
    expect(session.timelineItems).toEqual([old, canonical]);
    expect(session.timelineItems[0]).toBe(loadedOld);
    expect(session.runEvents).toEqual([]);
  });

  it('replaces the lineage when History generation changes despite newer local sequences', () => {
    const session = state();
    loadHistory(
      session,
      [{ id: 'old', role: 'user', content: 'Edited away' }],
      { items: [messageItem({ id: 'old' })], generation: 'before' },
    );
    startRun(session, { run_id: 'old-run' });
    projection(session, 'old-run', 100, [
      assistantRun('old-run', [outputItem('Old answer')]),
    ]);
    appendRunEvent(session, {
      run_id: 'old-run',
      sequence: 100,
      type: 'run_completed',
    });
    loadHistory(
      session,
      [{ id: 'replacement', role: 'user', content: 'New question' }],
      {
        generation: 'after',
        incremental: true,
        reset: true,
        items: [
          messageItem({
            id: 'replacement',
            role: 'user',
            content: 'New question',
          }),
        ],
      },
    );
    expect(session.messages.map((message) => message.id)).toEqual([
      'replacement',
    ]);
    expect(session.timelineItems.map((item) => item.id)).toEqual([
      'replacement',
    ]);
    expect(session.timelineSequences).toEqual({});
    expect(session.runEvents).toEqual([]);
    expect(session.currentRun).toBeNull();
    expect(session.status).toBe('idle');
  });

  it('prepends an older page without changing the active Run or its streamed children', () => {
    const session = state();
    startRun(session, { run_id: 'active' });
    projection(session, 'active', 10, [
      assistantRun('active', [outputItem('Current')]),
    ]);
    const live = session.timelineItems[0];
    const current = session.currentRun;
    prependHistory(
      session,
      [{ id: 'older', role: 'user', content: 'Earlier' }],
      {
        items: [messageItem({ id: 'older', role: 'user', content: 'Earlier' })],
        hasMore: true,
        nextBefore: 'cursor',
      },
    );
    expect(session.timelineItems.map((item) => item.id)).toEqual([
      'older',
      live.id,
    ]);
    expect(session.timelineItems[1]).toBe(live);
    expect(session.currentRun).toBe(current);
    expect(session.historyBefore).toBe('cursor');
  });

  it('does not mistake a matching message id for completed durable Run coverage', () => {
    const session = state();
    startRun(session, { run_id: 'one' });
    appendRunEvent(session, {
      run_id: 'one',
      sequence: 1,
      type: 'run_completed',
    });
    projection(session, 'one', 1, [
      assistantRun('one', [outputItem('Done')], { status: 'completed' }),
    ]);
    const events = session.runEvents;
    loadHistory(
      session,
      [{ id: 'output-one', role: 'assistant', content: 'Done' }],
      {
        items: [],
        incremental: true,
        runs: [{ run_id: 'one', complete: false }],
      },
    );
    expect(session.runEvents).toEqual(events);
    expect(session.timelineItems[0].outputs[0].content).toBe('Done');
  });
});
