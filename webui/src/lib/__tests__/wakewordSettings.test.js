import { describe, expect, it } from 'vitest';

import {
  createVoiceSettingsState,
  applyWakewordStatus,
  applyRuntimeStatus,
  buildVoiceSettingsPayload,
  voiceSettingsDirty,
  snapshotVoiceSettings,
} from '../wakewordSettings.js';

function calibrationStatus(overrides = {}) {
  return {
    active: false,
    phase: null,
    scores: {},
    peaks: {},
    noise_levels: {},
    sample_counts: {},
    required_samples: 3,
    target_model_id: null,
    recommended_sensitivities: {},
    noise_seconds_remaining: 0,
    ...overrides,
  };
}

describe('createVoiceSettingsState', () => {
  it('starts with both Nabu wakeword models active', () => {
    const state = createVoiceSettingsState();

    expect(state.enabled).toBe(false);
    expect(state.active_model_ids).toEqual([
      'builtin/okay_nabu',
      'builtin/hey_nabu',
    ]);
    expect(state.model_sensitivities).toEqual({});
    expect(state.liveState).toBe('off');
    expect(state.calibration).toEqual(calibrationStatus());
  });

  it('isolates arrays and objects between calls', () => {
    const first = createVoiceSettingsState();
    const second = createVoiceSettingsState();

    first.active_model_ids.pop();
    first.model_sensitivities['builtin/okay_nabu'] = 0.8;

    expect(second.active_model_ids).toHaveLength(2);
    expect(second.model_sensitivities).toEqual({});
  });
});

describe('applyWakewordStatus', () => {
  it('hydrates the multi-model bridge contract', () => {
    const state = createVoiceSettingsState();
    const status = {
      enabled: true,
      state: 'listening',
      active_model_ids: ['builtin/okay_nabu', 'custom/computer'],
      model_sensitivities: {
        'builtin/okay_nabu': 0.8,
        'custom/computer': 0.65,
      },
      target_agent_id: 'agent-1',
      session_behavior: 'new',
      calibration: calibrationStatus({
        active: true,
        phase: 'phrases',
        scores: { 'builtin/okay_nabu': 0.4 },
        peaks: { 'builtin/okay_nabu': 0.7 },
        noise_levels: { 'builtin/okay_nabu': 0.03 },
        sample_counts: { 'builtin/okay_nabu': 2 },
        target_model_id: 'builtin/okay_nabu',
      }),
    };

    const hydrated = applyWakewordStatus(state, status);

    expect(hydrated.enabled).toBe(true);
    expect(hydrated.liveState).toBe('listening');
    expect(hydrated.active_model_ids).toEqual(status.active_model_ids);
    expect(hydrated.model_sensitivities).toEqual(status.model_sensitivities);
    expect(hydrated.active_model_ids).not.toBe(status.active_model_ids);
    expect(hydrated.model_sensitivities).not.toBe(status.model_sensitivities);
    expect(hydrated.target_agent_id).toBe('agent-1');
    expect(hydrated.session_behavior).toBe('new');
    expect(hydrated.calibration).toEqual(status.calibration);
    expect(hydrated.calibration).not.toBe(status.calibration);
  });

  it('preserves editable values for missing keys and accepts explicit nulls', () => {
    const state = {
      ...createVoiceSettingsState(),
      microphone: 3,
      target_agent_id: 'agent-1',
      model_sensitivities: { 'builtin/okay_nabu': 0.3 },
    };

    const hydrated = applyWakewordStatus(state, {
      enabled: true,
      microphone: null,
      target_agent_id: null,
      calibration: calibrationStatus({
        active: true,
        phase: 'phrases',
        scores: { 'builtin/hey_nabu': 0.35 },
        peaks: { 'builtin/hey_nabu': 0.72 },
        noise_levels: { 'builtin/hey_nabu': 0.02 },
        sample_counts: { 'builtin/hey_nabu': 1 },
        target_model_id: 'builtin/hey_nabu',
      }),
    });

    expect(hydrated.enabled).toBe(true);
    expect(hydrated.microphone).toBeNull();
    expect(hydrated.target_agent_id).toBeNull();
    expect(hydrated.model_sensitivities).toEqual({
      'builtin/okay_nabu': 0.3,
    });
  });

  it('returns the original state when status is absent', () => {
    const state = createVoiceSettingsState();
    expect(applyWakewordStatus(state, null)).toBe(state);
  });
});

