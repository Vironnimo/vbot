import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createExtensionRunStream } from '../extensionRunStream.js';

beforeEach(() => {
  vi.useFakeTimers();
  vi.spyOn(Math, 'random').mockReturnValue(0.5);
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function setup() {
  const opened = { stream: { url: '/api/extension-runs/initial' } };
  const openRun = vi
    .fn()
    .mockResolvedValue({ stream: { url: '/api/extension-runs/fresh' } });
  const streams = [];
  const subscribeRunEvents = vi.fn((url, handlers, options) => {
    const stream = { url, handlers, options, close: vi.fn() };
    streams.push(stream);
    return stream;
  });
  const onEvent = vi.fn();
  const onResync = vi.fn();
  const subscription = createExtensionRunStream({
    opened,
    afterSequence: 4,
    openRun,
    subscribeRunEvents,
    onEvent,
    onResync,
  });
  return { subscription, streams, openRun, onEvent, onResync };
}

describe('Extension Run stream recovery', () => {
  it('leaves no watchdog behind when the initial transport cannot be constructed', () => {
    expect(() =>
      createExtensionRunStream({
        opened: { stream: { url: '/api/extension-runs/one' } },
        subscribeRunEvents: () => {
          throw new Error('EventSource unavailable');
        },
      }),
    ).toThrow('EventSource unavailable');
    expect(vi.getTimerCount()).toBe(0);
  });

  it('refreshes the capability at the last delivered sequence and ignores retired callbacks', async () => {
    const { subscription, streams, openRun, onEvent, onResync } = setup();
    streams[0].handlers.onEvent({
      type: 'assistant_output_delta',
      data: { sequence: 5 },
    });
    streams[0].handlers.onError();
    expect(streams[0].close).toHaveBeenCalledOnce();
    streams[0].handlers.onEvent({
      type: 'run_completed',
      data: { sequence: 6 },
    });
    await vi.advanceTimersByTimeAsync(500);
    expect(openRun).toHaveBeenCalledWith(5);
    expect(streams[1].options.afterSequence).toBe(5);
    expect(onResync).toHaveBeenCalledOnce();
    streams[1].handlers.onEvent({
      type: 'assistant_output_delta',
      data: { sequence: 5 },
    });
    streams[1].handlers.onEvent({
      type: 'run_completed',
      data: { sequence: 6 },
    });
    expect(onEvent).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(openRun).toHaveBeenCalledOnce();
    subscription.close();
  });

  it('uses heartbeats for liveness and recovers a silently stalled stream', async () => {
    const { subscription, streams, openRun } = setup();
    await vi.advanceTimersByTimeAsync(20_000);
    streams[0].handlers.onHeartbeat();
    await vi.advanceTimersByTimeAsync(20_000);
    expect(openRun).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(5_500);
    expect(openRun).toHaveBeenCalledWith(4);
    subscription.close();
  });

  it('refreshes History when the Run no longer has a replay stream', async () => {
    const { streams, openRun, onResync } = setup();
    openRun.mockResolvedValue({ stream: null });
    streams[0].handlers.onError();
    await vi.advanceTimersByTimeAsync(500);
    expect(onResync).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(openRun).toHaveBeenCalledOnce();
  });

  it('retries failed refreshes with backoff and stops pending work on disposal', async () => {
    const { subscription, streams, openRun } = setup();
    openRun.mockRejectedValueOnce(new Error('offline'));
    streams[0].handlers.onError();
    await vi.advanceTimersByTimeAsync(500);
    expect(openRun).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1000);
    expect(openRun).toHaveBeenCalledTimes(2);
    subscription.close();
    streams[1].handlers.onError();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(openRun).toHaveBeenCalledTimes(2);
  });

  it('ignores a capability response that arrives after teardown', async () => {
    const { subscription, streams, openRun, onResync } = setup();
    let resolve;
    openRun.mockReturnValue(
      new Promise((finish) => {
        resolve = finish;
      }),
    );
    streams[0].handlers.onError();
    await vi.advanceTimersByTimeAsync(500);
    subscription.close();
    resolve({ stream: { url: '/api/extension-runs/late' } });
    await Promise.resolve();
    expect(streams).toHaveLength(1);
    expect(onResync).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });
});
