// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: DesktopLiveVoiceShortcut } =
  await import('../settings/DesktopLiveVoiceShortcut.svelte');

const CTRL_ALT_SPACE = {
  ctrl: true,
  alt: true,
  shift: false,
  win: false,
  key: 'Space',
};

function status(overrides = {}) {
  return {
    supported: true,
    enabled: false,
    hotkey: { ...CTRL_ALT_SPACE },
    error_code: null,
    ...overrides,
  };
}

describe('DesktopLiveVoiceShortcut', () => {
  let mountedComponent;
  let api;

  beforeEach(() => {
    document.body.innerHTML = '';
    window.history.replaceState({}, '', '/?accessor=desktop');
    init('en');
    api = {
      getLiveHotkey: vi.fn().mockResolvedValue(status()),
      setLiveHotkey: vi.fn(),
    };
    window.pywebview = { api };
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) await unmount(mountedComponent);
    delete window.pywebview;
    document.body.innerHTML = '';
  });

  async function render(props = {}) {
    mountedComponent = mount(DesktopLiveVoiceShortcut, {
      target: document.body,
      props,
    });
    flushSync();
    await waitFor(() => !captureButton().disabled);
  }

  const enableSwitch = () =>
    document.querySelector(
      'button[role="switch"][aria-label="Enable the Live voice shortcut"]',
    );
  const captureButton = () => document.querySelector('.live-shortcut__capture');

  function press(init) {
    captureButton().dispatchEvent(
      new KeyboardEvent('keydown', {
        bubbles: true,
        cancelable: true,
        ...init,
      }),
    );
    flushSync();
  }

  it('shows the saved shortcut and enables it through the Desktop', async () => {
    api.setLiveHotkey.mockResolvedValue(status({ enabled: true }));
    await render();

    expect(captureButton().textContent.trim()).toBe('Ctrl + Alt + Space');
    expect(enableSwitch().getAttribute('aria-checked')).toBe('false');

    enableSwitch().click();
    await waitFor(() => enableSwitch().getAttribute('aria-checked') === 'true');
    expect(api.setLiveHotkey).toHaveBeenCalledWith({ enabled: true });
  });

  it('records the next key combination and keeps waiting through modifiers', async () => {
    api.setLiveHotkey.mockImplementation(async (changes) =>
      status({ enabled: true, hotkey: { ...changes } }),
    );
    await render();

    captureButton().click();
    flushSync();
    expect(captureButton().getAttribute('aria-pressed')).toBe('true');
    expect(captureButton().textContent.trim()).toBe('Press keys…');

    press({ code: 'ControlLeft', key: 'Control', ctrlKey: true });
    expect(api.setLiveHotkey).not.toHaveBeenCalled();
    press({ code: 'KeyK', key: 'K', ctrlKey: true, shiftKey: true });

    expect(api.setLiveHotkey).toHaveBeenCalledWith({
      ctrl: true,
      alt: false,
      shift: true,
      win: false,
      key: 'KeyK',
    });
    await waitFor(
      () => captureButton().textContent.trim() === 'Ctrl + Shift + K',
    );
    expect(captureButton().getAttribute('aria-pressed')).toBe('false');
  });

  it('cancels recording with Escape without changing the shortcut', async () => {
    await render();

    captureButton().click();
    flushSync();
    press({ code: 'Escape', key: 'Escape' });

    expect(api.setLiveHotkey).not.toHaveBeenCalled();
    expect(captureButton().textContent.trim()).toBe('Ctrl + Alt + Space');
  });

  it.each([
    ['hotkey_in_use', 'Another app already uses this key combination'],
    ['hotkey_invalid', 'This key combination cannot be used'],
    ['hotkey_failed', 'Windows could not register the shortcut'],
  ])('explains %s from the Desktop', async (code, text) => {
    api.setLiveHotkey.mockResolvedValue(
      status({ enabled: true, error_code: code }),
    );
    await render();

    enableSwitch().click();
    await waitFor(() => document.body.textContent.includes(text));
    // A failed registration keeps the saved preference.
    expect(enableSwitch().getAttribute('aria-checked')).toBe('true');
  });

  it('retries when the Desktop does not answer the first load', async () => {
    api.getLiveHotkey
      .mockRejectedValueOnce(new Error('bridge starting'))
      .mockResolvedValue(status({ enabled: true }));
    mountedComponent = mount(DesktopLiveVoiceShortcut, {
      target: document.body,
    });
    flushSync();
    await waitFor(() =>
      document.body.textContent.includes(
        'The Desktop app did not return the shortcut settings.',
      ),
    );

    buttonByText('Retry').click();
    await waitFor(
      () => enableSwitch()?.getAttribute('aria-checked') === 'true',
    );
  });

  it('reports a failed change and keeps the confirmed state', async () => {
    const onToast = vi.fn();
    api.setLiveHotkey.mockRejectedValue(new Error('bridge gone'));
    await render({ onToast });

    enableSwitch().click();
    await waitFor(() => onToast.mock.calls.length === 1);

    expect(onToast.mock.calls[0][0]).toMatchObject({
      variant: 'error',
      message: 'bridge gone',
    });
    expect(enableSwitch().getAttribute('aria-checked')).toBe('false');
  });
});

function buttonByText(text) {
  return [...document.body.querySelectorAll('button')].find(
    (button) => button.textContent.trim() === text,
  );
}

async function waitFor(predicate) {
  await vi.waitFor(() => {
    flushSync();
    expect(predicate()).toBe(true);
  });
}
