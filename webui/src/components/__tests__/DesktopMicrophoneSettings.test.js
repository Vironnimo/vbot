// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: DesktopMicrophoneSettings } =
  await import('../settings/DesktopMicrophoneSettings.svelte');

const STUDIO = {
  index: 4,
  name: 'Studio microphone',
  host_api: 'Windows WASAPI',
};

function device(overrides = {}) {
  return {
    ...STUDIO,
    default_sample_rate: 48000,
    supported: true,
    capture_sample_rate: 48000,
    ...overrides,
  };
}

describe('DesktopMicrophoneSettings', () => {
  let mountedComponent;
  let api;

  beforeEach(() => {
    document.body.innerHTML = '';
    window.history.replaceState({}, '', '/?accessor=desktop');
    init('en');
    api = {
      getMicrophone: vi
        .fn()
        .mockResolvedValue({ device: null, echo_cancellation: true }),
      setMicrophone: vi.fn(async (changes) => ({
        device: null,
        echo_cancellation: true,
        ...changes,
      })),
      listMicrophones: vi.fn().mockResolvedValue([
        device(),
        device({
          index: 5,
          name: 'Bluetooth hands-free',
          supported: false,
          capture_sample_rate: null,
        }),
      ]),
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
    mountedComponent = mount(DesktopMicrophoneSettings, {
      target: document.body,
      props,
    });
    flushSync();
    await waitFor(() => echoSwitch() && !echoSwitch().disabled);
  }

  const microphoneLabel = () => t('settings.microphone.device');
  const deviceDropdown = () =>
    document.querySelector(`button[aria-label="${microphoneLabel()}"]`);
  const echoSwitch = () =>
    document.querySelector(
      `button[role="switch"][aria-label="${t('settings.microphone.echoCancellationAria')}"]`,
    );
  const warning = () => document.querySelector('.banner--warn');

  function openDevices() {
    deviceDropdown().click();
    flushSync();
  }

  it('saves the chosen device by its identity and the system default as null', async () => {
    await render();

    expect(deviceDropdown().textContent).toContain(
      t('settings.microphone.systemDefault'),
    );
    openDevices();
    expect(option('Bluetooth hands-free').disabled).toBe(true);
    option('Studio microphone').click();
    await waitFor(() =>
      deviceDropdown().textContent.includes('Studio microphone'),
    );
    expect(api.setMicrophone).toHaveBeenLastCalledWith({ device: STUDIO });

    openDevices();
    option(t('settings.microphone.systemDefault')).click();
    await waitFor(() => api.setMicrophone.mock.calls.length === 2);
    expect(api.setMicrophone).toHaveBeenLastCalledWith({ device: null });
  });

  it('finds the chosen device under a new index and marks it while it is not connected', async () => {
    api.getMicrophone.mockResolvedValue({
      device: { ...STUDIO, index: 9 },
      echo_cancellation: true,
    });
    api.listMicrophones.mockResolvedValueOnce([]);
    await render();

    // Not in the list: shown as chosen, but not selectable, with a warning.
    expect(deviceDropdown().textContent).toContain('Studio microphone');
    expect(warning().textContent).toContain(
      t('settings.microphone.notConnectedWarning'),
    );

    // Opening the list reads the devices again; the same name and host API
    // under another index is the same microphone.
    openDevices();
    await waitFor(() => warning() === null);
    expect(api.listMicrophones).toHaveBeenCalledTimes(2);
    const selected = document.querySelector(
      '[role="option"][aria-selected="true"]',
    );
    expect(
      selected.querySelector('.dropdown-primitive__option-label').textContent,
    ).toBe('Studio microphone');
    expect(api.setMicrophone).not.toHaveBeenCalled();
  });

  it('turns echo cancellation off as the Desktop confirms', async () => {
    await render();
    expect(echoSwitch().getAttribute('aria-checked')).toBe('true');

    echoSwitch().click();

    await waitFor(() => echoSwitch().getAttribute('aria-checked') === 'false');
    expect(api.setMicrophone).toHaveBeenCalledWith({
      echo_cancellation: false,
    });
  });

  it('reports a rejected change and keeps the confirmed settings', async () => {
    const onToast = vi.fn();
    api.setMicrophone.mockRejectedValue(new Error('microphone_config_invalid'));
    await render({ onToast });

    echoSwitch().click();
    await waitFor(() => onToast.mock.calls.length === 1);

    expect(onToast).toHaveBeenCalledWith({
      title: t('errors.generic'),
      message: t('settings.microphone.error.configInvalid'),
      variant: 'error',
    });
    expect(echoSwitch().getAttribute('aria-checked')).toBe('true');
  });

  it('keeps the microphone while a wake phrase calibration runs', async () => {
    mountedComponent = mount(DesktopMicrophoneSettings, {
      target: document.body,
      props: { desktopVoice: { status: { calibration: { model_id: 'a' } } } },
    });
    flushSync();
    await waitFor(() => api.getMicrophone.mock.calls.length === 1);

    expect(echoSwitch().disabled).toBe(true);
    expect(deviceDropdown().disabled).toBe(true);
  });

  it('retries when the Desktop does not answer the first load', async () => {
    api.getMicrophone
      .mockRejectedValueOnce(new Error('bridge starting'))
      .mockResolvedValue({ device: null, echo_cancellation: false });
    mountedComponent = mount(DesktopMicrophoneSettings, {
      target: document.body,
    });
    flushSync();
    await waitFor(() =>
      document.body.textContent.includes(t('settings.microphone.loadError')),
    );

    buttonByText(t('common.retry')).click();
    await waitFor(() => echoSwitch()?.getAttribute('aria-checked') === 'false');
  });
});

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
