import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';

import {
  isDesktop,
  isDesktopAccessor,
  getDesktopCapabilities,
  getWakewordStatus,
  setWakewordEnabled,
  setWakewordConfig,
  onWakewordStatusChange,
  waitForDesktopBridge,
} from '../desktopBridge.js';

describe('desktop detection', () => {
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
    expect(isDesktopAccessor()).toBe(false);
    expect(isDesktop()).toBe(false);
  });

  it('detects the accessor param before the bridge is ready', () => {
    globalThis.window.location.search = '?accessor=desktop';
    expect(isDesktopAccessor()).toBe(true);
    expect(isDesktop()).toBe(false);
  });

  it('returns false with only bridge api', () => {
    globalThis.window.pywebview = { api: {} };
    expect(isDesktop()).toBe(false);
  });

  it('returns true with both accessor param and bridge api', () => {
    globalThis.window.location.search = '?accessor=desktop';
    globalThis.window.pywebview = { api: {} };
    expect(isDesktopAccessor()).toBe(true);
    expect(isDesktop()).toBe(true);
  });

  it('returns false when window is undefined', () => {
    const savedWindow = globalThis.window;
    globalThis.window = undefined;
    expect(isDesktopAccessor()).toBe(false);
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

  it('does not reuse cached capabilities for a different bridge api object', async () => {
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: {
        api: {
          getDesktopCapabilities: () => ({ wakeword: true }),
        },
      },
    };

    expect(await getDesktopCapabilities()).toEqual({ wakeword: true });

    globalThis.window.pywebview = {
      api: {
        getDesktopCapabilities: () => ({ wakeword: false }),
      },
    };

    expect(await getDesktopCapabilities()).toEqual({ wakeword: false });
  });
});

describe('waitForDesktopBridge', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('resolves false outside the desktop accessor URL', async () => {
    globalThis.window = { location: { search: '' }, pywebview: undefined };

    await expect(waitForDesktopBridge()).resolves.toBe(false);
  });

  it('resolves true immediately when the bridge already exists', async () => {
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: { api: {} },
    };

    await expect(waitForDesktopBridge()).resolves.toBe(true);
  });

  it('waits for pywebviewready before resolving in desktop mode', async () => {
    const listeners = new Map();
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: undefined,
      addEventListener: (eventName, callback) => {
        listeners.set(eventName, callback);
      },
      removeEventListener: (eventName) => {
        listeners.delete(eventName);
      },
    };

    const readyPromise = waitForDesktopBridge();
    globalThis.window.pywebview = { api: {} };
    listeners.get('pywebviewready')();

    await expect(readyPromise).resolves.toBe(true);
  });

  it('resolves false after timeout when the bridge never appears', async () => {
    globalThis.window = createDesktopWindowWithoutBridge();

    const readyPromise = waitForDesktopBridge(100);
    await vi.advanceTimersByTimeAsync(100);

    await expect(readyPromise).resolves.toBe(false);
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

  it('calls callback when config changes while state is unchanged', async () => {
    const callbacks = [];
    let pollCount = 0;
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: {
        api: {
          getWakewordStatus: () => {
            pollCount += 1;
            return {
              state: 'listening',
              enabled: true,
              target_agent_id: pollCount === 1 ? 'main' : 'writer',
            };
          },
        },
      },
    };

    const cleanup = onWakewordStatusChange((status) => {
      callbacks.push(status);
    }, 100);

    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(100);

    expect(callbacks).toHaveLength(2);
    expect(callbacks[1].target_agent_id).toBe('writer');

    cleanup();
  });

  it('returns noop cleanup when not on desktop', () => {
    globalThis.window = { location: { search: '' }, pywebview: undefined };

    const cleanup = onWakewordStatusChange(() => {});
    expect(typeof cleanup).toBe('function');
    expect(cleanup()).toBeUndefined();
  });
});

function createDesktopWindowWithoutBridge() {
  const listeners = new Map();

  return {
    location: { search: '?accessor=desktop' },
    pywebview: undefined,
    addEventListener: (eventName, callback) => {
      listeners.set(eventName, callback);
    },
    removeEventListener: (eventName) => {
      listeners.delete(eventName);
    },
  };
}
