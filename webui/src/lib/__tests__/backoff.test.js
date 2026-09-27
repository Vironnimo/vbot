import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  DEFAULT_RECONNECT_JITTER_FACTOR,
  reconnectBackoffDelay,
} from '../backoff.js';

describe('reconnectBackoffDelay()', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it.each([
    ['attempt 0 starts at the initial delay', 0, { initialDelayMs: 500 }, 500],
    ['each attempt doubles the delay', 3, { initialDelayMs: 500 }, 4000],
    ['the default is unclamped', 5, { initialDelayMs: 500 }, 16000],
    [
      'maxDelayMs clamps the base delay',
      10,
      { initialDelayMs: 1000, maxDelayMs: 30000 },
      30000,
    ],
  ])('with centered jitter, %s', (_label, attempt, options, delay) => {
    vi.spyOn(Math, 'random').mockReturnValue(0.5);

    expect(reconnectBackoffDelay(attempt, options)).toBe(delay);
  });

  // The jitter is linear in Math.random(), so its two ends bound every delay.
  it.each([
    ['down to the lower bound', 0, {}, 1 - DEFAULT_RECONNECT_JITTER_FACTOR],
    ['up to the upper bound', 1, {}, 1 + DEFAULT_RECONNECT_JITTER_FACTOR],
    ['by a custom jitterFactor', 1, { jitterFactor: 0.5 }, 1.5],
  ])('jitters %s of the base delay', (_label, random, options, factor) => {
    vi.spyOn(Math, 'random').mockReturnValue(random);

    expect(reconnectBackoffDelay(0, { initialDelayMs: 1000, ...options })).toBe(
      1000 * factor,
    );
  });
});
