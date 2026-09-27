// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../lib/i18n.js';

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
    delete navigator.keyboard;
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
      `button[role="switch"][aria-label="${t('settings.liveShortcut.enabledAria')}"]`,
    );
  const captureButton = () => document.querySelector('.live-shortcut__capture');
  const space = () => t('settings.liveShortcut.space');

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

    expect(captureButton().textContent.trim()).toBe(`Ctrl + Alt + ${space()}`);
    expect(enableSwitch().getAttribute('aria-checked')).toBe('false');

    enableSwitch().click();
    await waitFor(() => enableSwitch().getAttribute('aria-checked') === 'true');
    expect(api.setLiveHotkey).toHaveBeenCalledWith({ enabled: true });
  });

  it('records the next key combination and labels it as the keyboard layout prints it', async () => {
    setLayoutMap(() => Promise.resolve(new Map([['KeyY', 'z']])));
    api.setLiveHotkey.mockImplementation(async (changes) =>
      status({ enabled: true, hotkey: { ...changes } }),
    );
    await render();

    captureButton().click();
    flushSync();
    expect(captureButton().getAttribute('aria-pressed')).toBe('true');
    expect(captureButton().textContent.trim()).toBe(
      t('settings.liveShortcut.capturing'),
    );

    // A modifier alone keeps the capture waiting for the key.
    press({ code: 'ControlLeft', key: 'Control', ctrlKey: true });
    expect(api.setLiveHotkey).not.toHaveBeenCalled();
    press({
      code: 'KeyK',
      key: 'K',
      ctrlKey: true,
      shiftKey: true,
      metaKey: true,
    });

    expect(api.setLiveHotkey).toHaveBeenCalledWith({
      ctrl: true,
      alt: false,
      shift: true,
      win: true,
      key: 'KeyK',
    });
    await waitFor(
      () => captureButton().textContent.trim() === 'Ctrl + Shift + Win + K',
    );
    expect(captureButton().getAttribute('aria-pressed')).toBe('false');

    // Modifiers keep a fixed order; letters follow the layout, while digits,
    // function keys and Space keep their names.
    for (const [keys, label] of [
      [
        {
          code: 'Space',
          metaKey: true,
          shiftKey: true,
          altKey: true,
          ctrlKey: true,
        },
        `Ctrl + Alt + Shift + Win + ${space()}`,
      ],
      [{ code: 'KeyY', altKey: true }, 'Alt + Z'],
      [{ code: 'Digit7', ctrlKey: true }, 'Ctrl + 7'],
      [{ code: 'F13' }, 'F13'],
    ]) {
      captureButton().click();
      flushSync();
      press(keys);
      await waitFor(() => captureButton().textContent.trim() === label);
    }
  });

  it('labels a letter by its key when the keyboard layout cannot be read', async () => {
    setLayoutMap(() => Promise.reject(new Error('insecure')));
    api.getLiveHotkey.mockResolvedValue(
      status({ hotkey: { alt: true, key: 'KeyY' } }),
    );
    await render();

    expect(captureButton().textContent.trim()).toBe('Alt + Y');
  });

  it('cancels recording with Escape without changing the shortcut', async () => {
    await render();

    captureButton().click();
    flushSync();
    press({ code: 'Escape', key: 'Escape' });

    expect(api.setLiveHotkey).not.toHaveBeenCalled();
    expect(captureButton().textContent.trim()).toBe(`Ctrl + Alt + ${space()}`);
  });

  it.each([
    ['hotkey_in_use', 'settings.liveShortcut.error.inUse'],
    ['hotkey_invalid', 'settings.liveShortcut.error.invalid'],
    ['hotkey_failed', 'settings.liveShortcut.error.failed'],
  ])('explains %s from the Desktop', async (code, key) => {
    api.setLiveHotkey.mockResolvedValue(
      status({ enabled: true, error_code: code }),
    );
    await render();

    enableSwitch().click();
    await waitFor(() => document.body.textContent.includes(t(key)));
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
      document.body.textContent.includes(t('settings.liveShortcut.loadError')),
    );

    buttonByText(t('common.retry')).click();
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

    expect(onToast.mock.calls[0][0]).toEqual({
      title: t('errors.generic'),
      message: 'bridge gone',
      variant: 'error',
    });
    expect(enableSwitch().getAttribute('aria-checked')).toBe('false');
  });
});

function setLayoutMap(getLayoutMap) {
  Object.defineProperty(navigator, 'keyboard', {
    configurable: true,
    value: { getLayoutMap },
  });
}

function buttonByText(text) {
  return [...document.body.querySelectorAll('button')].find(
    (button) => button.textContent.trim() === text,
  );
}

async function waitFor(predicate) {
  // Poll briefly: each captured shortcut resolves within a few microtasks.
  await vi.waitFor(
    () => {
      flushSync();
      expect(predicate()).toBe(true);
    },
    { interval: 5 },
  );
}
