// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';

import { init, t } from '../../lib/i18n.js';
import { errorMessage } from '../voice/voiceLabels.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/desktopBridge.js', async (importOriginal) => ({
  desktopErrorCode: (await importOriginal()).desktopErrorCode,
  isDesktopAccessor: vi.fn(() => true),
  setVoiceEnabled: vi.fn(),
  updateVoiceConfig: vi.fn(),
  listWakewordModels: vi.fn(),
  importWakewordModel: vi.fn(),
  deleteWakewordModel: vi.fn(),
  retryVoice: vi.fn(),
  startVoiceCalibration: vi.fn(),
  restartVoiceCalibration: vi.fn(),
  stopVoiceCalibration: vi.fn(),
}));
const desktopBridge = await import('$lib/desktopBridge.js');
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
  });

  afterEach(async () => {
    vi.useRealTimers();
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    owner = null;
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

  describe('availability', () => {
    it.each([
      [
        'outside the Desktop app',
        'settings.voice.desktopOnly',
        () => {
          desktopBridge.isDesktopAccessor.mockReturnValue(false);
          return { desktopVoice: null, wakewordAvailable: false };
        },
      ],
      [
        'in a Desktop app with an older Voice bridge',
        'settings.voice.desktopUpdateRequired',
        () => {
          owner = createVoiceOwner(null, { available: false });
          return {};
        },
      ],
    ])('explains why Voice cannot be set up %s', async (_label, key, setup) => {
      await mountPanel(setup());

      expect(document.body.textContent).toContain(t(key));
      expect(switchByLabel('Enable wakeword listening')).toBeNull();
      expect(document.querySelector('.voice-model-card')).toBeNull();
      expect(desktopBridge.listWakewordModels).not.toHaveBeenCalled();
    });

    it('waits for the first status and retries loading the lists', async () => {
      vi.useFakeTimers();
      owner = createVoiceOwner(null);
      desktopBridge.listWakewordModels.mockRejectedValueOnce(
        new Error('bridge starting'),
      );
      await mountPanel();

      expect(document.body.textContent).toContain(
        t('settings.voice.statusUnavailableTitle'),
      );
      expect(switchByLabel('Enable wakeword listening').disabled).toBe(true);

      owner.status = voiceStatus();
      await vi.advanceTimersByTimeAsync(3000);
      await settle();

      expect(desktopBridge.listWakewordModels).toHaveBeenCalledTimes(2);
      expect(document.body.textContent).not.toContain(
        t('settings.voice.statusUnavailableTitle'),
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
        errorMessage('speech_to_text_unconfigured'),
      );
    });

    it('shows a warning and retries a disconnected microphone', async () => {
      await mountPanel({
        status: voiceStatus({
          state: 'microphone_disconnected',
          error_code: 'microphone_read_failed',
        }),
      });

      const warning = document.querySelector('.banner--warn');
      expect(warning.getAttribute('role')).toBe('status');
      expect(document.querySelector('.banner--error')).toBeNull();

      buttonByText(retryLabel()).click();
      await settle();

      expect(desktopBridge.retryVoice).toHaveBeenCalledOnce();
      expect(owner.refresh).toHaveBeenCalledOnce();
    });

    it('shows a Voice failure with a retry', async () => {
      await mountPanel({
        status: voiceStatus({ state: 'error', error_code: 'pipeline_failed' }),
      });

      const banner = document.querySelector('.banner--error[role="alert"]');
      expect(banner.textContent).toContain(errorMessage('pipeline_failed'));
      expect(buttonByText(retryLabel())).not.toBeUndefined();
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
        errorMessage('voice_stack_unavailable'),
      );
      expect(buttonByText(retryLabel())).toBeUndefined();
      expect(switchByLabel('Enable wakeword listening').disabled).toBe(true);
    });
  });

  describe('wake phrases', () => {
    it('bounds the active phrases by the Desktop limit', async () => {
      await mountPanel();

      expect(document.body.textContent).toContain(
        t('settings.voice.phraseLimit', { count: 2, max: 2 }),
      );
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
      expect(document.body.textContent).not.toContain(
        t('settings.voice.phraseLimit', { count: 2, max: 2 }),
      );
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
      expect(
        document.querySelector('.save-status [role="status"]').textContent,
      ).toContain(t('common.saved'));
    });

    it('sends a phrase to its own Agent and Session', async () => {
      await mountPanel();

      expect(buttonByLabel('Agent for Okay Nabu').textContent).toContain(
        t('settings.voice.agentDefault'),
      );
      await choose('Agent for Okay Nabu', 'Writer');
      expect(desktopBridge.updateVoiceConfig).toHaveBeenLastCalledWith({
        phrase_actions: { [NABU]: { type: 'command', agent_id: 'writer' } },
      });

      await choose(
        'Session for Okay Nabu',
        t('settings.voice.sessionBehaviorNew'),
      );
      expect(desktopBridge.updateVoiceConfig).toHaveBeenLastCalledWith({
        phrase_actions: {
          [NABU]: {
            type: 'command',
            agent_id: 'writer',
            session_behavior: 'new',
          },
        },
      });

      await choose('Agent for Okay Nabu', t('settings.voice.agentDefault'));
      await choose('Session for Okay Nabu', t('settings.voice.sessionDefault'));
      expect(desktopBridge.updateVoiceConfig).toHaveBeenLastCalledWith({
        phrase_actions: { [NABU]: null },
      });
    });

    it('lets an active phrase whose model is gone be deactivated unless it is the last one', async () => {
      const GONE = 'custom/gone';
      await mountPanel({
        status: voiceStatus({
          phrases: [
            phrase(GONE, {
              sensitivity: 0.7,
              action: { type: 'live_voice', mode: 'start' },
            }),
            phrase(NABU),
          ],
        }),
      });

      const card = phraseCard(GONE);
      expect(card.querySelector('.voice-model-card__name').textContent).toBe(
        GONE,
      );
      expect(card.querySelector('.badge--warn').textContent).toContain(
        t('settings.voice.phraseUnavailable'),
      );
      expect(card.textContent).toContain(
        errorMessage('wakeword_model_unavailable'),
      );
      expect(document.querySelectorAll('.voice-model-card')).toHaveLength(4);

      buttonByLabel(`Stop listening for ${GONE}`).click();
      await settle();

      expect(desktopBridge.updateVoiceConfig).toHaveBeenCalledExactlyOnceWith({
        active_model_ids: [NABU],
      });
      expect(phraseCard(GONE)).toBeNull();

      owner.status = voiceStatus({ sequence: 10, phrases: [phrase(GONE)] });
      flushSync();
      expect(buttonByLabel(`Stop listening for ${GONE}`).disabled).toBe(true);
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
        t('settings.voice.phraseNotReady'),
      );
      expect(card.textContent).toContain(
        errorMessage('target_agent_unavailable'),
      );
    });

    it('lets a phrase start Live voice instead of sending a command', async () => {
      await mountPanel();

      expect(buttonByLabel('When Hey Nabu is heard').textContent).toContain(
        t('settings.voice.actionCommand'),
      );
      await choose(
        'When Hey Nabu is heard',
        t('settings.voice.actionLiveStart'),
      );
      expect(desktopBridge.updateVoiceConfig).toHaveBeenLastCalledWith({
        phrase_actions: { [HEY_NABU]: { type: 'live_voice', mode: 'start' } },
      });
      expect(buttonByLabel('Agent for Hey Nabu')).toBeNull();

      await choose(
        'When Hey Nabu is heard',
        t('settings.voice.actionLiveToggle'),
      );
      expect(desktopBridge.updateVoiceConfig).toHaveBeenLastCalledWith({
        phrase_actions: { [HEY_NABU]: { type: 'live_voice', mode: 'toggle' } },
      });

      await choose('When Hey Nabu is heard', t('settings.voice.actionCommand'));
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
        t('settings.voice.overlapWarning', {
          name: 'Okay Nabu',
          others: '“Hey Nabu”',
        }),
      );
      expect(phraseCard(HEY_NABU).textContent).toContain(
        t('settings.voice.overlapWarning', {
          name: 'Hey Nabu',
          others: '“Okay Nabu”',
        }),
      );

      await choose('When Hey Nabu is heard', t('settings.voice.actionCommand'));
      expect(
        document.querySelector('.voice-model-card__notice--warn'),
      ).toBeNull();
    });
  });

  describe('defaults and audio input', () => {
    it('saves the default Agent and Session behavior for this server', async () => {
      await mountPanel();

      expect(buttonByLabel('Default Agent').textContent).toContain('Main');
      await choose('Default Agent', 'Writer');
      expect(desktopBridge.updateVoiceConfig).toHaveBeenLastCalledWith({
        default_agent_id: 'writer',
      });

      await choose(
        'Default Session behavior',
        t('settings.voice.sessionBehaviorNew'),
      );
      expect(desktopBridge.updateVoiceConfig).toHaveBeenLastCalledWith({
        default_session_behavior: 'new',
      });

      await choose('Default Agent', t('settings.voice.noDefaultAgent'));
      expect(desktopBridge.updateVoiceConfig).toHaveBeenLastCalledWith({
        default_agent_id: null,
      });
    });

    it('shows the microphone the running capture uses', async () => {
      await mountPanel();
      expect(document.querySelector('.voice-active-microphone')).toBeNull();

      owner.status = voiceStatus({
        sequence: 2,
        active_microphone: {
          index: 4,
          name: 'Studio microphone',
          host_api: 'Windows WASAPI',
          sample_rate: 48000,
        },
      });
      flushSync();

      expect(
        document.querySelector('.voice-active-microphone').textContent.trim(),
      ).toBe('Studio microphone');
    });

    it.each([
      [{ enabled: true, state: 'starting' }, 'Starting', true],
      [{ enabled: true, state: 'active' }, 'Active', false],
      [{ enabled: true, state: 'no_reference' }, 'NoReference', true],
      [{ enabled: true, state: 'unavailable' }, 'Unavailable', true],
      // Turned off in the shared microphone settings.
      [{ enabled: false, state: 'off' }, 'Off', true],
    ])('shows the echo cancellation state %o', async (echo, key, explained) => {
      await mountPanel({
        status: voiceStatus({ echo_cancellation: echo }),
      });

      const control = document.querySelector('.voice-echo-control');
      expect(control.querySelector('.chip').textContent).toContain(
        t(`settings.voice.echo${key}`),
      );
      // Only a state that lets speaker output through is explained.
      const detail = control.closest('.s-row').querySelector('.s-row-desc');
      if (explained) {
        expect(detail.textContent).toContain(
          t(`settings.voice.echo${key}Detail`),
        );
      } else {
        expect(detail).toBeNull();
      }
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

    it.each([
      [
        'with an error code by its explanation',
        new Error('voice_config_invalid'),
        () => errorMessage('voice_config_invalid'),
      ],
      [
        'without an error code by its own message',
        new Error('Desktop bridge not available'),
        () => 'Desktop bridge not available',
      ],
    ])(
      'keeps the edit and reports a rejected save %s',
      async (_label, error, message) => {
        const onToast = vi.fn();
        desktopBridge.updateVoiceConfig.mockRejectedValue(error);
        await mountPanel({ onToast });

        switchByLabel('Listen for Hey Nabu').click();
        await settle();

        expect(onToast).toHaveBeenCalledWith({
          title: t('errors.generic'),
          message: message(),
          variant: 'error',
        });
        // The unsaved edit offers Save again as its retry.
        expect(
          document.querySelector('.save-status button').textContent.trim(),
        ).toBe(t('common.save'));
        expect(
          switchByLabel('Listen for Hey Nabu').getAttribute('aria-checked'),
        ).toBe('false');
      },
    );
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
      expect(panel.textContent).toContain(
        t('settings.voice.calibrationHeading', { name: 'Okay Nabu' }),
      );
      expect(panel.textContent).toContain(
        t('settings.voice.calibrationRecommendation', { value: 65 }),
      );
      expect(buttonByLabel('Calibrate Hey Nabu').disabled).toBe(true);
      expect(slider(NABU).disabled).toBe(true);
      expect(desktopBridge.updateVoiceConfig).not.toHaveBeenCalled();

      buttonByText(t('settings.voice.calibrationApply')).click();
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

      expect(buttonByText(t('settings.voice.calibrationApply')).disabled).toBe(
        true,
      );
      expect(
        document.querySelector('.voice-calibration-steps li:nth-child(2)')
          .dataset.state,
      ).toBe('current');

      buttonByText(t('settings.voice.calibrationReset')).click();
      await settle();
      expect(desktopBridge.restartVoiceCalibration).toHaveBeenCalledOnce();
      expect(
        document.querySelector('.voice-calibration-steps li:first-child')
          .dataset.state,
      ).toBe('current');

      const discard = t('settings.voice.calibrationDiscard');
      buttonByText(discard).click();
      flushSync();
      const dialog = document.querySelector('[role="dialog"]');
      [...dialog.querySelectorAll('button')]
        .find((button) => button.textContent.trim() === discard)
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
        title: t('settings.voice.importSuccessInactive'),
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
          title: t('settings.voice.importTooLargeTitle'),
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
      const remove = t('settings.voice.removeModel');
      expect(
        [...document.querySelectorAll('button')].filter(
          (button) => button.textContent.trim() === remove,
        ),
      ).toHaveLength(1);
      buttonByText(remove).click();
      flushSync();
      const dialog = document.querySelector('[role="dialog"]');
      expect(dialog.textContent).toContain('Hey Computer');
      buttonByText(t('common.delete')).click();
      await settle();

      expect(desktopBridge.deleteWakewordModel).toHaveBeenCalledWith(
        customModel.id,
      );
      expect(desktopBridge.updateVoiceConfig).not.toHaveBeenCalled();
    });
  });
});

// The English catalog has no entry for this label yet.
const retryLabel = () => t('settings.voice.retry');

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
