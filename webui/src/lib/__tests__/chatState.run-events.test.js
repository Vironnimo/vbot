import { describe, expect, it } from 'vitest';
import {
  appendRunEvent,
  applyRunControls,
  createChatState,
  ensureSessionState,
  startRun,
} from '../chatState.js';

describe('Session client state', () => {
  it.each([
    'assistant_output_delta',
    'reasoning_delta',
    'tool_call_delta',
    'tool_call_output',
  ])(
    'keeps high-volume %s out of retained lifecycle state and leaves server rows unchanged',
    (type) => {
      const session = ensureSessionState(createChatState(), 'alpha', 'session');
      const rows = [
        { id: 'server-row', type: 'assistant_run', runId: 'one', items: [] },
      ];
      session.timelineItems = rows;
      appendRunEvent(session, {
        run_id: 'one',
        sequence: 1,
        type,
        payload: { content_delta: 'Ignored raw delta' },
      });
      expect(session.runEvents).toEqual([]);
      expect(session.timelineItems).toBe(rows);
    },
  );
  it('appends each stable Run event once against the currently retained events', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    const event = (runId, sequence, type = 'tool_call_started') => ({
      type,
      run_id: runId,
      sequence,
      payload: {},
    });

    appendRunEvent(sessionState, event('run-a', 1, 'run_started'));
    appendRunEvent(sessionState, event('run-a', 2));
    appendRunEvent(sessionState, event('run-a', 2));
    appendRunEvent(sessionState, event('run-b', 2));
    expect(
      sessionState.runEvents.map(({ run_id, sequence }) => [run_id, sequence]),
    ).toEqual([
      ['run-a', 1],
      ['run-a', 2],
      ['run-b', 2],
    ]);

    // Another owner (History reconciliation, edits) replaces the retained
    // events; deduplication follows the replacement, not the old contents.
    sessionState.runEvents = sessionState.runEvents.filter(
      (runEvent) => runEvent.run_id === 'run-b',
    );
    appendRunEvent(sessionState, event('run-a', 2));
    appendRunEvent(sessionState, event('run-b', 2));
    expect(
      sessionState.runEvents.map(({ run_id, sequence }) => [run_id, sequence]),
    ).toEqual([
      ['run-b', 2],
      ['run-a', 2],
    ]);
  });

  it('marks a session running when a run_started event arrives from server push', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    appendRunEvent(sessionState, {
      type: 'run_started',
      run_id: 'run-from-ws',
      sequence: 1,
      timestamp: '2026-08-05T18:04:00.000Z',
      payload: {},
    });

    expect(sessionState.status).toBe('running');
    expect(sessionState.streamStatus).toBe('running');
    expect(sessionState.currentRun).toMatchObject({
      runId: 'run-from-ws',
      status: 'running',
      startedAt: '2026-08-05T18:04:00.000Z',
    });
  });

  it('keeps live Run controls authoritative across stale snapshots and other Runs', () => {
    const session = ensureSessionState(
      createChatState(),
      'agent@project',
      'session',
    );
    startRun(session, { run_id: 'one', status: 'running' });
    appendRunEvent(session, {
      run_id: 'one',
      sequence: 5,
      type: 'run_controls_changed',
      payload: { compaction: 'pending' },
    });
    applyRunControls(session, {
      run_id: 'one',
      controls_sequence: 4,
      controls: { compaction: 'idle' },
    });
    applyRunControls(session, {
      run_id: 'other',
      controls_sequence: 6,
      controls: { compaction: 'idle' },
    });
    expect(session.currentRun.controls.compaction).toBe('pending');
    applyRunControls(session, {
      run_id: 'one',
      controls_sequence: 6,
      controls: { compaction: 'running' },
    });
    expect(session.currentRun.controls.compaction).toBe('running');
  });
});
