// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount } from 'svelte';

import {
  App,
  cleanupAppHarness,
  resetAppHarness,
  rpcMock,
  subscribeServerEventsMock,
  waitForAssertion,
} from './App.support.js';

vi.mock('svelte', async () => {
  return import('../../node_modules/svelte/src/index-client.js');
});

describe('App', () => {
  let mountedComponent;

  beforeEach(() => {
    resetAppHarness();
    mountedComponent = null;
  });

  afterEach(async () => {
    mountedComponent = await cleanupAppHarness(mountedComponent);
    document.documentElement.removeAttribute('style');
  });

  it('passes the live shell theme and display context to an Extension page', async () => {
    document.documentElement.style.setProperty('--bg', '#111111');
    document.documentElement.style.setProperty('--surface', '#222222');
    document.documentElement.style.setProperty('--surface-2', '#333333');
    document.documentElement.style.setProperty('--border', '#444444');
    document.documentElement.style.setProperty('--text-hi', '#eeeeee');
    document.documentElement.style.setProperty('--text-med', '#999999');
    document.documentElement.style.setProperty('--accent', '#ff8800');
    document.documentElement.style.colorScheme = 'dark';
    rpcMock.mockImplementation(async (method) => {
      if (method === 'extensions.pages') {
        return {
          pages: [
            {
              extension: 'fixture',
              page: 'main',
              title: 'Fixture page',
              route: 'extension:fixture:main',
              entry_url: '/fixture-page.html',
              epoch: 'fixture-epoch',
            },
          ],
        };
      }
      if (method === 'settings.get') {
        return {
          general: { timezone: 'Europe/Berlin' },
          appearance: { language: 'de', available_languages: ['de'] },
        };
      }
      if (method === 'agent.list') return { agents: [] };
      if (method === 'chat.commands') return { items: [] };
      if (method === 'skill.list') return { skills: [], invalid_skills: [] };
      throw new Error(`Unexpected RPC method: ${method}`);
    });
    mountedComponent = mount(App, { target: document.body });
    flushSync();

    await waitForAssertion(() => {
      expect(
        Array.from(document.querySelectorAll('button')).find((button) =>
          button.textContent.includes('Fixture page'),
        ),
      ).toBeTruthy();
    });
    Array.from(document.querySelectorAll('button'))
      .find((button) => button.textContent.includes('Fixture page'))
      .click();
    flushSync();

    const frame = document.querySelector('iframe');
    const child = frame.contentWindow;
    const sent = vi.spyOn(child, 'postMessage');
    frame.dispatchEvent(new Event('load'));
    const init = sent.mock.calls.find(
      ([message]) => message.type === 'vbot.extension.init',
    )[0];
    expect(init).toMatchObject({
      locale: 'de',
      timezone: 'Europe/Berlin',
      theme: {
        mode: 'dark',
        background: '#111111',
        surface: '#222222',
        elevatedSurface: '#333333',
        border: '#444444',
        text: '#eeeeee',
        mutedText: '#999999',
        accent: '#ff8800',
      },
    });

    const ready = {
      type: 'vbot.extension.ready',
      version: 1,
      nonce: init.nonce,
      epoch: init.epoch,
      descriptor: init.descriptor,
    };
    const event = new MessageEvent('message', { data: ready });
    Object.defineProperties(event, {
      origin: { value: 'null' },
      source: { value: child },
    });
    window.dispatchEvent(event);
    document.documentElement.style.setProperty('--accent', '#00cc88');

    await waitForAssertion(() => {
      expect(
        sent.mock.calls.some(
          ([message]) =>
            message.type === 'vbot.extension.context' &&
            message.theme.accent === '#00cc88',
        ),
      ).toBe(true);
    });
  });

  it('maps app_error WebSocket events to error toasts', () => {
    mountedComponent = mount(App, { target: document.body });
    flushSync();

    expect(subscribeServerEventsMock).toHaveBeenCalledTimes(1);
    const [handlers] = subscribeServerEventsMock.mock.calls[0];

    handlers.onEvent({
      type: 'app_error',
      sequence: 1,
      payload: { message: 'Provider credentials are missing.' },
    });
    flushSync();

    const toast = document.querySelector('.toast.error');
    expect(toast).toBeTruthy();
    expect(toast.closest('[aria-live="polite"]')).toBeTruthy();
    expect(toast.textContent).toContain('Provider credentials are missing.');
  });

  it('keeps error toasts on screen past the auto-dismiss window (sticky by default)', () => {
    vi.useFakeTimers();
    try {
      mountedComponent = mount(App, { target: document.body });
      flushSync();

      const [handlers] = subscribeServerEventsMock.mock.calls[0];

      // An error toast without an explicit autoDismiss stays until the user
      // dismisses it — a transport/server failure they must acknowledge.
      handlers.onEvent({
        type: 'app_error',
        sequence: 1,
        payload: { message: 'Provider credentials are missing.' },
      });
      flushSync();
      expect(document.querySelector('.toast.error')).toBeTruthy();

      // TOAST_AUTO_DISMISS_MS is 3200ms; advancing well past it leaves the
      // error toast in place (success/info/warn would have gone by now).
      vi.advanceTimersByTime(10000);
      flushSync();
      expect(document.querySelector('.toast.error')).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  it('shows one global server notice across tabs and refreshes after reconnect', () => {
    vi.useFakeTimers();
    try {
      mountedComponent = mount(App, { target: document.body });
      flushSync();

      expect(document.querySelector('.server-availability-notice')).toBeNull();
      const initialConnectionHandlers =
        subscribeServerEventsMock.mock.calls[0][0];
      initialConnectionHandlers.onEvent({
        type: 'app_error',
        sequence: 1,
        payload: { message: 'A saved operation failed.' },
      });
      flushSync();
      const unrelatedErrorToast = document.querySelector('.toast.error');
      expect(unrelatedErrorToast).toBeTruthy();

      initialConnectionHandlers.onClose();
      flushSync();

      expect(
        document.querySelector('.app-shell')?.dataset.serverUnavailable,
      ).toBe('true');
      expect(document.querySelector('.app-shell__content')?.inert).toBe(true);

      vi.advanceTimersByTime(999);
      flushSync();
      expect(document.querySelector('.server-availability-notice')).toBeNull();

      vi.advanceTimersByTime(1);
      flushSync();
      const offlineNotice = document.querySelector(
        '.server-availability-notice',
      );
      expect(offlineNotice).toBeTruthy();
      expect(offlineNotice.getAttribute('role')).toBe('alert');
      // The offline notice replaces connection symptoms, not unrelated sticky
      // errors the user has not dismissed.
      expect(document.querySelector('.toast.error')).toBe(unrelatedErrorToast);

      const agentsNavigation = [
        ...document.querySelectorAll('.app-shell__nav-item'),
      ].find((item) => item.textContent.includes('Agents'));
      agentsNavigation.click();
      flushSync();
      expect(agentsNavigation.getAttribute('aria-current')).toBe('page');
      expect(document.querySelector('.server-availability-notice')).toBe(
        offlineNotice,
      );

      const connectionCountBeforeRetry =
        subscribeServerEventsMock.mock.calls.length;
      const retryButton = [...offlineNotice.querySelectorAll('button')].find(
        (button) => button.textContent.includes('Retry now'),
      );
      retryButton.click();
      flushSync();
      expect(subscribeServerEventsMock).toHaveBeenCalledTimes(
        connectionCountBeforeRetry + 1,
      );

      const agentLoadsBeforeRecovery = rpcMock.mock.calls.filter(
        ([method]) => method === 'agent.list',
      ).length;
      const recoveredConnectionHandlers =
        subscribeServerEventsMock.mock.calls.at(-1)[0];
      recoveredConnectionHandlers.onOpen();
      flushSync();

      const restoredNotice = document.querySelector(
        '.server-availability-notice--restored',
      );
      expect(restoredNotice).toBeTruthy();
      expect(restoredNotice.getAttribute('role')).toBe('status');
      expect(
        document.querySelector('.app-shell__content')?.inert,
      ).toBeUndefined();
      expect(
        rpcMock.mock.calls.filter(([method]) => method === 'agent.list').length,
      ).toBeGreaterThan(agentLoadsBeforeRecovery);

      vi.advanceTimersByTime(1400);
      flushSync();
      expect(document.querySelector('.server-availability-notice')).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it('offers Desktop server switching outside the inert app content', async () => {
    vi.useFakeTimers();
    try {
      window.history.replaceState({}, '', '/?accessor=desktop');
      window.pywebview = {
        api: {
          getDesktopCapabilities: vi.fn().mockResolvedValue({
            wakeword: false,
            serverSelection: true,
          }),
          listServers: vi
            .fn()
            .mockResolvedValue([
              { host: 'pi.lan', port: 8420, label: 'Home', active: true },
            ]),
        },
      };

      mountedComponent = mount(App, { target: document.body });
      flushSync();
      await vi.advanceTimersByTimeAsync(0);
      flushSync();

      subscribeServerEventsMock.mock.calls[0][0].onClose();
      await vi.advanceTimersByTimeAsync(1000);
      flushSync();

      const notice = document.querySelector('.server-availability-notice');
      const switchButton = [...notice.querySelectorAll('button')].find(
        (button) => button.textContent.includes('Switch server'),
      );
      expect(switchButton).toBeTruthy();

      switchButton.click();
      flushSync();
      await vi.advanceTimersByTimeAsync(0);
      flushSync();

      const modal = document.querySelector('[role="dialog"]');
      expect(modal).toBeTruthy();
      expect(modal.closest('.app-shell__content')).toBeNull();
      expect(document.querySelector('.server-availability-notice')).toBeNull();
      expect(modal.textContent).toContain('Home');
      expect(
        modal.querySelector('.desktop-server-row .chip.success'),
      ).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });
});
