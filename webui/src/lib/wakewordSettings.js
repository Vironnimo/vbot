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
  mock: false,
});

// Fields observed from the worker, not edited by the user: never part of a save
// payload or the dirty check, and safe for a status poll to overwrite mid-edit.
const RUNTIME_KEYS = new Set(['liveState', 'mock']);

const hasKey = (value, key) => Object.prototype.hasOwnProperty.call(value, key);

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
    enabled: hasKey(status, 'enabled') ? status.enabled : state.enabled,
    engine: hasKey(status, 'engine') ? status.engine : state.engine,
    microphone: hasKey(status, 'microphone')
      ? status.microphone
      : state.microphone,
    sensitivity: hasKey(status, 'sensitivity')
      ? status.sensitivity
      : state.sensitivity,
    target_agent_id: hasKey(status, 'target_agent_id')
      ? status.target_agent_id
      : state.target_agent_id,
    session_behavior: hasKey(status, 'session_behavior')
      ? status.session_behavior
      : state.session_behavior,
    wake_phrase: hasKey(status, 'wake_phrase')
      ? status.wake_phrase
      : state.wake_phrase,
    liveState: hasKey(status, 'state') ? status.state : state.liveState,
    mock: hasKey(status, 'mock') ? status.mock : state.mock,
  };
}

/**
 * Merge only the observed runtime fields (live worker state, mock flag) from a
 * status poll, leaving editable config fields untouched. Used by the 500ms poll
 * so it can never revert an unsaved edit made during the autosave debounce.
 * Returns the same object reference when nothing changed.
 */
export function applyRuntimeStatus(state, status) {
  if (!status) return state;
  let next = state;
  if (hasKey(status, 'state') && status.state !== state.liveState) {
    next = { ...next, liveState: status.state };
  }
  if (hasKey(status, 'mock') && status.mock !== next.mock) {
    next = { ...next, mock: status.mock };
  }
  return next;
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
    if (RUNTIME_KEYS.has(key)) continue;
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
    if (RUNTIME_KEYS.has(key)) continue;
    if (state[key] !== lastSaved[key]) return true;
  }
  return false;
}

/** Clone the current state as a last-saved snapshot. */
export function snapshotVoiceSettings(state) {
  return { ...state };
}
