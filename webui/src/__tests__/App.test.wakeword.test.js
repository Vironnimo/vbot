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
      expect(toast.textContent).toContain('Voice needs attention');
      expect(toast.textContent).toContain('Configure a Speech-to-text Model');
    });

    await vi.advanceTimersByTimeAsync(10000);
    flushSync();
    expect(document.querySelector('.toast.error')).toBeTruthy();
  });
});
