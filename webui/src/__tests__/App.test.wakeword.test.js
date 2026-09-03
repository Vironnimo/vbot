// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount } from 'svelte';

import {
  App,
  cleanupAppHarness,
  resetAppHarness,
  waitForAssertion,
} from './App.support.js';

vi.mock('svelte', async () => {
  return import('../../node_modules/svelte/src/index-client.js');
});

describe('App Desktop Wakeword feedback', () => {
  let mountedComponent;

  beforeEach(() => {
    resetAppHarness();
    mountedComponent = null;
    window.history.replaceState({}, '', '/?accessor=desktop');
  });

  afterEach(async () => {
    vi.useRealTimers();
    mountedComponent = await cleanupAppHarness(mountedComponent);
  });

  it('initializes Wakeword after the Desktop bridge arrives late', async () => {
    vi.useFakeTimers();
    window.pywebview = undefined;
    const getCapabilities = vi.fn().mockResolvedValue({
      wakeword: true,
      serverSelection: false,
      contextMenu: false,
    });
    const getStatus = vi.fn().mockResolvedValue({
      enabled: true,
      state: 'listening',
      events: [],
    });

    mountedComponent = mount(App, { target: document.body });
    flushSync();
    await vi.advanceTimersByTimeAsync(1000);

    window.pywebview = {
      api: {
        getDesktopCapabilities: getCapabilities,
        getWakewordStatus: getStatus,
      },
    };
    await vi.advanceTimersByTimeAsync(1000);
    flushSync();

    expect(getCapabilities).toHaveBeenCalledOnce();
    expect(getStatus).toHaveBeenCalled();
  });

  it('shows a sticky global Toast when Wakeword activation lacks STT', async () => {
    vi.useFakeTimers();
    const status = {
      enabled: false,
      state: 'error',
      error_code: 'speech_to_text_unconfigured',
      events: [
        {
          sequence: 1,
          state: 'error',
          error_code: 'speech_to_text_unconfigured',
        },
      ],
    };
    window.pywebview = {
      api: {
        getDesktopCapabilities: vi.fn().mockResolvedValue({
          wakeword: true,
          serverSelection: false,
        }),
        getWakewordStatus: vi
          .fn()
          .mockImplementation(() => Promise.resolve(status)),
      },
    };

    mountedComponent = mount(App, { target: document.body });
    flushSync();
    await vi.advanceTimersByTimeAsync(0);
    flushSync();

    await waitForAssertion(() => {
      const toast = document.querySelector('.toast.error');
      expect(toast).toBeTruthy();
      expect(toast.closest('[aria-live="polite"]')).toBeTruthy();
    });

    await vi.advanceTimersByTimeAsync(10000);
    flushSync();
    expect(document.querySelector('.toast.error')).toBeTruthy();
  });

  it('shows an auto-dismissing warning when a running microphone disconnects', async () => {
    vi.useFakeTimers();
    let status = {
      enabled: true,
      state: 'listening',
      error_code: null,
      events: [{ sequence: 1, state: 'listening', error_code: null }],
    };
    window.pywebview = {
      api: {
        getDesktopCapabilities: vi.fn().mockResolvedValue({
          wakeword: true,
          serverSelection: false,
        }),
        getWakewordStatus: vi
          .fn()
          .mockImplementation(() => Promise.resolve(status)),
      },
    };

    mountedComponent = mount(App, { target: document.body });
    flushSync();
    await vi.advanceTimersByTimeAsync(0);
    flushSync();

    status = {
      enabled: true,
      state: 'microphone_disconnected',
      error_code: 'microphone_read_failed',
      events: [
        { sequence: 1, state: 'listening', error_code: null },
        {
          sequence: 2,
          state: 'microphone_disconnected',
          error_code: 'microphone_read_failed',
        },
      ],
    };
    await vi.advanceTimersByTimeAsync(500);
    flushSync();

    const warningToast = document.querySelector('.toast.warn');
    expect(warningToast).toBeTruthy();
    expect(warningToast.closest('[aria-live="polite"]')).toBeTruthy();
    expect(document.querySelector('.toast.error')).toBeNull();

    await vi.advanceTimersByTimeAsync(3200);
    flushSync();
    expect(document.querySelector('.toast.warn')).toBeNull();
  });
});
