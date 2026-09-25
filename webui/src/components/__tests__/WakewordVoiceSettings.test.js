// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { reactiveProps } from './_reactiveProps.svelte.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/desktopBridge.js', () => ({
  isDesktopAccessor: vi.fn(() => true),
  setVoiceEnabled: vi.fn(),
  updateVoiceConfig: vi.fn(),
  listMicrophones: vi.fn(),
  listWakewordModels: vi.fn(),
  importWakewordModel: vi.fn(),
  deleteWakewordModel: vi.fn(),
  retryVoice: vi.fn(),
  startVoiceCalibration: vi.fn(),
  restartVoiceCalibration: vi.fn(),
  stopVoiceCalibration: vi.fn(),
}));
vi.mock('$lib/api.js', () => ({
  updateSettings: vi.fn(),
}));

const desktopBridge = await import('$lib/desktopBridge.js');
const { updateSettings } = await import('$lib/api.js');
const { default: WakewordVoiceSettings } =
  await import('../WakewordVoiceSettings.svelte');

const NABU = 'builtin/okay_nabu';
const HEY_NABU = 'builtin/hey_nabu';
const JARVIS = 'builtin/hey_jarvis';
const LABELS = {
  [NABU]: 'Okay Nabu',
  [HEY_NABU]: 'Hey Nabu',
  [JARVIS]: 'Hey Jarvis',
};
const MODELS = [
  { id: NABU, label: 'Okay Nabu', source: 'built_in', removable: false },
  { id: HEY_NABU, label: 'Hey Nabu', source: 'built_in', removable: false },
  { id: JARVIS, label: 'Hey Jarvis', source: 'built_in', removable: false },
].map((model) => ({ ...model, format: 'tflite', overlaps: [] }));
const AGENTS = [
  { id: 'main', name: 'Main' },
  { id: 'writer', name: 'Writer' },
];
const DEFAULT_COMMAND = {
  type: 'command',
  agent_id: null,
  session_behavior: null,
};

function phrase(modelId, overrides = {}) {
  const action = overrides.action ?? DEFAULT_COMMAND;
  return {
    model_id: modelId,
    label: LABELS[modelId] ?? modelId,
    sensitivity: 0.5,
    action,
    effective:
      action.type === 'live_voice'
        ? action
        : {
            type: 'command',
            agent_id: action.agent_id ?? 'main',
            session_behavior: action.session_behavior ?? 'active',
          },
    problem: null,
    ...overrides,
  };
}

function voiceStatus(overrides = {}) {
  return {
    enabled: true,
    mode: 'real',
    state: 'listening',
    error_code: null,
    sequence: 1,
    microphone: null,
    active_microphone: null,
    echo_cancellation: { enabled: true, state: 'active' },
    default_agent_id: 'main',
    default_session_behavior: 'active',
    phrases: [phrase(NABU), phrase(HEY_NABU)],
    recording: null,
    commands: [],
    calibration: null,
    limits: {
      max_active_phrases: 2,
      min_sensitivity: 0.05,
      max_sensitivity: 0.95,
    },
    ...overrides,
  };
}

// What the Desktop does with an `updateVoiceConfig` change.
function applyChanges(status, changes) {
  let phrases = status.phrases;
  if (changes.active_model_ids)
    phrases = changes.active_model_ids.map(
      (modelId) =>
        phrases.find((entry) => entry.model_id === modelId) ?? phrase(modelId),
    );
  phrases = phrases.map((entry) => {
    let next = entry;
    const sensitivity = changes.model_sensitivities?.[entry.model_id];
    if (sensitivity !== undefined) next = { ...next, sensitivity };
    if (changes.phrase_actions && entry.model_id in changes.phrase_actions) {
      const action = changes.phrase_actions[entry.model_id];
      next = phrase(entry.model_id, {
        sensitivity: next.sensitivity,
        action:
          action?.type === 'live_voice'
            ? action
            : { ...DEFAULT_COMMAND, ...(action ?? {}) },
      });
    }
    return next;
  });
  return {
    ...status,
    sequence: status.sequence + 1,
    phrases,
    microphone:
      'microphone' in changes ? changes.microphone : status.microphone,
    echo_cancellation:
      'echo_cancellation' in changes
        ? {
            enabled: changes.echo_cancellation,
            state: changes.echo_cancellation ? 'active' : 'off',
          }
        : status.echo_cancellation,
    default_agent_id:
      'default_agent_id' in changes
        ? changes.default_agent_id
        : status.default_agent_id,
    default_session_behavior:
      changes.default_session_behavior ?? status.default_session_behavior,
  };
}

