import { describe, expect, it } from 'vitest';

import {
  createBoundedKeySet,
  mergeBoundedEntries,
  subAgentGuardKeysForEvictedStatuses,
} from '../clientCaches.js';

describe('createBoundedKeySet', () => {
  it('remembers keys below the cap', () => {
    const keySet = createBoundedKeySet(3);

    keySet.add('a');
    keySet.add('b');

    expect(keySet.has('a')).toBe(true);
    expect(keySet.has('b')).toBe(true);
    expect(keySet.has('c')).toBe(false);
  });

  it('evicts the oldest key once the cap is exceeded', () => {
    const keySet = createBoundedKeySet(2);

    keySet.add('first');
    keySet.add('second');
    keySet.add('third');

    expect(keySet.has('first')).toBe(false);
    expect(keySet.has('second')).toBe(true);
    expect(keySet.has('third')).toBe(true);
  });

  it('ignores re-adds of known keys without evicting anything', () => {
    const keySet = createBoundedKeySet(2);

    keySet.add('first');
    keySet.add('second');
    keySet.add('first');
    keySet.add('first');

    expect(keySet.has('first')).toBe(true);
    expect(keySet.has('second')).toBe(true);
  });
});

describe('mergeBoundedEntries', () => {
  it('merges updates without eviction while under the cap', () => {
    const { entries, evictedKeys } = mergeBoundedEntries({ a: 1 }, { b: 2 }, 5);

    expect(entries).toEqual({ a: 1, b: 2 });
    expect(evictedKeys).toEqual([]);
  });

  it('evicts the least-recently-written entries beyond the cap and reports them', () => {
    const { entries, evictedKeys } = mergeBoundedEntries(
      { a: 1, b: 2, c: 3 },
      { d: 4, e: 5 },
      3,
    );

    expect(entries).toEqual({ c: 3, d: 4, e: 5 });
    expect(evictedKeys).toEqual(['a', 'b']);
  });

  it('treats an updated key as fresh so it survives eviction', () => {
    const { entries, evictedKeys } = mergeBoundedEntries(
      { a: 1, b: 2, c: 3 },
      { a: 'updated' },
      2,
    );

    expect(entries).toEqual({ c: 3, a: 'updated' });
    expect(evictedKeys).toEqual(['b']);
  });

  it('does not mutate the previous entries object', () => {
    const previousEntries = { a: 1 };

    const { entries } = mergeBoundedEntries(previousEntries, { b: 2 }, 5);

    expect(previousEntries).toEqual({ a: 1 });
    expect(entries).not.toBe(previousEntries);
  });
});

describe('subAgentGuardKeysForEvictedStatuses', () => {
  it('maps run- and session-scoped status keys to their verification-guard keys', () => {
    const guardKeys = subAgentGuardKeysForEvictedStatuses([
      'run:run-1',
      'session:agent-a::session-b',
    ]);

    expect(guardKeys).toEqual(['run-1', 'agent-a::session-b']);
  });

  it('ignores duration, queue-mapping, and non-string keys', () => {
    const guardKeys = subAgentGuardKeysForEvictedStatuses([
      'runDuration:run-1',
      'sessionDuration:agent-a::session-b',
      'queueRun:item-1',
      42,
    ]);

    expect(guardKeys).toEqual([]);
  });
});
