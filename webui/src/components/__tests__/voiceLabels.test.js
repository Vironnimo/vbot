import { beforeEach, describe, expect, it } from 'vitest';

import { init, t } from '../../lib/i18n.js';
import {
  bridgeErrorMessage,
  commandFailureMessage,
  errorMessage,
  voiceIndicator,
} from '../voice/voiceLabels.js';

// Every code a Desktop bridge call can reject with.
const BRIDGE_ERROR_CODES = [
  'voice_config_invalid',
  'no_server',
  'wakeword_model_invalid',
  'wakeword_model_unavailable',
  'wakeword_model_active',
  'wakeword_model_delete_failed',
  'calibration_unavailable',
  'calibration_inactive',
];

beforeEach(() => {
  init('en');
});

function voiceStatus(overrides = {}) {
  return {
    enabled: true,
    state: 'listening',
    sequence: 1,
    recording: null,
    commands: [],
    ...overrides,
  };
}

const RECORDING = { command_id: 'c-1' };
const command = (stage) => ({ command_id: 'c-1', model_id: null, stage });

describe('voiceIndicator', () => {
  // Priority: an error, a running recording, a lost microphone, commands in
  // flight, starting, listening; no status or a disabled Voice is off.
  it.each([
    ['no status yet', null, 'off', 'off', 'off'],
    ['disabled Voice', voiceStatus({ enabled: false }), 'off', 'off', 'off'],
    ['the off state', voiceStatus({ state: 'off' }), 'off', 'off', 'off'],
    ['an error', voiceStatus({ state: 'error' }), 'error', 'error', 'error'],
    [
      'an error during a recording',
      voiceStatus({ state: 'error', recording: RECORDING }),
      'error',
      'error',
      'error',
    ],
    [
      'a recording',
      voiceStatus({
        state: 'microphone_disconnected',
        recording: RECORDING,
        commands: [command('sending')],
      }),
      'recording',
      'recording',
      'recording',
    ],
    [
      'a lost microphone',
      voiceStatus({
        state: 'microphone_disconnected',
        commands: [command('sending')],
      }),
      'warning',
      'microphone_disconnected',
      'microphoneDisconnected',
    ],
    [
      'the last command being transcribed',
      voiceStatus({ commands: [command('sending'), command('transcribing')] }),
      'processing',
      'transcribing',
      'processing',
    ],
    [
      'a command being sent',
      voiceStatus({ state: 'starting', commands: [command('sending')] }),
      'processing',
      'sending',
      'processing',
    ],
    [
      'a command in an unknown stage',
      voiceStatus({ commands: [command(null)] }),
      'processing',
      'processing',
      'processing',
    ],
    [
      'starting',
      voiceStatus({ state: 'starting' }),
      'processing',
      'starting',
      'starting',
    ],
    ['listening', voiceStatus(), 'listening', 'listening', 'listening'],
  ])('shows %s', (_label, status, tone, stateKey, tooltipKey) => {
    expect(voiceIndicator(status)).toEqual({
      tone,
      label: t(`voice.state.${stateKey}`),
      tooltip: t(`voice.mic.tooltip.${tooltipKey}`),
      recording: tone === 'recording',
    });
  });
});

describe('bridgeErrorMessage', () => {
  it('explains every bridge error code with its own message', () => {
    const generic = errorMessage('not_a_known_code');
    const messages = BRIDGE_ERROR_CODES.map((code) =>
      bridgeErrorMessage(new Error(code)),
    );

    messages.forEach((message, index) => {
      expect(message).toBe(errorMessage(BRIDGE_ERROR_CODES[index]));
      expect(message).not.toBe(BRIDGE_ERROR_CODES[index]);
      expect(message).not.toBe(generic);
    });
    expect(new Set(messages).size).toBe(BRIDGE_ERROR_CODES.length);
  });

  it('leaves an unknown code to the generic toast title and keeps other failures', () => {
    expect(bridgeErrorMessage(new Error('future_code'))).toBe('');
    expect(bridgeErrorMessage(new Error('Desktop bridge timed out'))).toBe(
      'Desktop bridge timed out',
    );
    expect(bridgeErrorMessage(null)).toBe('');
  });
});

describe('commandFailureMessage', () => {
  it('explains a known failure code and falls back to the generic command failure', () => {
    const generic = t('voice.toast.commandFailedMessage');
    const interrupted = commandFailureMessage('recording_interrupted');
    const microphone = commandFailureMessage('microphone_read_failed');

    expect(interrupted).toBe(errorMessage('recording_interrupted'));
    expect(microphone).toBe(errorMessage('microphone_read_failed'));
    expect(new Set([interrupted, microphone, generic]).size).toBe(3);
    expect(commandFailureMessage('future_code')).toBe(generic);
  });
});
