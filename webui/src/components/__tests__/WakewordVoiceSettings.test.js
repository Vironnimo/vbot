// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/desktopBridge.js', () => ({
  getWakewordStatus: vi.fn(),
  setWakewordEnabled: vi.fn(),
  setWakewordConfig: vi.fn(),
  listMicrophones: vi.fn(),
  listWakewordModels: vi.fn(),
  importWakewordModel: vi.fn(),
  deleteWakewordModel: vi.fn(),
  onWakewordStatusChange: vi.fn(() => () => {}),
  retryWakeword: vi.fn(),
  startWakewordCalibration: vi.fn(),
  stopWakewordCalibration: vi.fn(),
  restartWakewordCalibration: vi.fn(),
  retryWakewordModelCalibration: vi.fn(),
  isDesktop: vi.fn(() => true),
}));
vi.mock('$lib/api.js', () => ({
  updateSettings: vi.fn(),
}));

const desktopBridge = await import('$lib/desktopBridge.js');
const { updateSettings } = await import('$lib/api.js');
const { default: WakewordVoiceSettings } =
  await import('../WakewordVoiceSettings.svelte');

const BUILTIN_MODELS = [
  {
    id: 'builtin/okay_nabu',
    label: 'Okay Nabu',
    source: 'built_in',
    format: 'tflite',
    removable: false,
  },
  {
    id: 'builtin/hey_nabu',
    label: 'Hey Nabu',
    source: 'built_in',
    format: 'tflite',
    removable: false,
  },
];