// The app-level Voice owner the panel edits against.
function createVoiceOwner(status, { available = true } = {}) {
  const owner = reactiveProps({ available, status });
  owner.adopt = vi.fn((snapshot) => {
    if (owner.status && snapshot.sequence < owner.status.sequence) return false;
    owner.status = snapshot;
    return true;
  });
  owner.refresh = vi.fn(async () => owner.status);
  return owner;
}

describe('WakewordVoiceSettings', () => {
  let mountedComponent;
  let owner;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
    vi.clearAllMocks();
    desktopBridge.isDesktopAccessor.mockReturnValue(true);
    desktopBridge.listMicrophones.mockResolvedValue([
      {
        index: 4,
        name: 'Studio microphone',
        host_api: 'WASAPI',
        supported: true,
        default_sample_rate: 48000,
      },
      {
        index: 5,
        name: 'Bluetooth hands-free',
        host_api: 'WASAPI',
        supported: false,
        default_sample_rate: 8000,
      },
    ]);
    desktopBridge.listWakewordModels.mockResolvedValue(MODELS);
    desktopBridge.updateVoiceConfig.mockImplementation(async (changes) =>
      applyChanges(owner.status, changes),
    );
    desktopBridge.setVoiceEnabled.mockImplementation(async (enabled) => ({
      enabled,
      error_code: null,
    }));
    desktopBridge.deleteWakewordModel.mockResolvedValue({ deleted: true });
    desktopBridge.retryVoice.mockResolvedValue(undefined);
    desktopBridge.stopVoiceCalibration.mockImplementation(async () => ({
      ...owner.status,
      sequence: owner.status.sequence + 1,
      calibration: null,
    }));
    updateSettings.mockImplementation(async (payload) => payload);
  });

  afterEach(async () => {
    vi.useRealTimers();
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
  });

  async function mountPanel({ status = voiceStatus(), ...props } = {}) {
    owner ??= createVoiceOwner(status);
    mountedComponent = mount(WakewordVoiceSettings, {
      target: document.body,
      props: {
        agents: AGENTS,
        wakewordAvailable: true,
        desktopVoice: owner,
        ...props,
      },
    });
    flushSync();
    await settle();
    await settle();
  }

  afterEach(() => {
    owner = null;
  });

  describe('availability', () => {
    it('explains that Voice is set up in the Desktop app', async () => {
      desktopBridge.isDesktopAccessor.mockReturnValue(false);
      await mountPanel({ desktopVoice: null, wakewordAvailable: false });

      expect(document.body.textContent).toContain(
        'Wakeword listening is configured in the vBot Desktop app.',
      );
      expect(switchByLabel('Enable wakeword listening')).toBeNull();
      expect(desktopBridge.listWakewordModels).not.toHaveBeenCalled();
    });

    it('asks to update a Desktop app with an older Voice bridge', async () => {
      owner = createVoiceOwner(null, { available: false });
      await mountPanel();

      expect(document.body.textContent).toContain(
        'Update the vBot Desktop app to use Voice with this server.',
      );
      expect(switchByLabel('Enable wakeword listening')).toBeNull();
      expect(document.querySelector('.voice-model-card')).toBeNull();
    });

    it('waits for the first status and retries loading the lists', async () => {
      vi.useFakeTimers();
      owner = createVoiceOwner(null);
      desktopBridge.listWakewordModels.mockRejectedValueOnce(
        new Error('bridge starting'),
      );
      await mountPanel();

      expect(document.body.textContent).toContain(
        'Desktop Voice status unavailable',
      );
      expect(switchByLabel('Enable wakeword listening').disabled).toBe(true);

      owner.status = voiceStatus();
      await vi.advanceTimersByTimeAsync(3000);
      await settle();

      expect(desktopBridge.listWakewordModels).toHaveBeenCalledTimes(2);
      expect(document.body.textContent).not.toContain(
        'Desktop Voice status unavailable',
      );
      expect(
        switchByLabel('Enable wakeword listening').getAttribute('aria-checked'),
      ).toBe('true');
      expect(document.querySelectorAll('.voice-model-card')).toHaveLength(3);
    });
  });

  describe('listening', () => {
    it('enables Voice and reads the status again', async () => {
      await mountPanel({
        status: voiceStatus({ enabled: false, state: 'off' }),
      });

      switchByLabel('Enable wakeword listening').click();
      await settle();

      expect(desktopBridge.setVoiceEnabled).toHaveBeenCalledWith(true);
      expect(owner.refresh).toHaveBeenCalledOnce();
    });

    it('keeps Voice off and explains why the Desktop refused', async () => {
      desktopBridge.setVoiceEnabled.mockResolvedValue({
        enabled: false,
        error_code: 'speech_to_text_unconfigured',
      });
      await mountPanel({
        status: voiceStatus({ enabled: false, state: 'off' }),
      });

      switchByLabel('Enable wakeword listening').click();
      await settle();

      expect(
        switchByLabel('Enable wakeword listening').getAttribute('aria-checked'),
      ).toBe('false');
      const banner = document.querySelector(
        '.voice-attention-banner.banner--error[role="alert"]',
      );
      expect(banner.textContent).toContain(
        'Configure a Speech-to-text Model under Settings → Voice',
      );
    });

    it('shows a warning and retries a disconnected microphone', async () => {
      desktopBridge.listMicrophones
        .mockResolvedValueOnce([])
        .mockResolvedValue([
          {
            index: 7,
            name: 'Hot-plugged microphone',
            supported: true,
            default_sample_rate: 48000,
          },
        ]);
      await mountPanel({
        status: voiceStatus({
          state: 'microphone_disconnected',
          error_code: 'microphone_read_failed',
        }),
      });

      const warning = document.querySelector('.banner--warn');
      expect(warning.getAttribute('role')).toBe('status');
      expect(document.querySelector('.banner--error')).toBeNull();

      buttonByText('Retry listening').click();
      await settle();

      expect(desktopBridge.retryVoice).toHaveBeenCalledOnce();
      expect(owner.refresh).toHaveBeenCalledOnce();
      buttonByLabel('Microphone').click();
      flushSync();
      expect(option('Hot-plugged microphone')).not.toBeUndefined();
    });

    it('shows a Voice failure with a retry', async () => {
      await mountPanel({
        status: voiceStatus({ state: 'error', error_code: 'pipeline_failed' }),
      });

      const banner = document.querySelector('.banner--error[role="alert"]');
      expect(banner.textContent).toContain(
        'The Voice pipeline stopped unexpectedly.',
      );
      expect(buttonByText('Retry listening')).not.toBeUndefined();
    });

    it('explains a Desktop without the Voice components', async () => {
      await mountPanel({
        status: voiceStatus({
          enabled: false,
          state: 'off',
          mode: 'unavailable',
        }),
      });

      expect(document.body.textContent).toContain(
        'The Desktop Voice components are unavailable.',
      );
      expect(buttonByText('Retry listening')).toBeUndefined();
      expect(switchByLabel('Enable wakeword listening').disabled).toBe(true);
    });
  });

  describe('wake phrases', () => {
    it('bounds the active phrases by the Desktop limit', async () => {
      await mountPanel();

      expect(document.body.textContent).toContain('2 of 2 phrases active');
      expect(switchByLabel('Listen for Hey Jarvis').disabled).toBe(true);

      switchByLabel('Listen for Hey Nabu').click();
      await settle();
      expect(desktopBridge.updateVoiceConfig).toHaveBeenLastCalledWith({
        active_model_ids: [NABU],
      });
      // The last active phrase stays.
      expect(switchByLabel('Listen for Okay Nabu').disabled).toBe(true);
      expect(switchByLabel('Listen for Hey Jarvis').disabled).toBe(false);

      switchByLabel('Listen for Hey Jarvis').click();
      await settle();
      expect(desktopBridge.updateVoiceConfig).toHaveBeenLastCalledWith({
        active_model_ids: [NABU, JARVIS],
      });
      expect(
        document.querySelectorAll('.voice-model-card--active'),
      ).toHaveLength(2);
    });

    it('offers no activation or sensitivity the Desktop reports no limits for', async () => {
      await mountPanel({
        status: voiceStatus({
          limits: {
            max_active_phrases: null,
            min_sensitivity: null,
            max_sensitivity: null,
          },
        }),
      });

      expect(switchByLabel('Listen for Hey Jarvis').disabled).toBe(true);
      expect(document.body.textContent).not.toContain('phrases active');
      expect(slider(NABU).disabled).toBe(true);
    });

    it('saves the sensitivity of one phrase within the Desktop limits', async () => {
      await mountPanel();

      expect(slider(NABU).min).toBe('0.05');
      expect(slider(NABU).max).toBe('0.95');
      setSlider(NABU, '0.8');
      await settle();

      expect(desktopBridge.updateVoiceConfig).toHaveBeenCalledExactlyOnceWith({
        model_sensitivities: { [NABU]: 0.8 },
      });
      expect(owner.adopt).toHaveBeenCalled();
      expect(document.querySelector('.voice-save-state').textContent).toContain(
        'Saved',
      );
    });

    it('sends a phrase to its own Agent and Session', async () => {
      await mountPanel();

      expect(buttonByLabel('Agent for Okay Nabu').textContent).toContain(
        'Default Agent',
      );
      await choose('Agent for Okay Nabu', 'Writer');
      expect(desktopBridge.updateVoiceConfig).toHaveBeenLastCalledWith({
        phrase_actions: { [NABU]: { type: 'command', agent_id: 'writer' } },
      });

      await choose('Session for Okay Nabu', 'New Session each time');
      expect(desktopBridge.updateVoiceConfig).toHaveBeenLastCalledWith({
        phrase_actions: {
          [NABU]: {
            type: 'command',
            agent_id: 'writer',
            session_behavior: 'new',
          },
        },
      });

      await choose('Agent for Okay Nabu', 'Default Agent');
      await choose('Session for Okay Nabu', 'Default Session behavior');
      expect(desktopBridge.updateVoiceConfig).toHaveBeenLastCalledWith({
        phrase_actions: { [NABU]: null },
      });
    });

    it('marks a phrase Agent that is not on this server', async () => {
      await mountPanel({
        status: voiceStatus({
          phrases: [
            phrase(NABU, {
              action: { ...DEFAULT_COMMAND, agent_id: 'retired' },
              problem: 'target_agent_unavailable',
            }),
            phrase(HEY_NABU),
          ],
        }),
      });

      const card = phraseCard(NABU);
      expect(buttonByLabel('Agent for Okay Nabu').textContent).toContain(
        'retired',
      );
      expect(card.querySelector('.badge--warn').textContent).toContain(
        'Not ready',
      );
      expect(card.textContent).toContain(
        'The chosen Agent no longer exists on this server.',
      );
    });

    it('lets a phrase start Live voice instead of sending a command', async () => {
      await mountPanel();

      expect(buttonByLabel('When Hey Nabu is heard').textContent).toContain(
        'Send a command',
      );
      await choose('When Hey Nabu is heard', 'Start Live voice');
      expect(desktopBridge.updateVoiceConfig).toHaveBeenLastCalledWith({
        phrase_actions: { [HEY_NABU]: { type: 'live_voice', mode: 'start' } },
      });
      expect(buttonByLabel('Agent for Hey Nabu')).toBeNull();

      await choose('When Hey Nabu is heard', 'Start or end Live voice');
      expect(desktopBridge.updateVoiceConfig).toHaveBeenLastCalledWith({
        phrase_actions: { [HEY_NABU]: { type: 'live_voice', mode: 'toggle' } },
      });

      await choose('When Hey Nabu is heard', 'Send a command');
      expect(desktopBridge.updateVoiceConfig).toHaveBeenLastCalledWith({
        phrase_actions: { [HEY_NABU]: null },
      });
      expect(buttonByLabel('Agent for Hey Nabu')).not.toBeNull();
    });

    it('warns about overlapping phrases that do different things', async () => {
      desktopBridge.listWakewordModels.mockResolvedValue(
        MODELS.map((model) =>
          model.id === NABU ? { ...model, overlaps: [HEY_NABU] } : model,
        ),
      );
      await mountPanel({
        status: voiceStatus({
          phrases: [
            phrase(NABU),
            phrase(HEY_NABU, {
              action: { type: 'live_voice', mode: 'toggle' },
            }),
          ],
        }),
      });

      expect(phraseCard(NABU).textContent).toContain(
        '“Okay Nabu” can also be heard as “Hey Nabu”, which does something else.',
      );
      expect(phraseCard(HEY_NABU).textContent).toContain(
        '“Hey Nabu” can also be heard as “Okay Nabu”',
      );

      await choose('When Hey Nabu is heard', 'Send a command');
      expect(
        document.querySelector('.voice-model-card__notice--warn'),
      ).toBeNull();
    });
  });

  describe('defaults and capture', () => {
    it('saves the default Agent and Session behavior for this server', async () => {
      await mountPanel();

      expect(buttonByLabel('Default Agent').textContent).toContain('Main');
      await choose('Default Agent', 'Writer');
      expect(desktopBridge.updateVoiceConfig).toHaveBeenLastCalledWith({
        default_agent_id: 'writer',
      });

      await choose('Default Session behavior', 'New Session each time');
      expect(desktopBridge.updateVoiceConfig).toHaveBeenLastCalledWith({
        default_session_behavior: 'new',
      });

      await choose('Default Agent', 'None');
      expect(desktopBridge.updateVoiceConfig).toHaveBeenLastCalledWith({
        default_agent_id: null,
      });
    });

    it('saves a stable microphone descriptor instead of a device index', async () => {
      await mountPanel();

      buttonByLabel('Microphone').click();
      flushSync();
      expect(option('Bluetooth hands-free').disabled).toBe(true);
      option('Studio microphone').click();
      await settle();

      expect(desktopBridge.updateVoiceConfig).toHaveBeenCalledWith({
        microphone: {
          index: 4,
          name: 'Studio microphone',
          host_api: 'WASAPI',
        },
      });
    });

    it.each([
      [
        'active',
        'Active',
        'Speaker output is removed from the microphone signal',
      ],
      [
        'no_reference',
        'No speaker signal',
        'cannot capture the speaker output',
      ],
      ['unavailable', 'Unavailable', 'Echo cancellation is not installed'],
    ])(
      'explains the echo cancellation state %s',
      async (state, chip, detail) => {
        await mountPanel({
          status: voiceStatus({ echo_cancellation: { enabled: true, state } }),
        });

        const control = document.querySelector('.voice-echo-control');
        expect(control.querySelector('.chip').textContent).toContain(chip);
        expect(control.closest('.s-row').textContent).toContain(detail);
      },
    );

    it('turns echo cancellation off', async () => {
      await mountPanel();

      switchByLabel('Use echo cancellation').click();
      await settle();

      expect(desktopBridge.updateVoiceConfig).toHaveBeenCalledWith({
        echo_cancellation: false,
      });
      const row = document
        .querySelector('.voice-echo-control')
        .closest('.s-row');
      expect(row.querySelector('.chip').textContent).toContain('Off');
      expect(row.textContent).toContain(
        'Speaker output can trigger wake phrases',
      );
    });

    it('keeps unsaved edits when the Desktop pushes a newer status', async () => {
      await mountPanel();

      slider(NABU).value = '0.8';
      slider(NABU).dispatchEvent(new Event('input', { bubbles: true }));
      flushSync();
      owner.status = voiceStatus({ sequence: 5, default_agent_id: 'writer' });
      flushSync();

      expect(slider(NABU).value).toBe('0.8');
      expect(buttonByLabel('Default Agent').textContent).toContain('Writer');
    });

    it('keeps the edit and reports a save the Desktop rejected', async () => {
      const onToast = vi.fn();
      desktopBridge.updateVoiceConfig.mockRejectedValue(
        new Error('voice_config_invalid'),
      );
      await mountPanel({ onToast });

      switchByLabel('Listen for Hey Nabu').click();
      await settle();

      expect(onToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'error' }),
      );
      expect(document.querySelector('.voice-save-state').textContent).toContain(
        'Not saved',
      );
      expect(
        switchByLabel('Listen for Hey Nabu').getAttribute('aria-checked'),
      ).toBe('false');
    });
  });

  describe('calibration', () => {
    function calibration(overrides = {}) {
      return {
        model_id: NABU,
        phase: 'ready',
        score: 0.3,
        peak: 0.7,
        noise_level: 0.03,
        noise_high: false,
        sample_count: 3,
        required_samples: 3,
        recommended_sensitivity: 0.65,
        noise_seconds_remaining: 0,
        ...overrides,
      };
    }

    function startWith(calibrationState) {
      desktopBridge.startVoiceCalibration.mockImplementation(async () => ({
        ...owner.status,
        sequence: owner.status.sequence + 1,
        calibration: calibrationState,
      }));
    }

    it('calibrates one active phrase and saves the result after confirmation', async () => {
      const onToast = vi.fn();
      startWith(calibration());
      await mountPanel({ onToast });

      expect(buttonByLabel('Calibrate Hey Jarvis')).toBeNull();
      buttonByLabel('Calibrate Okay Nabu').click();
      await settle();

      expect(desktopBridge.startVoiceCalibration).toHaveBeenCalledWith(NABU);
      const panel = phraseCard(NABU).querySelector('.voice-calibration-panel');
      expect(panel.textContent).toContain('Calibrating “Okay Nabu”');
      expect(panel.textContent).toContain('Recommended sensitivity 65%');
      expect(buttonByLabel('Calibrate Hey Nabu').disabled).toBe(true);
      expect(slider(NABU).disabled).toBe(true);
      expect(desktopBridge.updateVoiceConfig).not.toHaveBeenCalled();

      buttonByText('Apply calibrated value').click();
      await waitForCondition(
        () => desktopBridge.updateVoiceConfig.mock.calls.length === 1,
      );

      expect(
        desktopBridge.stopVoiceCalibration.mock.invocationCallOrder[0],
      ).toBeLessThan(
        desktopBridge.updateVoiceConfig.mock.invocationCallOrder[0],
      );
      expect(desktopBridge.updateVoiceConfig).toHaveBeenCalledWith({
        model_sensitivities: { [NABU]: 0.65 },
      });
      await waitForCondition(() =>
        onToast.mock.calls.some(([toast]) => toast.variant === 'success'),
      );
      expect(document.querySelector('.voice-calibration-panel')).toBeNull();
    });

    it('calibrates only while listening', async () => {
      await mountPanel({
        status: voiceStatus({ enabled: false, state: 'off' }),
      });

      expect(buttonByLabel('Calibrate Okay Nabu').disabled).toBe(true);
    });

    it('restarts and discards a calibration without saving', async () => {
      startWith(calibration({ phase: 'phrases', sample_count: 1 }));
      desktopBridge.restartVoiceCalibration.mockImplementation(async () => ({
        ...owner.status,
        sequence: owner.status.sequence + 1,
        calibration: calibration({
          phase: 'noise',
          sample_count: 0,
          recommended_sensitivity: null,
          noise_seconds_remaining: 3,
        }),
      }));
      await mountPanel();
      buttonByLabel('Calibrate Okay Nabu').click();
      await settle();

      expect(buttonByText('Apply calibrated value').disabled).toBe(true);
      expect(
        document.querySelector('.voice-calibration-steps li:nth-child(2)')
          .dataset.state,
      ).toBe('current');

      buttonByText('Restart calibration').click();
      await settle();
      expect(desktopBridge.restartVoiceCalibration).toHaveBeenCalledOnce();
      expect(
        document.querySelector('.voice-calibration-steps li:first-child')
          .dataset.state,
      ).toBe('current');

      buttonByText('Discard and stop').click();
      flushSync();
      const dialog = document.querySelector('[role="dialog"]');
      [...dialog.querySelectorAll('button')]
        .find((button) => button.textContent.trim() === 'Discard and stop')
        .click();
      await waitForCondition(
        () => document.querySelector('.voice-calibration-panel') === null,
      );

      expect(desktopBridge.stopVoiceCalibration).toHaveBeenCalledOnce();
      expect(desktopBridge.updateVoiceConfig).not.toHaveBeenCalled();
      expect(slider(NABU).value).toBe('0.5');
    });

    it('ends a running calibration when the panel closes', async () => {
      await mountPanel({ status: voiceStatus({ calibration: calibration() }) });

      await unmount(mountedComponent);
      mountedComponent = null;

      expect(desktopBridge.stopVoiceCalibration).toHaveBeenCalledOnce();
    });
  });

  describe('models', () => {
    it('imports a TFLite model without activating it', async () => {
      const onToast = vi.fn();
      const importedModel = {
        id: 'custom/computer',
        label: 'Hey Computer',
        source: 'imported',
        format: 'tflite',
        removable: true,
        overlaps: [],
      };
      desktopBridge.importWakewordModel.mockResolvedValue({
        ...importedModel,
        activated: false,
      });
      desktopBridge.listWakewordModels
        .mockResolvedValueOnce(MODELS)
        .mockResolvedValue([...MODELS, importedModel]);
      await mountPanel({ onToast });

      const fileInput = document.body.querySelector('input[type="file"]');
      expect(fileInput.accept).toContain('.tflite');
      Object.defineProperty(fileInput, 'files', {
        configurable: true,
        value: [new File(['model'], 'hey_computer.tflite')],
      });
      fileInput.dispatchEvent(new Event('change', { bubbles: true }));
      await waitForCondition(() => onToast.mock.calls.length > 0);

      expect(desktopBridge.importWakewordModel).toHaveBeenCalledWith(
        'hey_computer.tflite',
        'bW9kZWw=',
      );
      expect(onToast).toHaveBeenCalledWith({
        title: 'Wakeword model imported. Activate it to listen for it.',
        variant: 'success',
      });
      expect(owner.refresh).toHaveBeenCalledOnce();
      expect(switchByLabel('Listen for Hey Computer')).not.toBeNull();
      expect(desktopBridge.updateVoiceConfig).not.toHaveBeenCalled();
    });

    it('rejects oversized model files before reading or bridging them', async () => {
      const onToast = vi.fn();
      await mountPanel({ onToast });
      const fileInput = document.body.querySelector('input[type="file"]');
      const file = new File(['model'], 'too_large.tflite');
      Object.defineProperty(file, 'size', { value: 20 * 1024 * 1024 + 1 });
      Object.defineProperty(fileInput, 'files', {
        configurable: true,
        value: [file],
      });

      fileInput.dispatchEvent(new Event('change', { bubbles: true }));
      await settle();

      expect(desktopBridge.importWakewordModel).not.toHaveBeenCalled();
      expect(onToast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: 'Wakeword model is too large.',
          variant: 'error',
        }),
      );
    });

    it('removes only an inactive imported model after confirmation', async () => {
      const customModel = {
        id: 'custom/computer',
        label: 'Hey Computer',
        source: 'imported',
        format: 'tflite',
        removable: true,
        overlaps: [],
      };
      desktopBridge.listWakewordModels.mockResolvedValue([
        ...MODELS,
        customModel,
      ]);
      await mountPanel();

      // Built-in phrases cannot be removed.
      expect(
        [...document.querySelectorAll('button')].filter(
          (button) => button.textContent.trim() === 'Remove imported model',
        ),
      ).toHaveLength(1);
      buttonByText('Remove imported model').click();
      flushSync();
      const dialog = document.querySelector('[role="dialog"]');
      expect(dialog.textContent).toContain('Hey Computer');
      buttonByText('Delete').click();
      await settle();

      expect(desktopBridge.deleteWakewordModel).toHaveBeenCalledWith(
        customModel.id,
      );
      expect(desktopBridge.updateVoiceConfig).not.toHaveBeenCalled();
    });
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
    option('Custom').click();
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
  });
});

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

function option(label) {
  return [...document.body.querySelectorAll('[role="option"]')].find(
    (element) =>
      element
        .querySelector('.dropdown-primitive__option-label')
        ?.textContent.trim() === label,
  );
}

async function choose(dropdownLabel, optionLabel) {
  buttonByLabel(dropdownLabel).click();
  flushSync();
  option(optionLabel).click();
  await settle();
}

function phraseCard(modelId) {
  return document.querySelector(
    `.voice-model-card[data-model-id="${modelId}"]`,
  );
}

function slider(modelId) {
  return document.getElementById(`voice-sensitivity-${modelId}`);
}

function setSlider(modelId, value) {
  const input = slider(modelId);
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
}

async function settle() {
  await tick();
  await Promise.resolve();
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
