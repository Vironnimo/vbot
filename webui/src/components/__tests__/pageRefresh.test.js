import { afterEach, expect, it, vi } from 'vitest';
import { createPageRefresh } from '../../../../resources/extensions/swarm/ui/pageRefresh.js';

afterEach(() => {
  vi.useRealTimers();
});

const change = (resource, id) => ({ resource, ids: [id], revision: 1 });

it('coalesces invalidations, spaces passes 1 s apart under sustained changes without delaying a full refresh, and stops when destroyed', async () => {
  vi.useFakeTimers();
  const finishes = [];
  const refresh = vi.fn(() => new Promise((resolve) => finishes.push(resolve)));
  const updates = createPageRefresh(refresh);
  // A quiet page refreshes 100 ms after a burst, once for all of it.
  for (let i = 0; i < 90; i++) updates.schedule(change('swarms', 'swr-a'));
  await vi.advanceTimersByTimeAsync(99);
  expect(refresh).not.toHaveBeenCalled();
  await vi.advanceTimersByTimeAsync(1);
  expect(refresh).toHaveBeenCalledTimes(1);
  expect(refresh).toHaveBeenLastCalledWith(
    new Map([['swarms', new Set(['swr-a'])]]),
  );
  await vi.advanceTimersByTimeAsync(50);
  finishes[0]();
  // Changes that keep coming start the next pass 1 s after the previous one
  // started (t=100), however early it finished, and none of them is lost.
  for (let i = 0; i < 90; i++) {
    updates.schedule(change(i % 2 ? 'profiles' : 'swarms', `id-${i % 3}`));
    await vi.advanceTimersByTimeAsync(10);
  }
  await vi.advanceTimersByTimeAsync(49);
  expect(refresh).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(1);
  expect(refresh).toHaveBeenCalledTimes(2);
  const ids = new Set(['id-0', 'id-1', 'id-2']);
  expect(refresh).toHaveBeenLastCalledWith(
    new Map([
      ['swarms', ids],
      ['profiles', ids],
    ]),
  );
  // A change waiting behind the interval is overtaken by one that needs
  // everything, which refreshes 100 ms after it and carries both.
  updates.schedule(change('swarms', 'swr-b'));
  await vi.advanceTimersByTimeAsync(100);
  finishes[1]();
  await vi.advanceTimersByTimeAsync(100);
  updates.schedule(change('logs', 'swr-b'));
  await vi.advanceTimersByTimeAsync(99);
  expect(refresh).toHaveBeenCalledTimes(2);
  await vi.advanceTimersByTimeAsync(1);
  expect(refresh).toHaveBeenCalledTimes(3);
  expect(refresh).toHaveBeenLastCalledWith(
    new Map([
      ['swarms', new Set(['swr-b'])],
      ['logs', new Set(['swr-b'])],
    ]),
  );
  finishes[2]();
  // After a quiet second, named changes refresh within 100 ms again.
  await vi.advanceTimersByTimeAsync(1000);
  updates.schedule(change('swarms', 'swr-c'));
  await vi.advanceTimersByTimeAsync(99);
  expect(refresh).toHaveBeenCalledTimes(3);
  await vi.advanceTimersByTimeAsync(1);
  expect(refresh).toHaveBeenCalledTimes(4);
  expect(refresh).toHaveBeenLastCalledWith(
    new Map([['swarms', new Set(['swr-c'])]]),
  );
  finishes[3]();
  updates.schedule();
  updates.destroy();
  await vi.advanceTimersByTimeAsync(2000);
  expect(refresh).toHaveBeenCalledTimes(4);
});
