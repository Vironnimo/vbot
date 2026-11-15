// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createContributionSources } from '../contributionSources.js';

const descriptor = {
  extension: 'test',
  id: 'extension:test:row',
  local_id: 'row',
  epoch: 'epoch',
  source: { operation: 'read', resource: 'items' },
};
const deferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
};
async function settle() {
  for (let turn = 0; turn < 16; ++turn) await Promise.resolve();
}
const replies = (requests, result = {}) => ({
  results: requests.map(() => ({ ok: true, result })),
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('Contribution source read owner', () => {
  it.each(['release', 'reconnect', 'timeout'])(
    'releases blocked transport slots after %s and ignores aborted late replies',
    async (mode) => {
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
      let invalidate;
      const calls = [];
      const owner = createContributionSources({
        subscribe: (listener) => {
          invalidate = listener;
        },
        read: (requests, { signal }) => {
          const pending = deferred();
          calls.push({ requests, signal, pending });
          return calls.length <= 2
            ? pending.promise
            : Promise.resolve(replies(requests, { fresh: true }));
        },
      });
      const received = Array.from({ length: 65 }, () => vi.fn());
      const handles = received.map((listener, id) =>
        owner.watch(descriptor, { id }, listener),
      );
      await settle();
      expect(calls).toHaveLength(2);
      if (mode === 'release')
        handles.slice(0, 64).forEach((handle) => handle.release());
      else if (mode === 'reconnect') invalidate({ owner: null });
      else await vi.advanceTimersByTimeAsync(60_000);
      await settle();
      expect(calls[0].signal.aborted).toBe(true);
      expect(calls[1].signal.aborted).toBe(true);
      expect(received[64].mock.lastCall[0]).toEqual({
        result: { fresh: true },
        error: null,
      });
      const before = received.map((listener) => listener.mock.calls.length);
      for (const call of calls.slice(0, 2))
        call.pending.resolve(replies(call.requests, { obsolete: true }));
      await settle();
      expect(received.map((listener) => listener.mock.calls.length)).toEqual(
        before,
      );
      handles.forEach((handle) => handle.release());
      expect(vi.getTimerCount()).toBe(0);
    },
  );

  it('deduplicates equal sources and bounds 300 unique rows to two batches of at most 32', async () => {
    const admitted = [];
    const read = vi.fn((requests) => {
      const pending = deferred();
      admitted.push({ requests, pending });
      return pending.promise;
    });
    const owner = createContributionSources({ read });
    const handles = [];
    const values = [];
    for (let id = 0; id < 300; ++id) {
      handles.push(
        owner.watch(descriptor, { id, nested: { b: 2, a: 1 } }, (value) =>
          values.push(value),
        ),
      );
      handles.push(
        owner.watch(descriptor, { nested: { a: 1, b: 2 }, id }, (value) =>
          values.push(value),
        ),
      );
    }
    await settle();
    expect(admitted.map(({ requests }) => requests.length)).toEqual([32, 32]);
    let completed = 0;
    while (completed < 300) {
      const batch = admitted.shift();
      completed += batch.requests.length;
      batch.pending.resolve(replies(batch.requests));
      await settle();
      expect(admitted.length).toBeLessThanOrEqual(2);
      expect(admitted.every(({ requests }) => requests.length <= 32)).toBe(
        true,
      );
    }
    expect(read).toHaveBeenCalledTimes(10);
    expect(values).toHaveLength(600);
    handles.forEach((handle) => handle.release());
  });

  it('coalesces invalidations, discards superseded reads and retains usable data on individual errors', async () => {
    let invalidate;
    const unsubscribe = vi.fn();
    const calls = [];
    const owner = createContributionSources({
      subscribe: (listener) => {
        invalidate = listener;
        return unsubscribe;
      },
      read: (requests) => {
        const pending = deferred();
        calls.push({ requests, pending });
        return pending.promise;
      },
    });
    const values = [];
    const handle = owner.watch(descriptor, {}, (value) => values.push(value));
    await settle();
    calls[0].pending.resolve(replies(calls[0].requests, { text: 'usable' }));
    await settle();
    invalidate({ owner: 'other', change: { resource: 'items' } });
    invalidate({ owner: 'test', change: { resource: 'unrelated' } });
    await settle();
    expect(calls).toHaveLength(1);
    invalidate({ owner: 'test', change: { resource: 'items' } });
    await settle();
    for (let index = 0; index < 300; ++index) invalidate({ owner: 'test' });
    calls[1].pending.resolve(
      replies(calls[1].requests, { text: 'superseded' }),
    );
    await settle();
    expect(calls).toHaveLength(3);
    expect(values.map((value) => value.result)).toEqual([{ text: 'usable' }]);
    calls[2].pending.resolve({
      results: [
        { ok: false, error: { code: 'failed', message: 'Read unavailable' } },
      ],
    });
    await settle();
    expect(values.at(-1).result).toEqual({ text: 'usable' });
    expect(values.at(-1).error.message).toBe('Read unavailable');
    handle.refresh();
    await settle();
    calls[3].pending.resolve(replies(calls[3].requests, { text: 'recovered' }));
    await settle();
    expect(values.at(-1)).toEqual({
      result: { text: 'recovered' },
      error: null,
    });
    handle.release();
    expect(unsubscribe).toHaveBeenCalledOnce();
  });

  it('drops queued and late released work and recovers surviving consumers after transport failure', async () => {
    const calls = [];
    const owner = createContributionSources({
      read: (requests) => {
        const pending = deferred();
        calls.push({ requests, pending });
        return pending.promise;
      },
    });
    const abandoned = vi.fn();
    const beforeAdmission = owner.watch(
      descriptor,
      { id: 'queued' },
      abandoned,
    );
    beforeAdmission.release();
    const running = owner.watch(descriptor, { id: 'running' }, abandoned);
    const received = vi.fn();
    const retained = owner.watch(
      { ...descriptor, epoch: 'new' },
      { id: 'running' },
      received,
    );
    await settle();
    expect(calls[0].requests).toHaveLength(2);
    running.release();
    calls[0].pending.reject(new Error('Transport unavailable'));
    await settle();
    expect(abandoned).not.toHaveBeenCalled();
    expect(received.mock.lastCall[0].error.message).toBe(
      'Transport unavailable',
    );
    retained.refresh();
    await settle();
    expect(calls[1].requests).toEqual([
      expect.objectContaining({ epoch: 'new' }),
    ]);
    calls[1].pending.resolve(replies(calls[1].requests));
    await settle();
    expect(received.mock.lastCall[0].error).toBeNull();
    retained.release();
  });

  it('pauses hidden documents and performs one current read when visibility returns', async () => {
    vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
    const read = vi.fn(async (requests) => replies(requests));
    const owner = createContributionSources({ read });
    const handle = owner.watch(descriptor, {}, () => {});
    await settle();
    expect(read).not.toHaveBeenCalled();
    handle.refresh();
    handle.refresh();
    vi.spyOn(document, 'hidden', 'get').mockReturnValue(false);
    document.dispatchEvent(new Event('visibilitychange'));
    await settle();
    expect(read).toHaveBeenCalledOnce();
    handle.release();
  });
});
