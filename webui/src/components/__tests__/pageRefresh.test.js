import { afterEach, expect, it, vi } from 'vitest';
import { createPageRefresh } from '../../../../resources/extensions/swarm/ui/pageRefresh.js';

afterEach(() => {
  vi.useRealTimers();
});

it('bounds overlapping invalidations, still refreshes during continuous activity and stops when destroyed', async () => {
  vi.useFakeTimers();
  const finishes = [];
  const refresh = vi.fn(() => new Promise((resolve) => finishes.push(resolve)));
  const updates = createPageRefresh(refresh);
  for (let i = 0; i < 90; i++) updates.schedule();
  await vi.advanceTimersByTimeAsync(100);
  expect(refresh).toHaveBeenCalledTimes(1);
  for (let i = 0; i < 90; i++) {
    updates.schedule();
    await vi.advanceTimersByTimeAsync(10);
  }
  expect(refresh).toHaveBeenCalledTimes(1);
  finishes[0]();
  await vi.advanceTimersByTimeAsync(100);
  expect(refresh).toHaveBeenCalledTimes(2);
  updates.schedule();
  updates.destroy();
  finishes[1]();
  await vi.advanceTimersByTimeAsync(1000);
  expect(refresh).toHaveBeenCalledTimes(2);
});
