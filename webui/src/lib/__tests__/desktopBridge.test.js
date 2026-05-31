import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';

import {
  isDesktop,
  getDesktopCapabilities,
  getWakewordStatus,
  setWakewordEnabled,
  setWakewordConfig,
  onWakewordStatusChange,
} from '../desktopBridge.js';

describe('isDesktop', () => {
  let originalLocation;
  let originalPywebview;

  beforeEach(() => {
    originalLocation = globalThis.window?.location;
    originalPywebview = globalThis.window?.pywebview;
    globalThis.window = {
      location: { search: '' },
      pywebview: undefined,
    };
  });

  afterEach(() => {
    if (originalLocation !== undefined) {
      globalThis.window.location = originalLocation;
    }
    globalThis.window.pywebview = originalPywebview;
  });

  it('returns false without accessor param or bridge', () => {
    expect(isDesktop()).toBe(false);
  });

  it('returns false with only accessor param', () => {
    globalThis.window.location.search = '?accessor=desktop';
    expect(isDesktop()).toBe(false);
  });

  it('returns false with only bridge api', () => {
    globalThis.window.pywebview = { api: {} };
    expect(isDesktop()).toBe(false);
  });

  it('returns true with both accessor param and bridge api', () => {
    globalThis.window.location.search = '?accessor=desktop';
    globalThis.window.pywebview = { api: {} };
    expect(isDesktop()).toBe(true);
  });

  it('returns false when window is undefined', () => {
    const savedWindow = globalThis.window;
    globalThis.window = undefined;
    expect(isDesktop()).toBe(false);
    globalThis.window = savedWindow;
  });
});

describe('getDesktopCapabilities', () => {
  it('returns cached capabilities on second call', async () => {
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: {
        api: {
          getDesktopCapabilities: () => ({ wakeword: true }),
        },
      },
    };

    const caps1 = await getDesktopCapabilities();
    expect(caps1).toEqual({ wakeword: true });

    // Second call should return cached result
    const caps2 = await getDesktopCapabilities();
    expect(caps2).toEqual({ wakeword: true });
  });

  it('returns disabled when bridge absent', async () => {
    globalThis.window = { location: { search: '' }, pywebview: undefined };

    const caps = await getDesktopCapabilities();
    expect(caps).toEqual({ wakeword: false });
  });
});

describe('getWakewordStatus', () => {
  it('returns status from bridge', async () => {
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: {
        api: {
          getWakewordStatus: () => ({
            enabled: true,
            state: 'listening',
            engine: 'openwakeword',
          }),
        },
      },
    };

    const status = await getWakewordStatus();
    expect(status.enabled).toBe(true);
    expect(status.state).toBe('listening');
  });

  it('returns disabled fallback when bridge absent', async () => {
    globalThis.window = { location: { search: '' }, pywebview: undefined };

    const status = await getWakewordStatus();
    expect(status.enabled).toBe(false);
    expect(status.state).toBe('off');
  });
});

describe('setWakewordEnabled', () => {
  it('calls bridge method', async () => {
    const enabledCalls = [];
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: {
        api: {
          setWakewordEnabled: (val) => {
            enabledCalls.push(val);
          },
        },
      },
    };

    await setWakewordEnabled(true);
    expect(enabledCalls).toEqual([true]);

    await setWakewordEnabled(false);
    expect(enabledCalls).toEqual([true, false]);
  });
});

describe('setWakewordConfig', () => {
  it('calls bridge with config object', async () => {
    const calls = [];
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: {
        api: {
          setWakewordConfig: (config) => {
            calls.push(config);
          },
        },
      },
    };

    await setWakewordConfig({ sensitivity: 0.8 });
    expect(calls).toEqual([{ sensitivity: 0.8 }]);
  });
});

describe('onWakewordStatusChange', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('polls and calls callback on state change', async () => {
    const callbacks = [];
    let pollCount = 0;
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: {
        api: {
          getWakewordStatus: () => {
            pollCount += 1;
            if (pollCount === 1) return { state: 'off', enabled: true };
            return { state: 'listening', enabled: true };
          },
        },
      },
    };

    const cleanup = onWakewordStatusChange((status) => {
      callbacks.push(status);
    }, 100);

    // First poll fires immediately (async)
    await vi.advanceTimersByTimeAsync(0);
    expect(callbacks).toHaveLength(1);
    expect(callbacks[0].state).toBe('off');

    // Second poll on interval
    await vi.advanceTimersByTimeAsync(100);
    expect(callbacks).toHaveLength(2);
    expect(callbacks[1].state).toBe('listening');

    cleanup();
  });

  it('does not call callback when state unchanged', async () => {
    const callbacks = [];
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: {
        api: {
          getWakewordStatus: () => ({ state: 'listening', enabled: true }),
        },
      },
    };

    const cleanup = onWakewordStatusChange((status) => {
      callbacks.push(status);
    }, 100);

    await vi.advanceTimersByTimeAsync(0);
    expect(callbacks).toHaveLength(1);

    await vi.advanceTimersByTimeAsync(200);
    expect(callbacks).toHaveLength(1); // Still only the initial poll

    cleanup();
  });

  it('returns noop cleanup when not on desktop', () => {
    globalThis.window = { location: { search: '' }, pywebview: undefined };

    const cleanup = onWakewordStatusChange(() => {});
    expect(typeof cleanup).toBe('function');
    expect(cleanup()).toBeUndefined();
  });
});
