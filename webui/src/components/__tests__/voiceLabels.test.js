import { beforeEach, describe, expect, it } from 'vitest';

import { init } from '../../lib/i18n.js';
import { bridgeErrorMessage, errorMessage } from '../voice/voiceLabels.js';

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

describe('bridgeErrorMessage', () => {
  beforeEach(() => {
    init('en');
  });

  it.each(BRIDGE_ERROR_CODES)('explains the bridge error %s', (code) => {
    const message = bridgeErrorMessage(new Error(code));

    expect(message).not.toBe('');
    expect(message).not.toBe(code);
    expect(message).toBe(errorMessage(code));
    expect(message).not.toBe(errorMessage('not_a_known_code'));
  });

  it('leaves an unknown code to the generic toast title', () => {
    expect(bridgeErrorMessage(new Error('future_code'))).toBe('');
  });

  it('keeps the message of a failure without a code', () => {
    expect(bridgeErrorMessage(new Error('Desktop bridge timed out'))).toBe(
      'Desktop bridge timed out',
    );
    expect(bridgeErrorMessage(null)).toBe('');
  });
});