describe('applyRuntimeStatus', () => {
  it('updates runtime fields without reverting model edits', () => {
    const state = {
      ...createVoiceSettingsState(),
      active_model_ids: ['builtin/hey_nabu'],
      model_sensitivities: { 'builtin/hey_nabu': 0.9 },
      target_agent_id: 'agent-1',
    };
    const status = {
      state: 'recording',
      mock: true,
      mode: 'unavailable',
      error_code: 'microphone_unavailable',
      active_microphone: { index: 4, name: 'Desk mic', sample_rate: 48000 },
      active_model_ids: ['builtin/okay_nabu'],
      model_sensitivities: { 'builtin/okay_nabu': 0.5 },
      target_agent_id: null,
      calibration: calibrationStatus({
        active: true,
        phase: 'ready',
        scores: { 'builtin/hey_nabu': 0.35 },
        peaks: { 'builtin/hey_nabu': 0.72 },
        noise_levels: { 'builtin/hey_nabu': 0.02 },
        sample_counts: { 'builtin/hey_nabu': 3 },
        recommended_sensitivities: { 'builtin/hey_nabu': 0.8 },
      }),
    };

    const next = applyRuntimeStatus(state, status);

    expect(next.liveState).toBe('recording');
    expect(next.mock).toBe(true);
    expect(next.mode).toBe('unavailable');
    expect(next.errorCode).toBe('microphone_unavailable');
    expect(next.activeMicrophone.name).toBe('Desk mic');
    expect(next.active_model_ids).toEqual(['builtin/hey_nabu']);
    expect(next.model_sensitivities).toEqual({ 'builtin/hey_nabu': 0.9 });
    expect(next.target_agent_id).toBe('agent-1');
    expect(next.calibration).toEqual(status.calibration);
  });

  it('returns the same reference when runtime state is unchanged', () => {
    const state = {
      ...createVoiceSettingsState(),
      liveState: 'listening',
      mock: false,
    };
    expect(applyRuntimeStatus(state, { state: 'listening', mock: false })).toBe(
      state,
    );
  });
});

describe('buildVoiceSettingsPayload', () => {
  it('builds a full structured payload without runtime fields', () => {
    const state = {
      ...createVoiceSettingsState(),
      enabled: true,
      model_sensitivities: {
        'builtin/okay_nabu': 0.7,
        'builtin/hey_nabu': 0.6,
      },
      target_agent_id: 'agent-1',
      session_behavior: 'new',
      liveState: 'listening',
    };

    const payload = buildVoiceSettingsPayload(state, null);

    expect(payload.active_model_ids).toEqual(state.active_model_ids);
    expect(payload.model_sensitivities).toEqual(state.model_sensitivities);
    expect(payload.target_agent_id).toBe('agent-1');
    expect(payload.session_behavior).toBe('new');
    expect(payload.liveState).toBeUndefined();
  });

  it('detects array and object changes by value', () => {
    const lastSaved = snapshotVoiceSettings(createVoiceSettingsState());
    const state = {
      ...lastSaved,
      active_model_ids: ['builtin/okay_nabu'],
      model_sensitivities: { 'builtin/okay_nabu': 0.9 },
    };

    expect(buildVoiceSettingsPayload(state, lastSaved)).toEqual({
      active_model_ids: ['builtin/okay_nabu'],
      model_sensitivities: { 'builtin/okay_nabu': 0.9 },
    });
  });

  it('ignores runtime-only changes', () => {
    const lastSaved = snapshotVoiceSettings(createVoiceSettingsState());
    const state = {
      ...lastSaved,
      liveState: 'recording',
      mock: true,
      calibration: calibrationStatus({
        active: true,
        phase: 'noise',
        scores: { 'builtin/okay_nabu': 0.4 },
        peaks: { 'builtin/okay_nabu': 0.7 },
        noise_seconds_remaining: 2,
      }),
    };

    expect(buildVoiceSettingsPayload(state, lastSaved)).toEqual({});
    expect(voiceSettingsDirty(state, lastSaved)).toBe(false);
  });
});

describe('voiceSettingsDirty and snapshotVoiceSettings', () => {
  it('treats equivalent structured values as clean', () => {
    const state = createVoiceSettingsState();
    const lastSaved = snapshotVoiceSettings(state);

    state.active_model_ids = [...state.active_model_ids];
    state.model_sensitivities = { ...state.model_sensitivities };

    expect(voiceSettingsDirty(state, lastSaved)).toBe(false);
  });

  it('deep-clones structured editable values', () => {
    const state = createVoiceSettingsState();
    const snapshot = snapshotVoiceSettings(state);

    state.active_model_ids.pop();
    state.model_sensitivities['builtin/okay_nabu'] = 0.8;

    expect(snapshot.active_model_ids).toHaveLength(2);
    expect(snapshot.model_sensitivities).toEqual({});
  });
});
