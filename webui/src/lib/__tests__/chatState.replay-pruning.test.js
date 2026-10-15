import { describe, expect, it } from 'vitest';
import {
  appendRunEvent,
  createChatState,
  ensureSessionState,
  loadHistory,
  releaseFinishedRunEvents,
  resetStaleRun,
  startRun,
} from '../chatState.js';
import {
  assistantRun,
  outputItem,
} from '../../components/__tests__/timelineItems.support.js';

const state = () => ensureSessionState(createChatState(), 'alpha', 'session');
const event = (runId, type, sequence = 1) => ({
  run_id: runId,
  type,
  sequence,
  payload: {},
});

describe('retained Run lifecycle events', () => {
  it.each([true, false])(
    'retires terminal events only when History proves completion (%s)',
    (complete) => {
      const session = state();
      appendRunEvent(session, event('finished', 'run_started'));
      appendRunEvent(session, event('finished', 'run_completed', 2));
      startRun(session, {
        run_id: 'active',
        events: [event('active', 'run_started')],
      });
      loadHistory(session, [], {
        items: [],
        incremental: true,
        activeRunId: 'active',
        runs: [{ run_id: 'finished', status: 'completed', complete }],
      });
      expect(
        session.runEvents.map(({ run_id, sequence }) => [run_id, sequence]),
      ).toEqual(
        complete
          ? [['active', 1]]
          : [
              ['finished', 1],
              ['finished', 2],
              ['active', 1],
            ],
      );
      expect(session.currentRun.runId).toBe('active');
      expect(session.status).toBe('running');
    },
  );

  it('releases background replay without allowing a late start to revive the finished Run', () => {
    const session = state();
    appendRunEvent(session, event('finished', 'run_started'));
    appendRunEvent(session, event('finished', 'run_completed', 2));
    releaseFinishedRunEvents(session, 'finished');
    appendRunEvent(session, event('finished', 'run_started'));
    expect(session.status).toBe('completed');
    expect(session.currentRun.status).toBe('completed');
    expect(session.hasUnreadCompletion).toBe(true);
    expect(session.latestCompletionRunId).toBe('finished');
  });

  it('clears stale lifecycle state after recovery without discarding the authoritative Timeline', () => {
    const session = state();
    startRun(session, { run_id: 'old', events: [event('old', 'run_started')] });
    const items = [
      assistantRun('old', [outputItem('Durable answer')], {
        status: 'completed',
        source: 'history',
      }),
    ];
    loadHistory(session, [], { items });
    const rendered = session.timelineItems;
    session.streamError = 'Disconnected';
    resetStaleRun(session);
    expect(session.currentRun).toBeNull();
    expect(session.runEvents).toEqual([]);
    expect(session.status).toBe('idle');
    expect(session.streamError).toBe('');
    expect(session.timelineItems).toBe(rendered);
    expect(session.timelineItems).toEqual(items);
  });
});
