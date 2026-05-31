/** Pure helpers for Settings → Voice panel state and payloads. */

const VOICE_SETTINGS_DEFAULTS = Object.freeze({
  enabled: false,
  engine: 'openwakeword',
  microphone: null,
  sensitivity: 0.5,
  target_agent_id: null,
  session_behavior: 'active',
  wake_phrase: 'hey_jarvis',
  liveState: 'off',
});

/** Create the initial voice settings state with defaults. */
export function createVoiceSettingsState() {
  return { ...VOICE_SETTINGS_DEFAULTS };
}

/**
 * Hydrate voice settings state from a bridge wakeword status response.
 * Does not mutate `state` — returns a new object.
 */
export function applyWakewordStatus(state, status) {
  if (!status) return state;
  return {
    ...state,
    enabled: status.enabled ?? state.enabled,
    engine: status.engine ?? state.engine,
    microphone: status.microphone ?? state.microphone,
    sensitivity: status.sensitivity ?? state.sensitivity,
    target_agent_id: status.target_agent_id ?? state.target_agent_id,
    session_behavior: status.session_behavior ?? state.session_behavior,
    wake_phrase: status.wake_phrase ?? state.wake_phrase,
    liveState: status.state ?? state.liveState,
  };
}

/**
 * Build the payload for `setWakewordConfig()` from voice settings state.
 * Only includes keys that differ from the last-saved snapshot.
 */
export function buildVoiceSettingsPayload(state, lastSaved) {
  if (!lastSaved) {
    return {
      enabled: state.enabled,
      engine: state.engine,
      microphone: state.microphone,
      sensitivity: state.sensitivity,
      target_agent_id: state.target_agent_id,
      session_behavior: state.session_behavior,
      wake_phrase: state.wake_phrase,
    };
  }
  const payload = {};
  for (const key of Object.keys(VOICE_SETTINGS_DEFAULTS)) {
    if (key === 'liveState') continue;
    if (state[key] !== lastSaved[key]) {
      payload[key] = state[key];
    }
  }
  return payload;
}

/** True when voice settings have unsaved changes. */
export function voiceSettingsDirty(state, lastSaved) {
  if (!lastSaved) return false;
  for (const key of Object.keys(VOICE_SETTINGS_DEFAULTS)) {
    if (key === 'liveState') continue;
    if (state[key] !== lastSaved[key]) return true;
  }
  return false;
}

/** Clone the current state as a last-saved snapshot. */
export function snapshotVoiceSettings(state) {
  return { ...state };
}