describe('WakewordVoiceSettings', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
    vi.clearAllMocks();
    desktopBridge.isDesktop.mockReturnValue(true);
    desktopBridge.getWakewordStatus.mockResolvedValue(baseStatus());
    desktopBridge.listMicrophones.mockResolvedValue([
      {
        index: 4,
        name: 'Studio microphone',
        supported: true,
        default_sample_rate: 48000,
      },
      {
        index: 5,
        name: 'Bluetooth hands-free',
        supported: false,
        default_sample_rate: 8000,
      },
    ]);
    desktopBridge.listWakewordModels.mockResolvedValue(BUILTIN_MODELS);
    desktopBridge.setWakewordConfig.mockResolvedValue(undefined);
    desktopBridge.setWakewordEnabled.mockResolvedValue(undefined);
    desktopBridge.deleteWakewordModel.mockResolvedValue({ deleted: true });
    desktopBridge.retryWakeword.mockResolvedValue(undefined);
    desktopBridge.retryWakewordModelCalibration.mockResolvedValue(undefined);
    desktopBridge.startWakewordCalibration.mockResolvedValue({
      ...baseStatus(),
      enabled: true,
      state: 'listening',
      calibration: {
        active: true,
        phase: 'ready',
        scores: {
          'builtin/okay_nabu': 0.34,
          'builtin/hey_nabu': 0.28,
        },
        peaks: {
          'builtin/okay_nabu': 0.72,
          'builtin/hey_nabu': 0.64,
        },
        noise_levels: {
          'builtin/okay_nabu': 0.03,
          'builtin/hey_nabu': 0.02,
        },
        sample_counts: {
          'builtin/okay_nabu': 5,
          'builtin/hey_nabu': 5,
        },
        required_samples: 5,
        target_model_id: null,
        recommended_sensitivities: {
          'builtin/okay_nabu': 0.65,
          'builtin/hey_nabu': 0.65,
        },
        noise_seconds_remaining: 0,
        noise_high: false,
      },
    });
    desktopBridge.stopWakewordCalibration.mockResolvedValue({
      ...baseStatus(),
      enabled: true,
      state: 'listening',
    });
    desktopBridge.restartWakewordCalibration.mockResolvedValue({
      ...baseStatus(),
      enabled: true,
      state: 'listening',
      calibration: {
        active: true,
        phase: 'noise',
        scores: {
          'builtin/okay_nabu': 0,
          'builtin/hey_nabu': 0,
        },
        peaks: {
          'builtin/okay_nabu': 0,
          'builtin/hey_nabu': 0,
        },
        noise_levels: {},
        sample_counts: {
          'builtin/okay_nabu': 0,
          'builtin/hey_nabu': 0,
        },
        required_samples: 5,
        target_model_id: null,
        recommended_sensitivities: {},
        noise_seconds_remaining: 3,
        noise_high: false,
      },
    });
    updateSettings.mockImplementation(async (payload) => payload);
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
  });

  it('shows a microphone warning and compatible device picker', async () => {
    desktopBridge.getWakewordStatus.mockResolvedValue({
      ...baseStatus(),
      enabled: true,
      state: 'microphone_disconnected',
      error_code: 'microphone_unavailable',
    });

    await mountPanel();

    expect(
      document.querySelector(
        '.voice-attention-banner.banner--warn[role="status"]',
      ),
    ).not.toBeNull();
    expect(document.querySelector('.banner--error')).toBeNull();
    expect(buttonByText('Retry listening')).not.toBeNull();

    buttonByLabel('Microphone').click();
    flushSync();
    expect(buttonByText('Studio microphone')).not.toBeNull();
    expect(buttonContainingText('Bluetooth hands-free').disabled).toBe(true);
  });

  it('does not start status polling after unmount during initial load', async () => {
    const status = deferred();
    const microphones = deferred();
    const models = deferred();
    desktopBridge.getWakewordStatus.mockReturnValue(status.promise);
    desktopBridge.listMicrophones.mockReturnValue(microphones.promise);
    desktopBridge.listWakewordModels.mockReturnValue(models.promise);

    mountedComponent = mount(WakewordVoiceSettings, {
      target: document.body,
    });
    flushSync();
    await Promise.resolve();
    const component = mountedComponent;
    mountedComponent = null;
    await unmount(component);

    status.resolve(baseStatus());
    microphones.resolve([]);
    models.resolve([]);
    await Promise.resolve();
    await Promise.resolve();

    expect(desktopBridge.onWakewordStatusChange).not.toHaveBeenCalled();
  });

  it('shows a warning instead of an error when a running microphone disconnects', async () => {
    desktopBridge.getWakewordStatus.mockResolvedValue({
      ...baseStatus(),
      enabled: true,
      state: 'microphone_disconnected',
      error_code: 'microphone_read_failed',
    });

    await mountPanel();

    const warning = document.querySelector('.banner--warn');
    expect(warning).not.toBeNull();
    expect(warning.getAttribute('role')).toBe('status');
    expect(document.querySelector('.banner--error')).toBeNull();
    expect(buttonByText('Retry listening')).not.toBeNull();
  });

  it('refreshes the microphone picker after retrying a hot-plugged device', async () => {
    desktopBridge.getWakewordStatus.mockResolvedValue({
      ...baseStatus(),
      enabled: true,
      state: 'microphone_disconnected',
      error_code: 'microphone_unavailable',
    });
    desktopBridge.listMicrophones
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([
        {
          index: 7,
          name: 'Hot-plugged microphone',
          supported: true,
          default_sample_rate: 48000,
        },
      ]);

    await mountPanel();
    buttonByText('Retry listening').click();
    await settle();

    expect(desktopBridge.retryWakeword).toHaveBeenCalledOnce();
    expect(desktopBridge.listMicrophones).toHaveBeenCalledTimes(2);
    buttonByLabel('Microphone').click();
    flushSync();
    expect(buttonByText('Hot-plugged microphone')).not.toBeNull();
  });

  it('saves one server-wide transcription profile for both microphone paths', async () => {
    await mountPanel({
      settings: {
        speech: {
          transcription_audio: {
            profile: 'compatibility',
            format: 'wav',
            sample_rate_hz: 16000,
          },
        },
      },
    });

    buttonByLabel('Transcription audio').click();
    flushSync();
    buttonByText('Custom').click();
    await settle();

    expect(updateSettings).toHaveBeenLastCalledWith({
      speech: {
        transcription_audio: {
          profile: 'custom',
          format: 'wav',
          sample_rate_hz: 16000,
        },
      },
    });

    buttonByLabel('Format').click();
    flushSync();
    buttonByText('FLAC (lossless PCM16)').click();
    await settle();

    expect(updateSettings).toHaveBeenLastCalledWith({
      speech: {
        transcription_audio: {
          profile: 'custom',
          format: 'flac',
          sample_rate_hz: 16000,
        },
      },
    });
  });

  it('saves the server-specific Personal Agent immediately', async () => {
    await mountPanel();

    buttonByText('— (none)').click();
    flushSync();
    buttonByText('Main').click();
    await settle();

    expect(desktopBridge.setWakewordConfig).toHaveBeenCalledWith({
      target_agent_id: 'main',
    });
    expect(buttonByText('Save')).toBeUndefined();
  });

  it('keeps Wakeword disabled and shows the STT requirement when activation is rejected', async () => {
    desktopBridge.getWakewordStatus.mockResolvedValue({
      ...baseStatus(),
      target_agent_id: 'main',
    });
    desktopBridge.setWakewordEnabled.mockResolvedValue({
      enabled: false,
      error_code: 'speech_to_text_unconfigured',
    });
    await mountPanel();

    switchByLabel('Enable wakeword listening').click();
    await settle();

    expect(desktopBridge.setWakewordEnabled).toHaveBeenCalledWith(true);
    expect(
      switchByLabel('Enable wakeword listening').getAttribute('aria-checked'),
    ).toBe('false');
    expect(
      document.querySelector(
        '.voice-attention-banner.banner--error[role="alert"]',
      ),
    ).not.toBeNull();
  });

  it('starts with Okay Nabu and Hey Nabu active together', async () => {
    await mountPanel();

    expect(
      switchByLabel('Listen for Okay Nabu').getAttribute('aria-checked'),
    ).toBe('true');
    expect(
      switchByLabel('Listen for Hey Nabu').getAttribute('aria-checked'),
    ).toBe('true');
    expect(document.querySelectorAll('.voice-model-card--active')).toHaveLength(
      2,
    );
  });

  it('allows one active model to be disabled but never the last one', async () => {
    await mountPanel();

    switchByLabel('Listen for Hey Nabu').click();
    await settle();

    expect(desktopBridge.setWakewordConfig).toHaveBeenCalledWith({
      active_model_ids: ['builtin/okay_nabu'],
    });
    expect(switchByLabel('Listen for Okay Nabu').disabled).toBe(true);
    expect(document.querySelectorAll('.voice-model-card--active')).toHaveLength(
      1,
    );
  });

  it('saves sensitivity for the edited model without changing selection', async () => {
    await mountPanel();
    const slider = document.getElementById(
      'voice-sensitivity-builtin/okay_nabu',
    );

    slider.value = '0.8';
    slider.dispatchEvent(new Event('input', { bubbles: true }));
    slider.dispatchEvent(new Event('change', { bubbles: true }));
    await settle();

    expect(desktopBridge.setWakewordConfig).toHaveBeenCalledWith({
      model_sensitivities: {
        'builtin/okay_nabu': 0.8,
        'builtin/hey_nabu': 0.5,
      },
    });
  });

  it('applies automatically calibrated sensitivities only after confirmation', async () => {
    desktopBridge.getWakewordStatus.mockResolvedValue({
      ...baseStatus(),
      enabled: true,
      state: 'listening',
      target_agent_id: 'main',
    });
    await mountPanel();

    buttonByText('Start calibration').click();
    await settle();

    expect(desktopBridge.startWakewordCalibration).toHaveBeenCalledOnce();
    const calibrationPanel = document.querySelector('.voice-calibration-panel');
    expect(calibrationPanel).not.toBeNull();
    expect(
      calibrationPanel.querySelectorAll(
        '.voice-calibration-steps li[data-state="complete"]',
      ),
    ).toHaveLength(2);
    expect(
      calibrationPanel.querySelectorAll(
        '.voice-calibration-steps li[data-state="current"]',
      ),
    ).toHaveLength(1);
    expect(calibrationPanel.querySelector('.chip.success')).not.toBeNull();
    expect(
      calibrationPanel.querySelector('.voice-calibration-model__result')
        .textContent,
    ).toContain('65%');

    const slider = document.getElementById(
      'voice-sensitivity-builtin/okay_nabu',
    );
    expect(slider.disabled).toBe(true);
    expect(desktopBridge.setWakewordConfig).not.toHaveBeenCalled();

    buttonByText('Apply calibrated values').click();
    await waitForCondition(
      () => desktopBridge.stopWakewordCalibration.mock.calls.length === 1,
    );

    expect(desktopBridge.stopWakewordCalibration).toHaveBeenCalledOnce();
    expect(desktopBridge.setWakewordConfig).toHaveBeenCalledWith({
      model_sensitivities: {
        'builtin/okay_nabu': 0.65,
        'builtin/hey_nabu': 0.65,
      },
    });
  });

  it('guides phrase capture and can restart from room-noise measurement', async () => {
    desktopBridge.getWakewordStatus.mockResolvedValue({
      ...baseStatus(),
      enabled: true,
      state: 'listening',
      target_agent_id: 'main',
    });
    desktopBridge.startWakewordCalibration.mockResolvedValue({
      ...baseStatus(),
      enabled: true,
      state: 'listening',
      calibration: {
        active: true,
        phase: 'phrases',
        scores: {
          'builtin/okay_nabu': 0.12,
          'builtin/hey_nabu': 0.01,
        },
        peaks: {
          'builtin/okay_nabu': 0.62,
          'builtin/hey_nabu': 0.02,
        },
        noise_levels: {
          'builtin/okay_nabu': 0.03,
          'builtin/hey_nabu': 0.02,
        },
        sample_counts: {
          'builtin/okay_nabu': 1,
          'builtin/hey_nabu': 0,
        },
        required_samples: 5,
        target_model_id: 'builtin/okay_nabu',
        recommended_sensitivities: {},
        noise_seconds_remaining: 0,
        noise_high: false,
      },
    });
    await mountPanel();

    buttonByText('Start calibration').click();
    await settle();

    expect(
      document.querySelector('.voice-calibration-steps li:nth-child(2)').dataset
        .state,
    ).toBe('current');
    expect(
      document.querySelector('.voice-calibration-model--target'),
    ).not.toBeNull();
    expect(buttonByText('Apply calibrated values').disabled).toBe(true);

    buttonByText('Restart calibration').click();
    await settle();

    expect(desktopBridge.restartWakewordCalibration).toHaveBeenCalledOnce();
    expect(
      document.querySelector('.voice-calibration-steps li:first-child').dataset
        .state,
    ).toBe('current');
  });

  it('discards a calibration result without saving', async () => {
    desktopBridge.getWakewordStatus.mockResolvedValue({
      ...baseStatus(),
      enabled: true,
      state: 'listening',
      target_agent_id: 'main',
    });
    await mountPanel();
    buttonByText('Start calibration').click();
    await settle();

    buttonByText('Discard and stop').click();
    await settle();

    // Confirm the discard dialog — the dialog's confirm button is the second
    // "Discard and stop" button in the DOM (after the panel button)
    const dialog = document.querySelector('[role="dialog"]');
    expect(dialog).not.toBeNull();
    const dialogButtons = [...dialog.querySelectorAll('button')];
    const confirmButton = dialogButtons.find(
      (button) => button.textContent.trim() === 'Discard and stop',
    );
    confirmButton.click();
    await waitForCondition(
      () => desktopBridge.stopWakewordCalibration.mock.calls.length > 0,
    );
    await waitForCondition(
      () => document.querySelector('.voice-calibration-panel') === null,
    );

    expect(desktopBridge.setWakewordConfig).not.toHaveBeenCalled();
    expect(
      document.getElementById('voice-sensitivity-builtin/okay_nabu').value,
    ).toBe('0.5');
    expect(document.querySelector('.voice-calibration-panel')).toBeNull();
    expect(document.querySelector('.voice-calibration-entry')).not.toBeNull();
  });

  it('imports a TFLite model without replacing two active phrases', async () => {
    const importedModel = {
      id: 'custom/computer',
      label: 'Hey Computer',
      source: 'imported',
      format: 'tflite',
      removable: true,
      activated: false,
    };
    desktopBridge.importWakewordModel.mockResolvedValue(importedModel);
    desktopBridge.listWakewordModels
      .mockResolvedValueOnce(BUILTIN_MODELS)
      .mockResolvedValue([...BUILTIN_MODELS, importedModel]);
    await mountPanel();

    const fileInput = document.body.querySelector('input[type="file"]');
    expect(fileInput.accept).toContain('.tflite');
    Object.defineProperty(fileInput, 'files', {
      configurable: true,
      value: [new File(['model'], 'hey_computer.tflite')],
    });
    fileInput.dispatchEvent(new Event('change', { bubbles: true }));
    await waitForCondition(
      () => desktopBridge.importWakewordModel.mock.calls.length > 0,
    );

    expect(desktopBridge.importWakewordModel).toHaveBeenCalledWith(
      'hey_computer.tflite',
      'bW9kZWw=',
    );
    expect(desktopBridge.setWakewordConfig).not.toHaveBeenCalled();
  });

  it('removes only an inactive imported model after confirmation', async () => {
    const customModel = {
      id: 'custom/computer',
      label: 'Hey Computer',
      source: 'imported',
      format: 'tflite',
      removable: true,
    };
    desktopBridge.listWakewordModels.mockResolvedValue([
      ...BUILTIN_MODELS,
      customModel,
    ]);
    await mountPanel();

    buttonByText('Remove imported model').click();
    flushSync();
    const dialog = document.querySelector('[role="dialog"]');
    expect(dialog).not.toBeNull();
    expect(dialog.textContent).toContain('Hey Computer');
    buttonByText('Delete').click();
    await settle();

    expect(desktopBridge.deleteWakewordModel).toHaveBeenCalledWith(
      customModel.id,
    );
    expect(desktopBridge.setWakewordConfig).not.toHaveBeenCalled();
  });

  async function mountPanel(props = {}) {
    mountedComponent = mount(WakewordVoiceSettings, {
      target: document.body,
      props: { agents: [{ id: 'main', name: 'Main' }], ...props },
    });
    flushSync();
    await settle();
  }
});

