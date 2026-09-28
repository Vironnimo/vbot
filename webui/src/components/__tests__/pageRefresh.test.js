import { afterEach, expect, it, vi } from 'vitest';
import { createPageRefresh } from '../../../../resources/extensions/swarm/ui/pageRefresh.js';

afterEach(() => {
  vi.useRealTimers();
});

const change = (resource, id) => ({ resource, ids: [id], revision: 1 });

it('bounds overlapping invalidations, merges their changes, still refreshes during continuous activity and stops when destroyed', async () => {
  vi.useFakeTimers();
  const finishes = [];
  const refresh = vi.fn(() => new Promise((resolve) => finishes.push(resolve)));
  const updates = createPageRefresh(refresh);
  for (let i = 0; i < 90; i++) updates.schedule(change('swarms', 'swr-a'));
  await vi.advanceTimersByTimeAsync(100);
  expect(refresh).toHaveBeenCalledTimes(1);
  expect(refresh).toHaveBeenLastCalledWith(
    new Map([['swarms', new Set(['swr-a'])]]),
  );
  for (let i = 0; i < 90; i++) {
    updates.schedule(change(i % 2 ? 'profiles' : 'swarms', `id-${i % 3}`));
    await vi.advanceTimersByTimeAsync(10);
  }
  expect(refresh).toHaveBeenCalledTimes(1);
  finishes[0]();
  await vi.advanceTimersByTimeAsync(100);
  expect(refresh).toHaveBeenCalledTimes(2);
  // The pending pass covers every change scheduled while the first one ran.
  const ids = new Set(['id-0', 'id-1', 'id-2']);
  expect(refresh).toHaveBeenLastCalledWith(
    new Map([
      ['swarms', ids],
      ['profiles', ids],
    ]),
  );
  // An invalidation without a change widens the pending pass to everything.
  updates.schedule(change('swarms', 'swr-b'));
  updates.schedule();
  finishes[1]();
  await vi.advanceTimersByTimeAsync(100);
  expect(refresh).toHaveBeenCalledTimes(3);
  expect(refresh).toHaveBeenLastCalledWith(null);
  updates.schedule();
  updates.destroy();
  finishes[2]();
  await vi.advanceTimersByTimeAsync(1000);
  expect(refresh).toHaveBeenCalledTimes(3);
});
