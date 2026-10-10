import { describe, expect, it } from 'vitest';

import { createRunEventFeed } from '../runEventFeed.svelte.js';

const lifecycle = (type, runId) => ({
  type,
  payload: {
    run_id: runId,
    agent_id: 'beta',
    project_id: null,
    session_id: `session-${runId}`,
    run_kind: 'chat',
    run_event_type: type,
    run_event_sequence: 1,
    run_event_timestamp: '2026-09-22T10:00:00+00:00',
  },
});

describe('Run event feed', () => {
  it('reads the events after a position and reports those it no longer holds', () => {
    const feed = createRunEventFeed({ capacity: 3 });
    const events = [
      lifecycle('run_started', 'one'),
      lifecycle('run_output', 'one'),
      lifecycle('run_completed', 'one'),
      lifecycle('run_started', 'two'),
      lifecycle('run_output', 'two'),
    ];
    feed.append(events[0]);
    feed.append(events[1]);

    expect(feed.read(0)).toEqual({
      events: events.slice(0, 2),
      from: 1,
      end: 2,
      missed: false,
    });
    expect(feed.read(2).events).toEqual([]);

    events.slice(2).forEach((event) => feed.append(event));

    // Positions 1 and 2 left the feed: a reader at 1 never saw position 2.
    expect(feed.read(1)).toEqual({
      events: events.slice(2),
      from: 3,
      end: 5,
      missed: true,
    });
    expect(feed.read(2).missed).toBe(false);
    expect(feed.read(4).events[0]).toBe(events[4]);
    expect([feed.end, feed.runStarts]).toEqual([5, 2]);
  });

  it('knows which terminal events it still holds', () => {
    const feed = createRunEventFeed({ capacity: 2 });

    feed.append(lifecycle('run_completed', 'done'));
    expect(feed.retainsTerminal('done')).toBe(true);
    expect(feed.retainsTerminal('other')).toBe(false);

    feed.append(lifecycle('run_output', 'next'));
    feed.append(lifecycle('run_failed', 'next'));
    expect(feed.retainsTerminal('done')).toBe(false);
    expect(feed.retainsTerminal('next')).toBe(true);
  });

  it('keeps the active Run list current beyond the events it holds', () => {
    const feed = createRunEventFeed({ capacity: 3 });
    feed.resetActiveRuns([{ run_id: 'listed', agent_id: 'beta' }]);

    feed.append(lifecycle('run_started', 'new'));
    feed.append(lifecycle('run_completed', 'listed'));
    // The listed Run's terminal event leaves the feed...
    for (let index = 0; index < 3; index += 1) {
      feed.append(lifecycle('run_output', 'new'));
    }

    // ...but the active list still reflects it; the new Run is running.
    expect(feed.retainsTerminal('listed')).toBe(false);
    expect(feed.activeRuns).toEqual([
      {
        run_id: 'new',
        agent_id: 'beta',
        project_id: null,
        session_id: 'session-new',
        run_kind: 'chat',
        status: 'running',
        started_at: '2026-09-22T10:00:00+00:00',
      },
    ]);
  });
});