function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function baseStatus() {
  return {
    enabled: false,
    state: 'off',
    microphone: null,
    active_model_ids: ['builtin/okay_nabu', 'builtin/hey_nabu'],
    model_sensitivities: {
      'builtin/okay_nabu': 0.5,
      'builtin/hey_nabu': 0.5,
    },
    target_agent_id: null,
    session_behavior: 'active',
    mock: false,
    mode: 'real',
    error_code: null,
    active_microphone: null,
    calibration: {
      active: false,
      phase: null,
      scores: {},
      peaks: {},
      noise_levels: {},
      sample_counts: {},
      required_samples: 5,
      target_model_id: null,
      recommended_sensitivities: {},
      noise_seconds_remaining: 0,
      noise_high: false,
    },
  };
}

function buttonByText(text) {
  return [...document.body.querySelectorAll('button')].find(
    (button) => button.textContent.trim() === text,
  );
}

function buttonByLabel(label) {
  return document.body.querySelector(`button[aria-label="${label}"]`);
}

function switchByLabel(label) {
  return document.body.querySelector(
    `button[role="switch"][aria-label="${label}"]`,
  );
}

function buttonContainingText(text) {
  return [...document.body.querySelectorAll('button')].find((button) =>
    button.textContent.includes(text),
  );
}

async function settle() {
  await tick();
  await Promise.resolve();
  flushSync();
}

async function waitForCondition(predicate) {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    flushSync();
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 0));
    await tick();
  }
  throw new Error('Condition was not met before timeout');
}
