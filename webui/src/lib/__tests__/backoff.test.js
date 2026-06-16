import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  DEFAULT_RECONNECT_JITTER_FACTOR,
  reconnectBackoffDelay,
} from '../backoff.js';

describe('reconnectBackoffDelay()', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('doubles the base delay each attempt when jitter sits at its midpoint', () => {
    vi.spyOn(Math, 'random').mockReturnValue(0.5);

    expect(reconnectBackoffDelay(0, { initialDelayMs: 500 })).toBe(500);
    expect(reconnectBackoffDelay(1, { initialDelayMs: 500 })).toBe(1000);
    expect(reconnectBackoffDelay(2, { initialDelayMs: 500 })).toBe(2000);
    expect(reconnectBackoffDelay(3, { initialDelayMs: 500 })).toBe(4000);
  });

  it('clamps the base delay to maxDelayMs before jitter', () => {
    vi.spyOn(Math, 'random').mockReturnValue(0.5);

    // Attempt 10 would be 1000 * 2^10 without the clamp.
    expect(
      reconnectBackoffDelay(10, { initialDelayMs: 1000, maxDelayMs: 30000 }),
    ).toBe(30000);
  });

  it('does not clamp by default, so the curve grows unbounded', () => {
    vi.spyOn(Math, 'random').mockReturnValue(0.5);

    expect(reconnectBackoffDelay(5, { initialDelayMs: 500 })).toBe(16000);
  });

  it('jitters down to (1 - jitterFactor) of the base at random() = 0', () => {
    vi.spyOn(Math, 'random').mockReturnValue(0);

    expect(reconnectBackoffDelay(0, { initialDelayMs: 1000 })).toBe(
      1000 * (1 - DEFAULT_RECONNECT_JITTER_FACTOR),
    );
  });

  it('jitters up to (1 + jitterFactor) of the base at random() = 1', () => {
    vi.spyOn(Math, 'random').mockReturnValue(1);

    expect(reconnectBackoffDelay(0, { initialDelayMs: 1000 })).toBe(
      1000 * (1 + DEFAULT_RECONNECT_JITTER_FACTOR),
    );
  });

  it('honors a custom jitterFactor', () => {
    vi.spyOn(Math, 'random').mockReturnValue(1);

    expect(
      reconnectBackoffDelay(0, { initialDelayMs: 1000, jitterFactor: 0.5 }),
    ).toBe(1500);
  });

  it('keeps every sampled delay within the jitter band around the base', () => {
    const samples = Array.from({ length: 200 }, () =>
      reconnectBackoffDelay(2, { initialDelayMs: 1000, maxDelayMs: 30000 }),
    );

    const base = 4000;
    const spread = base * DEFAULT_RECONNECT_JITTER_FACTOR;
    for (const delay of samples) {
      expect(delay).toBeGreaterThanOrEqual(base - spread);
      expect(delay).toBeLessThanOrEqual(base + spread);
    }
  });
});
