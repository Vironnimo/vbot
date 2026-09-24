import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  MAX_SESSION_INVALIDATIONS,
  appendSessionInvalidation,
  createCoalescedRefresh,
  sessionInvalidationListsTarget,
  sessionInvalidationTarget,
  takeSessionInvalidations,
} from '../sessionInvalidation.js';

function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe('sessionInvalidationTarget', () => {
  it('names the exact Session, terminal Run and read acknowledgement', () => {
    expect(
      sessionInvalidationTarget({
        project_id: 'vbot',
        agent_id: 'builder',
        session_id: 'one',
        run_id: 'run-1',
      }),
    ).toEqual({
      agentAddress: 'builder@vbot',
      sessionId: 'one',
      runId: 'run-1',
      readRunId: '',
    });
    expect(
      sessionInvalidationTarget({
        project_id: null,
        agent_id: 'coder',
        session_id: 'two',
        read_run_id: 'run-2',
      }),
    ).toEqual({
      agentAddress: 'coder',
      sessionId: 'two',
      runId: '',
      readRunId: 'run-2',
    });
  });

  it('recognizes deletions and Identity Agent renames', () => {
    expect(
      sessionInvalidationTarget({
        agent_id: 'coder',
        deleted_session_id: 'gone',
        next_session_id: 'next',
      }),
    ).toEqual({ agentAddress: 'coder', sessionId: 'gone', deleted: true });
    expect(
      sessionInvalidationTarget({ old_agent_id: 'old', new_agent_id: 'new' }),
    ).toEqual({ renamedAgent: { oldAgentId: 'old', newAgentId: 'new' } });
  });

  it('treats a missing, Agent-wide or malformed scope as everything', () => {
    expect(sessionInvalidationTarget(null)).toEqual({ all: true });
    expect(sessionInvalidationTarget(['coder'])).toEqual({ all: true });
    expect(sessionInvalidationTarget({ agent_id: 'coder' })).toEqual({
      all: true,
    });
    expect(sessionInvalidationTarget({ session_id: 'one' })).toEqual({
      all: true,
    });
  });
});

describe('Session invalidation window', () => {
  it('keeps the newest bounded window of scopes', () => {
    let entries = [];
    for (let id = 1; id <= MAX_SESSION_INVALIDATIONS + 2; id += 1) {
      entries = appendSessionInvalidation(entries, id, { agent_id: 'a' });
    }
    entries = appendSessionInvalidation(entries, 999, 'not a scope');

    expect(entries).toHaveLength(MAX_SESSION_INVALIDATIONS);
    expect(entries[0].id).toBe(4);
    expect(entries.at(-1)).toEqual({ id: 999, scope: null });
  });

  it('adopts the window for a new owner and returns only unseen entries later', () => {
    const entries = [
      { id: 1, scope: { agent_id: 'a', session_id: 'one' } },
      { id: 2, scope: { agent_id: 'b', session_id: 'two' } },
    ];

    expect(takeSessionInvalidations(entries, null)).toEqual({
      targets: [],
      lastId: 2,
      overflowed: false,
    });
    const next = [...entries, { id: 3, scope: null }];
    expect(takeSessionInvalidations(next, 2)).toEqual({
      targets: [{ all: true }],
      lastId: 3,
      overflowed: false,
    });
    expect(takeSessionInvalidations(next, 3)).toEqual({
      targets: [],
      lastId: 3,
      overflowed: false,
    });
  });

  it('reports entries the bounded window dropped before an owner saw them', () => {
    const entries = [
      { id: 5, scope: { agent_id: 'a', session_id: 'one' } },
      { id: 6, scope: { agent_id: 'a', session_id: 'two' } },
    ];

    expect(takeSessionInvalidations(entries, 3).overflowed).toBe(true);
    expect(takeSessionInvalidations(entries, 4).overflowed).toBe(false);
  });

  it('matches targets against the listed Agent addresses', () => {
    const listed = ['coder', 'builder@vbot'];

    expect(sessionInvalidationListsTarget({ all: true }, listed)).toBe(true);
    expect(
      sessionInvalidationListsTarget(
        { agentAddress: 'builder@vbot', sessionId: 'one' },
        listed,
      ),
    ).toBe(true);
    expect(
      sessionInvalidationListsTarget(
        { agentAddress: 'builder', sessionId: 'one' },
        listed,
      ),
    ).toBe(false);
    expect(
      sessionInvalidationListsTarget(
        { renamedAgent: { oldAgentId: 'coder', newAgentId: 'writer' } },
        listed,
      ),
    ).toBe(true);
  });
});

describe('createCoalescedRefresh', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('runs at most one refresh and folds requests made meanwhile into one pass', async () => {
    const first = deferred();
    const refresh = vi
      .fn()
      .mockReturnValueOnce(first.promise)
      .mockResolvedValue(true);
    const coalesced = createCoalescedRefresh(refresh, { delayMs: 0 });

    const running = coalesced.run(['a']);
    expect(coalesced.run(['b'])).toBe(running);
    coalesced.schedule(['c', 'b']);
    await Promise.resolve();
    expect(refresh.mock.calls).toEqual([[['a']]]);

    first.resolve(true);
    await running;
    await vi.waitFor(() => expect(refresh).toHaveBeenCalledTimes(2));
    expect(refresh.mock.calls[1]).toEqual([['b', 'c']]);
  });

  it('delays scheduled keys so a burst becomes one refresh', async () => {
    vi.useFakeTimers();
    const refresh = vi.fn().mockResolvedValue(true);
    const coalesced = createCoalescedRefresh(refresh, { delayMs: 100 });

    coalesced.schedule(['a']);
    coalesced.schedule(['b', 'a']);
    await vi.advanceTimersByTimeAsync(99);
    expect(refresh).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);

    expect(refresh.mock.calls).toEqual([[['a', 'b']]]);
  });

  it('keeps coalescing after a scheduled refresh fails', async () => {
    vi.useFakeTimers();
    const refresh = vi
      .fn()
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValue(true);
    const coalesced = createCoalescedRefresh(refresh, { delayMs: 10 });

    coalesced.schedule(['a']);
    await vi.advanceTimersByTimeAsync(10);
    coalesced.schedule(['b']);
    await vi.advanceTimersByTimeAsync(10);

    expect(refresh.mock.calls).toEqual([[['a']], [['b']]]);
  });

  it('drops pending work once destroyed', async () => {
    vi.useFakeTimers();
    const refresh = vi.fn().mockResolvedValue(true);
    const coalesced = createCoalescedRefresh(refresh, { delayMs: 10 });

    coalesced.schedule(['a']);
    coalesced.destroy();
    await vi.advanceTimersByTimeAsync(50);

    expect(refresh).not.toHaveBeenCalled();
    await expect(coalesced.run(['b'])).resolves.toBe(false);
    expect(refresh).not.toHaveBeenCalled();
  });
});
