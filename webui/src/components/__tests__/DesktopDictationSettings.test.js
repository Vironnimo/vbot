// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { formatDateTimeInApplicationZone } from '../../lib/dateTimePrefs.svelte.js';
import { activeLocaleTag, init, t } from '../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: DesktopDictationSettings } =
  await import('../settings/DesktopDictationSettings.svelte');

const CTRL_ALT_D = {
  ctrl: true,
  alt: true,
  shift: false,
  win: false,
  key: 'KeyD',
};
const FAILED_AT = '2026-10-05T09:30:00Z';

function status(overrides = {}) {
  return {
    supported: true,
    enabled: false,
    hotkey: { ...CTRL_ALT_D },
    mode: 'toggle',
    error_code: null,
    state: 'idle',
    last_failure: null,
    ...overrides,
  };
}

describe('DesktopDictationSettings', () => {
  let mountedComponent;
  let api;

  beforeEach(() => {
    document.body.innerHTML = '';
    window.history.replaceState({}, '', '/?accessor=desktop');
    init('en');
    // What the Desktop saved, as `setDictation` answers it.
    let saved = status();
    api = {
      getDictation: vi.fn().mockResolvedValue(status()),
      setDictation: vi.fn(async (changes) => {
        const { enabled, mode, ...hotkey } = changes;
        saved = {
          ...saved,
          ...(enabled === undefined ? {} : { enabled }),
          ...(mode === undefined ? {} : { mode }),
          ...(Object.keys(hotkey).length ? { hotkey } : {}),
        };
        return saved;
      }),
    };
    window.pywebview = { api };
    mountedComponent = null;
  });

  afterEach(async () => {
    vi.useRealTimers();
    if (mountedComponent) await unmount(mountedComponent);
    delete window.pywebview;
    document.body.innerHTML = '';
  });

  async function render(props = {}) {
    mountedComponent = mount(DesktopDictationSettings, {
      target: document.body,
      props,
    });
    flushSync();
    await waitFor(() => captureButton() && !captureButton().disabled);
  }

  const enableSwitch = () =>
    document.querySelector(
      `button[role="switch"][aria-label="${t('settings.dictation.enabledAria')}"]`,
    );
  const captureButton = () =>
    document.querySelector('.shortcut-combination__capture');
  const modeDropdown = () =>
    document.querySelector(
      `button[aria-label="${t('settings.dictation.mode')}"]`,
    );
  const lastResult = () =>
    document.querySelector('.dictation-last-result')?.textContent.trim() ??
    null;

  it('changes the switch, key combination and mode through the Desktop', async () => {
    await render();
    expect(captureButton().textContent.trim()).toBe('Ctrl + Alt + D');
    expect(modeDropdown().textContent).toContain(
      t('settings.dictation.modeToggle'),
    );
    expect(document.body.textContent).toContain(
      t('settings.dictation.modeToggleDescription'),
    );

    enableSwitch().click();
    await waitFor(() => enableSwitch().getAttribute('aria-checked') === 'true');
    expect(api.setDictation).toHaveBeenLastCalledWith({ enabled: true });

    captureButton().click();
    flushSync();
    captureButton().dispatchEvent(
      new KeyboardEvent('keydown', {
        bubbles: true,
        cancelable: true,
        code: 'F13',
      }),
    );
    await waitFor(() => captureButton().textContent.trim() === 'F13');
    expect(api.setDictation).toHaveBeenLastCalledWith({
      ctrl: false,
      alt: false,
      shift: false,
      win: false,
      key: 'F13',
    });

    modeDropdown().click();
    flushSync();
    option(t('settings.dictation.modeHold')).click();
    await waitFor(() =>
      document.body.textContent.includes(
        t('settings.dictation.modeHoldDescription'),
      ),
    );
    expect(api.setDictation).toHaveBeenLastCalledWith({ mode: 'hold' });
  });

  it.each([
    ['hotkey_in_use', 'settings.shortcut.error.inUse'],
    ['hotkey_invalid', 'settings.shortcut.error.invalid'],
    ['dictation_config_invalid', 'settings.dictation.error.configInvalid'],
  ])('explains %s from the Desktop', async (code, key) => {
    api.getDictation.mockResolvedValue(
      status({ enabled: true, error_code: code }),
    );
    await render();

    expect(document.querySelector('.banner--warn').textContent).toContain(
      t(key),
    );
    // A failed registration keeps the saved preference.
    expect(enableSwitch().getAttribute('aria-checked')).toBe('true');
  });

  it('explains where dictation is available when this Desktop cannot dictate', async () => {
    api.getDictation.mockResolvedValue(status({ supported: false }));
    mountedComponent = mount(DesktopDictationSettings, {
      target: document.body,
    });
    flushSync();
    await waitFor(() =>
      document.body.textContent.includes(t('settings.dictation.unsupported')),
    );

    expect(enableSwitch().disabled).toBe(true);
    expect(captureButton().disabled).toBe(true);
    expect(modeDropdown().disabled).toBe(true);
  });

  it.each([
    ['inserted_to_clipboard', FAILED_AT, 'settings.dictation.result.clipboard'],
    ['insert_failed', FAILED_AT, 'settings.dictation.result.insertFailed'],
    ['nothing_heard', null, 'settings.dictation.result.nothingHeard'],
    ['something_new', FAILED_AT, 'settings.dictation.result.failed'],
  ])(
    'tells how the last dictation ended with %s',
    async (code, at, messageKey) => {
      api.getDictation.mockResolvedValue(
        status({ last_failure: { code, at } }),
      );
      await render();

      const message = t(messageKey);
      expect(lastResult()).toBe(
        at
          ? t('settings.dictation.lastResult', {
              time: formatDateTimeInApplicationZone(at, activeLocaleTag(), {
                dateStyle: 'medium',
                timeStyle: 'short',
              }),
              message,
            })
          : message,
      );
    },
  );

  it('reads the state again until a pushed dictation ended', async () => {
    vi.useFakeTimers();
    await renderWithFakeTimers();
    expect(lastResult()).toBeNull();

    // The end of the recording can arrive before the Desktop's state moved on.
    api.getDictation.mockResolvedValue(status({ state: 'recording' }));
    pushRecording(true);
    await settle();
    pushRecording(false);
    await settle();
    expect(api.getDictation).toHaveBeenCalledTimes(3);

    // Still running: read again a second later.
    api.getDictation.mockResolvedValue(status({ state: 'transcribing' }));
    await vi.advanceTimersByTimeAsync(1000);
    await settle();
    expect(api.getDictation).toHaveBeenCalledTimes(4);

    api.getDictation.mockResolvedValue(
      status({ last_failure: { code: 'transcription_failed', at: null } }),
    );
    await vi.advanceTimersByTimeAsync(1000);
    await settle();
    expect(lastResult()).toBe(
      t('settings.dictation.result.transcriptionFailed'),
    );

    // Idle: no more reads.
    await vi.advanceTimersByTimeAsync(5000);
    expect(api.getDictation).toHaveBeenCalledTimes(5);
  });

  it('retries when the Desktop does not answer the first load', async () => {
    api.getDictation
      .mockRejectedValueOnce(new Error('bridge starting'))
      .mockResolvedValue(status({ enabled: true }));
    mountedComponent = mount(DesktopDictationSettings, {
      target: document.body,
    });
    flushSync();
    await waitFor(() =>
      document.body.textContent.includes(t('settings.dictation.loadError')),
    );

    buttonByText(t('common.retry')).click();
    await waitFor(
      () => enableSwitch()?.getAttribute('aria-checked') === 'true',
    );
  });

  async function renderWithFakeTimers() {
    mountedComponent = mount(DesktopDictationSettings, {
      target: document.body,
    });
    flushSync();
    await settle();
  }
});

function pushRecording(recording) {
  window.dispatchEvent(
    new CustomEvent('vbot-desktop-dictation', { detail: { recording } }),
  );
}

async function settle() {
  for (let index = 0; index < 5; index += 1) {
    await vi.advanceTimersByTimeAsync(0);
    flushSync();
  }
}

function option(label) {
  return [...document.body.querySelectorAll('[role="option"]')].find(
    (element) =>
      element
        .querySelector('.dropdown-primitive__option-label')
        ?.textContent.trim() === label,
  );
}

function buttonByText(text) {
  return [...document.body.querySelectorAll('button')].find(
    (button) => button.textContent.trim() === text,
  );
}

async function waitFor(predicate) {
  // Poll briefly: each bridge answer resolves within a few microtasks.
  await vi.waitFor(
    () => {
      flushSync();
      expect(predicate()).toBe(true);
    },
    { interval: 5 },
  );
}
