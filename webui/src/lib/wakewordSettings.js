/** Pure helpers for Settings → Voice panel state and payloads. */

const DEFAULT_ACTIVE_MODEL_IDS = Object.freeze([
  'builtin/okay_nabu',
  'builtin/hey_nabu',
]);

const VOICE_SETTINGS_DEFAULTS = Object.freeze({
  enabled: false,
  microphone: null,
  active_model_ids: DEFAULT_ACTIVE_MODEL_IDS,
  model_sensitivities: Object.freeze({}),
  target_agent_id: null,
  session_behavior: 'active',
  liveState: 'off',
  mock: false,
  mode: 'real',
  errorCode: null,
  activeMicrophone: null,
});

const RUNTIME_KEYS = new Set([
  'liveState',
  'mock',
  'mode',
  'errorCode',
  'activeMicrophone',
]);

const hasKey = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
const sameMicrophone = (left, right) =>
  left === right ||
  (left?.index === right?.index &&
    left?.name === right?.name &&
    left?.sample_rate === right?.sample_rate);
const sameArray = (left, right) =>
  Array.isArray(left) &&
  Array.isArray(right) &&
  left.length === right.length &&
  left.every((value, index) => value === right[index]);
const sameObject = (left, right) => {
  if (left === right) return true;
  if (
    !left ||
    !right ||
    typeof left !== 'object' ||
    typeof right !== 'object'
  ) {
    return false;
  }
  const leftKeys = Object.keys(left);
  const rightKeys = Object.keys(right);
  return (
    leftKeys.length === rightKeys.length &&
    leftKeys.every((key) => right[key] === left[key])
  );
};

/** Create the initial voice settings state with isolated structured defaults. */
export function createVoiceSettingsState() {
  return {
    ...VOICE_SETTINGS_DEFAULTS,
    active_model_ids: [...DEFAULT_ACTIVE_MODEL_IDS],
    model_sensitivities: {},
  };
}

/** Hydrate editable and runtime state from a full bridge status response. */
export function applyWakewordStatus(state, status) {
  if (!status) return state;
  return {
    ...state,
    enabled: hasKey(status, 'enabled') ? status.enabled : state.enabled,
    microphone: hasKey(status, 'microphone')
      ? status.microphone
      : state.microphone,
    active_model_ids: hasKey(status, 'active_model_ids')
      ? [...status.active_model_ids]
      : state.active_model_ids,
    model_sensitivities: hasKey(status, 'model_sensitivities')
      ? { ...status.model_sensitivities }
      : state.model_sensitivities,
    target_agent_id: hasKey(status, 'target_agent_id')
      ? status.target_agent_id
      : state.target_agent_id,
    session_behavior: hasKey(status, 'session_behavior')
      ? status.session_behavior
      : state.session_behavior,
    liveState: hasKey(status, 'state') ? status.state : state.liveState,
    mock: hasKey(status, 'mock') ? status.mock : state.mock,
    mode: hasKey(status, 'mode') ? status.mode : state.mode,
    errorCode: hasKey(status, 'error_code')
      ? status.error_code
      : state.errorCode,
    activeMicrophone: hasKey(status, 'active_microphone')
      ? status.active_microphone
      : state.activeMicrophone,
  };
}

/** Merge worker-owned runtime fields without overwriting edits in progress. */
export function applyRuntimeStatus(state, status) {
  if (!status) return state;
  let next = state;
  if (hasKey(status, 'state') && status.state !== state.liveState) {
    next = { ...next, liveState: status.state };
  }
  if (hasKey(status, 'mock') && status.mock !== next.mock) {
    next = { ...next, mock: status.mock };
  }
  if (hasKey(status, 'mode') && status.mode !== next.mode) {
    next = { ...next, mode: status.mode };
  }
  if (hasKey(status, 'error_code') && status.error_code !== next.errorCode) {
    next = { ...next, errorCode: status.error_code };
  }
  if (
    hasKey(status, 'active_microphone') &&
    !sameMicrophone(status.active_microphone, next.activeMicrophone)
  ) {
    next = { ...next, activeMicrophone: status.active_microphone };
  }
  return next;
}

/** Build a full or sparse `setWakewordConfig()` payload. */
export function buildVoiceSettingsPayload(state, lastSaved) {
  if (!lastSaved) {
    return editableVoiceSettings(state);
  }
  const payload = {};
  for (const key of Object.keys(VOICE_SETTINGS_DEFAULTS)) {
    if (RUNTIME_KEYS.has(key)) continue;
    if (!sameSetting(key, state[key], lastSaved[key])) {
      payload[key] = cloneSetting(key, state[key]);
    }
  }
  return payload;
}

/** True when voice settings have unsaved changes. */
export function voiceSettingsDirty(state, lastSaved) {
  if (!lastSaved) return false;
  for (const key of Object.keys(VOICE_SETTINGS_DEFAULTS)) {
    if (RUNTIME_KEYS.has(key)) continue;
    if (!sameSetting(key, state[key], lastSaved[key])) return true;
  }
  return false;
}

/** Clone current state as an isolated last-saved snapshot. */
export function snapshotVoiceSettings(state) {
  return {
    ...state,
    active_model_ids: [...state.active_model_ids],
    model_sensitivities: { ...state.model_sensitivities },
    activeMicrophone: state.activeMicrophone
      ? { ...state.activeMicrophone }
      : null,
  };
}

function editableVoiceSettings(state) {
  const payload = {};
  for (const key of Object.keys(VOICE_SETTINGS_DEFAULTS)) {
    if (!RUNTIME_KEYS.has(key)) payload[key] = cloneSetting(key, state[key]);
  }
  return payload;
}

function sameSetting(key, left, right) {
  if (key === 'active_model_ids') return sameArray(left, right);
  if (key === 'model_sensitivities') return sameObject(left, right);
  return left === right;
}

function cloneSetting(key, value) {
  if (key === 'active_model_ids') return [...value];
  if (key === 'model_sensitivities') return { ...value };
  return value;
}
