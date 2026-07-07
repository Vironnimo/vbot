import { describe, expect, it } from 'vitest';

import {
  createVoiceSettingsState,
  applyWakewordStatus,
  applyRuntimeStatus,
  buildVoiceSettingsPayload,
  voiceSettingsDirty,
  snapshotVoiceSettings,
} from '../wakewordSettings.js';

describe('createVoiceSettingsState', () => {
  it('returns defaults', () => {
    const state = createVoiceSettingsState();
    expect(state.enabled).toBe(false);
    expect(state.engine).toBe('openwakeword');
    expect(state.sensitivity).toBe(0.5);
    expect(state.liveState).toBe('off');
  });

  it('returns a new object each call', () => {
    const a = createVoiceSettingsState();
    const b = createVoiceSettingsState();
    expect(a).not.toBe(b);
  });
});

describe('applyWakewordStatus', () => {
  it('hydrates from bridge status', () => {
    const state = createVoiceSettingsState();
    const status = {
      enabled: true,
      state: 'listening',
      sensitivity: 0.8,
      engine: 'openwakeword',
      target_agent_id: 'agent-1',
      session_behavior: 'new',
    };

    const hydrated = applyWakewordStatus(state, status);
    expect(hydrated.enabled).toBe(true);
    expect(hydrated.liveState).toBe('listening');
    expect(hydrated.sensitivity).toBe(0.8);
    expect(hydrated.target_agent_id).toBe('agent-1');
    expect(hydrated.session_behavior).toBe('new');
  });

  it('preserves existing values for missing keys', () => {
    const state = { ...createVoiceSettingsState(), sensitivity: 0.3 };
    const status = { enabled: true };

    const hydrated = applyWakewordStatus(state, status);
    expect(hydrated.sensitivity).toBe(0.3);
    expect(hydrated.enabled).toBe(true);
  });

  it('applies explicit null values from bridge status', () => {
    const state = {
      ...createVoiceSettingsState(),
      microphone: 3,
      target_agent_id: 'agent-1',
    };
    const status = { microphone: null, target_agent_id: null };

    const hydrated = applyWakewordStatus(state, status);

    expect(hydrated.microphone).toBeNull();
    expect(hydrated.target_agent_id).toBeNull();
  });

  it('returns same state when status is null', () => {
    const state = createVoiceSettingsState();
    const hydrated = applyWakewordStatus(state, null);
    expect(hydrated).toEqual(state);
  });

  it('hydrates the mock flag', () => {
    const state = createVoiceSettingsState();
    const hydrated = applyWakewordStatus(state, { mock: true });
    expect(hydrated.mock).toBe(true);
  });
});

describe('applyRuntimeStatus', () => {
  it('updates only liveState and mock, never editable config', () => {
    const state = {
      ...createVoiceSettingsState(),
      sensitivity: 0.9,
      target_agent_id: 'agent-1',
    };
    const status = {
      state: 'recording',
      mock: true,
      // Config fields in the poll payload must be ignored so an in-progress
      // edit is never reverted.
      sensitivity: 0.5,
      target_agent_id: null,
    };

    const next = applyRuntimeStatus(state, status);

    expect(next.liveState).toBe('recording');
    expect(next.mock).toBe(true);
    expect(next.sensitivity).toBe(0.9);
    expect(next.target_agent_id).toBe('agent-1');
  });

  it('returns the same reference when nothing changed', () => {
    const state = {
      ...createVoiceSettingsState(),
      liveState: 'listening',
      mock: false,
    };
    const next = applyRuntimeStatus(state, { state: 'listening', mock: false });
    expect(next).toBe(state);
  });

  it('returns same state when status is null', () => {
    const state = createVoiceSettingsState();
    expect(applyRuntimeStatus(state, null)).toBe(state);
  });
});

describe('buildVoiceSettingsPayload', () => {
  it('builds full payload when no last-saved snapshot', () => {
    const state = {
      ...createVoiceSettingsState(),
      enabled: true,
      sensitivity: 0.7,
      target_agent_id: 'agent-1',
      session_behavior: 'new',
      liveState: 'listening',
    };

    const payload = buildVoiceSettingsPayload(state, null);
    expect(payload.enabled).toBe(true);
    expect(payload.sensitivity).toBe(0.7);
    expect(payload.target_agent_id).toBe('agent-1');
    expect(payload.session_behavior).toBe('new');
    expect(payload.liveState).toBeUndefined(); // liveState excluded
  });

  it('builds sparse payload with only changed keys', () => {
    const lastSaved = {
      ...createVoiceSettingsState(),
      enabled: false,
      sensitivity: 0.5,
    };
    const state = {
      ...lastSaved,
      sensitivity: 0.9,
      target_agent_id: 'agent-2',
    };

    const payload = buildVoiceSettingsPayload(state, lastSaved);
    expect(Object.keys(payload)).toEqual(['sensitivity', 'target_agent_id']);
    expect(payload.sensitivity).toBe(0.9);
    expect(payload.enabled).toBeUndefined(); // unchanged
  });

  it('excludes liveState from payload', () => {
    const state = createVoiceSettingsState();
    const payload = buildVoiceSettingsPayload(state, null);
    expect(payload.liveState).toBeUndefined();
    expect(payload.enabled).toBeDefined();
  });

  it('excludes the runtime mock flag from payload and dirty', () => {
    const lastSaved = snapshotVoiceSettings(createVoiceSettingsState());
    const state = { ...lastSaved, mock: true };

    expect(buildVoiceSettingsPayload(state, lastSaved)).toEqual({});
    expect(voiceSettingsDirty(state, lastSaved)).toBe(false);
  });
});

describe('voiceSettingsDirty', () => {
  it('returns false when unchanged', () => {
    const state = createVoiceSettingsState();
    const lastSaved = snapshotVoiceSettings(state);
    expect(voiceSettingsDirty(state, lastSaved)).toBe(false);
  });

  it('returns true when changed', () => {
    const state = createVoiceSettingsState();
    const lastSaved = snapshotVoiceSettings(state);
    state.sensitivity = 0.8;
    expect(voiceSettingsDirty(state, lastSaved)).toBe(true);
  });

  it('ignores liveState changes', () => {
    const state = createVoiceSettingsState();
    const lastSaved = snapshotVoiceSettings(state);
    state.liveState = 'recording';
    expect(voiceSettingsDirty(state, lastSaved)).toBe(false);
  });

  it('returns false when no lastSaved', () => {
    const state = createVoiceSettingsState();
    expect(voiceSettingsDirty(state, null)).toBe(false);
  });
});

describe('snapshotVoiceSettings', () => {
  it('clones the current state', () => {
    const state = createVoiceSettingsState();
    const snapshot = snapshotVoiceSettings(state);
    expect(snapshot).toEqual(state);
    expect(snapshot).not.toBe(state);
  });
});
