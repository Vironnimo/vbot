import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { loadHistory } from '../chatState.js';
import {
  activeRun,
  makeStreamHarness,
  timelineSnapshot,
  timelinePatch,
} from './chatRunStream.support.js';

const owners = [];
function setup(options) {
  const harness = makeStreamHarness(options);
  owners.push(harness.stream);
  harness.stream.applyConnectionSnapshot({
    active_runs: [activeRun('run-one')],
  });
  return harness;
}
beforeEach(() => {
  vi.useFakeTimers();
  vi.spyOn(Math, 'random').mockReturnValue(0.5);
});
afterEach(() => {
  for (const owner of owners.splice(0)) owner.closeSubscriptions();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('timeline stream recovery', () => {
  it('reconnects from an authoritative snapshot, never replaying suffixes over the old base', async () => {
    const h = setup();
    h.sse(timelineSnapshot('run-one'), timelinePatch('run-one', 10));
    const first = h.subscriptions[0];
    first.handlers.onError(new Error('offline'));
    expect(h.displayedSession().streamError).not.toBe('');
    expect(h.reconcileRunSession).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(500);
    expect(h.subscriptions).toHaveLength(2);
    h.sse(timelineSnapshot('run-one', 100, { content: 'Complete snapshot' }));
    h.sse(timelinePatch('run-one', 100));
    expect(h.displayedSession().timelineItems[0].outputs[0].content).toBe(
      'Complete snapshot!',
    );
    expect(h.displayedSession().streamError).toBe('');
    first.handlers.onEvent({
      type: 'timeline_update',
      data: timelinePatch('run-one', 11),
    });
    expect(h.displayedSession().timelineItems[0].outputs[0].content).toBe(
      'Complete snapshot!',
    );
  });

  it('rejects a patch whose base was superseded by History and reconnects without duplicated text', async () => {
    const h = setup();
    h.sse(timelineSnapshot('run-one', 10));
    loadHistory(h.displayedSession(), [], {
      items: timelineSnapshot('run-one', 12, { content: 'History text' }).items,
      timelineSequence: 12,
      activeRunId: 'run-one',
    });
    const patch = { ...timelinePatch('run-one', 10), sequence: 13 };
    h.sse(patch);
    expect(h.displayedSession().timelineItems[0].outputs[0].content).toBe(
      'History text',
    );
    expect(h.subscriptions[0].close).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(500);
    h.sse(timelineSnapshot('run-one', 15, { content: 'Recovered' }));
    expect(h.displayedSession().timelineItems[0].outputs[0].content).toBe(
      'Recovered',
    );
  });

  it('requires a snapshot before applying patches on every connection', async () => {
    const h = setup();
    h.sse(timelinePatch('run-one', 10));
    expect(h.displayedSession().timelineItems).toEqual([]);
    expect(h.subscriptions[0].close).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(500);
    expect(h.subscriptions).toHaveLength(2);
  });

  it('uses heartbeats to detect a stalled connection and cleans up all timers at disposal', async () => {
    const h = setup();
    h.sse(timelineSnapshot('run-one'));
    await vi.advanceTimersByTimeAsync(24_000);
    h.subscriptions[0].handlers.onHeartbeat();
    await vi.advanceTimersByTimeAsync(24_000);
    expect(h.subscriptions[0].close).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(h.subscriptions[0].close).toHaveBeenCalledOnce();
    h.stream.closeSubscriptions();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(h.subscriptions).toHaveLength(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('backs off repeated failures and resets the delay once a snapshot arrives', async () => {
    const h = setup();
    h.subscriptions.at(-1).handlers.onError();
    await vi.advanceTimersByTimeAsync(499);
    expect(h.subscriptions).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    h.subscriptions.at(-1).handlers.onError();
    await vi.advanceTimersByTimeAsync(999);
    expect(h.subscriptions).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(1);
    h.sse(timelineSnapshot('run-one', 99));
    h.subscriptions.at(-1).handlers.onError();
    await vi.advanceTimersByTimeAsync(500);
    expect(h.subscriptions).toHaveLength(4);
  });
});
