import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';

import {
  isDesktop,
  isDesktopAccessor,
  getDesktopCapabilities,
  getDesktopClipboardText,
  openDesktopExternalUrl,
  setDesktopClipboardText,
  listDesktopServers,
  addDesktopServer,
  removeDesktopServer,
  selectDesktopServer,
  getWakewordStatus,
  setWakewordEnabled,
  setWakewordConfig,
  listMicrophones,
  listWakewordModels,
  importWakewordModel,
  deleteWakewordModel,
  retryWakeword,
  startWakewordCalibration,
  stopWakewordCalibration,
  restartWakewordCalibration,
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
          getDesktopCapabilities: () => ({
            wakeword: true,
            serverSelection: true,
            contextMenu: true,
          }),
        },
      },
    };

    const caps1 = await getDesktopCapabilities();
    expect(caps1).toEqual({
      wakeword: true,
      serverSelection: true,
      contextMenu: true,
    });

    // Second call should return cached result
    const caps2 = await getDesktopCapabilities();
    expect(caps2).toEqual({
      wakeword: true,
      serverSelection: true,
      contextMenu: true,
    });
  });

  it('returns disabled when bridge absent', async () => {
    globalThis.window = { location: { search: '' }, pywebview: undefined };

    const caps = await getDesktopCapabilities();
    expect(caps).toEqual({
      wakeword: false,
      serverSelection: false,
      contextMenu: false,
    });
  });

  it('does not reuse cached capabilities for a different bridge api object', async () => {
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: {
        api: {
          getDesktopCapabilities: () => ({
            wakeword: true,
            serverSelection: true,
            contextMenu: true,
          }),
        },
      },
    };

    expect(await getDesktopCapabilities()).toEqual({
      wakeword: true,
      serverSelection: true,
      contextMenu: true,
    });

    globalThis.window.pywebview = {
      api: {
        getDesktopCapabilities: () => ({ wakeword: false }),
      },
    };

    expect(await getDesktopCapabilities()).toEqual({
      wakeword: false,
      serverSelection: false,
      contextMenu: false,
    });
  });
});

describe('desktop context-menu actions', () => {
  it('reads, writes, and opens through the native bridge', async () => {
    const setClipboardText = vi.fn(() => ({ copied: true }));
    const openExternalUrl = vi.fn(() => ({ opened: true }));
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: {
        api: {
          setClipboardText,
          getClipboardText: () => 'paste me',
          openExternalUrl,
        },
      },
    };

    await expect(setDesktopClipboardText('copy me')).resolves.toEqual({
      copied: true,
    });
    await expect(getDesktopClipboardText()).resolves.toBe('paste me');
    await expect(
      openDesktopExternalUrl('https://example.com/path'),
    ).resolves.toEqual({ opened: true });
    expect(setClipboardText).toHaveBeenCalledWith('copy me');
    expect(openExternalUrl).toHaveBeenCalledWith('https://example.com/path');
  });

  it('normalizes a non-text clipboard response to empty text', async () => {
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: { api: { getClipboardText: () => null } },
    };

    await expect(getDesktopClipboardText()).resolves.toBe('');
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

describe('desktop server management', () => {
  it('lists, adds, removes, and probes servers through the bridge', async () => {
    const servers = [
      { host: 'pi.lan', port: 8420, label: 'Home', active: true },
    ];
    const addServer = vi.fn(() => ({
      host: 'office.lan',
      port: 9000,
      label: 'Office',
    }));
    const removeServer = vi.fn(() => ({ removed: true }));
    const selectServer = vi.fn(() => ({
      status: 'server_unreachable',
      error_title: 'Server unreachable',
      error_body: 'Try again.',
    }));
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: {
        api: {
          listServers: () => servers,
          addServer,
          removeServer,
          selectServer,
        },
      },
    };

    await expect(listDesktopServers()).resolves.toEqual(servers);
    await expect(
      addDesktopServer('office.lan', 9000, 'Office'),
    ).resolves.toEqual({
      host: 'office.lan',
      port: 9000,
      label: 'Office',
    });
    await expect(removeDesktopServer('office.lan', 9000)).resolves.toEqual({
      removed: true,
    });
    await expect(selectDesktopServer('office.lan', 9000)).resolves.toEqual({
      status: 'server_unreachable',
      error_title: 'Server unreachable',
      error_body: 'Try again.',
    });
    expect(addServer).toHaveBeenCalledWith('office.lan', 9000, 'Office');
    expect(removeServer).toHaveBeenCalledWith('office.lan', 9000);
    expect(selectServer).toHaveBeenCalledWith('office.lan', 9000);
  });

  it('returns an empty list when the bridge returns no server array', async () => {
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: { api: { listServers: () => null } },
    };

    await expect(listDesktopServers()).resolves.toEqual([]);
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
            engine: 'pyopen_wakeword',
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
            return { enabled: val, error_code: null };
          },
        },
      },
    };

    await expect(setWakewordEnabled(true)).resolves.toEqual({
      enabled: true,
      error_code: null,
    });
    expect(enabledCalls).toEqual([true]);

    await expect(setWakewordEnabled(false)).resolves.toEqual({
      enabled: false,
      error_code: null,
    });
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

    await setWakewordConfig({
      model_sensitivities: { 'builtin/okay_nabu': 0.8 },
    });
    expect(calls).toEqual([
      { model_sensitivities: { 'builtin/okay_nabu': 0.8 } },
    ]);
  });
});

describe('desktop Voice recovery and devices', () => {
  it('returns microphone devices from the bridge', async () => {
    const devices = [{ index: 3, name: 'Desk mic', supported: true }];
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: { api: { listMicrophones: () => devices } },
    };

    await expect(listMicrophones()).resolves.toEqual(devices);
  });

  it('asks the bridge to retry wakeword listening', async () => {
    const retry = vi.fn();
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: { api: { retryWakeword: retry } },
    };

    await retryWakeword();

    expect(retry).toHaveBeenCalledOnce();
  });

  it('controls transient wakeword calibration through the bridge', async () => {
    const start = vi.fn(() => ({ calibration: { active: true } }));
    const stop = vi.fn(() => ({ calibration: { active: false } }));
    const restart = vi.fn(() => ({
      calibration: { active: true, phase: 'noise' },
    }));
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: {
        api: {
          startWakewordCalibration: start,
          stopWakewordCalibration: stop,
          restartWakewordCalibration: restart,
        },
      },
    };

    await expect(startWakewordCalibration()).resolves.toEqual({
      calibration: { active: true },
    });
    await expect(restartWakewordCalibration()).resolves.toEqual({
      calibration: { active: true, phase: 'noise' },
    });
    await expect(stopWakewordCalibration()).resolves.toEqual({
      calibration: { active: false },
    });
    expect(start).toHaveBeenCalledOnce();
    expect(restart).toHaveBeenCalledOnce();
    expect(stop).toHaveBeenCalledOnce();
  });
});

describe('desktop wakeword models', () => {
  it('lists, imports, and deletes models through the bridge', async () => {
    const models = [{ id: 'builtin/okay_nabu', label: 'Okay Nabu' }];
    const importModel = vi.fn(() => ({
      id: 'custom/model',
      label: 'Hey Computer',
    }));
    const deleteModel = vi.fn(() => ({ deleted: true }));
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: {
        api: {
          listWakewordModels: () => models,
          importWakewordModel: importModel,
          deleteWakewordModel: deleteModel,
        },
      },
    };

    await expect(listWakewordModels()).resolves.toEqual(models);
    await expect(
      importWakewordModel('computer.tflite', 'b25ueA=='),
    ).resolves.toEqual({ id: 'custom/model', label: 'Hey Computer' });
    await expect(deleteWakewordModel('custom/model')).resolves.toEqual({
      deleted: true,
    });
    expect(importModel).toHaveBeenCalledWith('computer.tflite', 'b25ueA==');
    expect(deleteModel).toHaveBeenCalledWith('custom/model');
  });

  it('returns an empty model list when the bridge is unavailable', async () => {
    globalThis.window = { location: { search: '' }, pywebview: undefined };

    await expect(listWakewordModels()).resolves.toEqual([]);
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

  it('never overlaps slow status polls or publishes after cleanup', async () => {
    const first = deferred();
    const second = deferred();
    const getStatus = vi
      .fn()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    const callback = vi.fn();
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: { api: { getWakewordStatus: getStatus } },
    };

    const cleanup = onWakewordStatusChange(callback, 100);
    await vi.advanceTimersByTimeAsync(500);
    expect(getStatus).toHaveBeenCalledOnce();

    first.resolve({ state: 'listening', enabled: true });
    await vi.advanceTimersByTimeAsync(0);
    expect(callback).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(100);
    expect(getStatus).toHaveBeenCalledTimes(2);

    cleanup();
    second.resolve({ state: 'off', enabled: false });
    await vi.advanceTimersByTimeAsync(500);
    expect(callback).toHaveBeenCalledOnce();
    expect(getStatus).toHaveBeenCalledTimes(2);
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

function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}
