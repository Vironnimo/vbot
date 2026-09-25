/** Desktop capability detection and pywebview bridge client.

 * The Desktop accessor injects `window.pywebview.api` when the WebUI
 * loads inside pywebview with `?accessor=desktop` in the URL.
 */

import { isPlainObject } from './values.js';

const BRIDGE_READY_EVENT = 'pywebviewready';
const BRIDGE_READY_TIMEOUT_MS = 5000;
const LIVE_REQUEST_EVENT = 'vbot-desktop-live';
const LIVE_REQUEST_ACTIONS = new Set(['start', 'toggle']);
const LIVE_REQUEST_SOURCES = new Set(['wakeword', 'hotkey']);
const VOICE_PUSH_EVENT = 'vbot-desktop-voice';
// The Voice UI works only against this Desktop Voice bridge version.
export const DESKTOP_VOICE_API_VERSION = 2;
// A Live voice start never waits longer than this for the Desktop capabilities
// that decide whether the page may open the microphone.
const MICROPHONE_ACCESS_TIMEOUT_MS = 3000;
const DISABLED_DESKTOP_CAPABILITIES = Object.freeze({
  wakeword: false,
  voiceApi: 0,
  serverSelection: false,
  contextMenu: false,
  liveHotkey: false,
  secureOrigins: Object.freeze([]),
});

const VOICE_STATES = new Set([
  'off',
  'starting',
  'listening',
  'microphone_disconnected',
  'error',
]);
const VOICE_MODES = new Set(['real', 'mock', 'unavailable']);
const ECHO_CANCELLATION_STATES = new Set([
  'off',
  'starting',
  'active',
  'no_reference',
  'unavailable',
]);
const SESSION_BEHAVIORS = new Set(['active', 'new']);
const LIVE_VOICE_MODES = new Set(['start', 'toggle']);
const CALIBRATION_PHASES = new Set(['noise', 'phrases', 'ready']);
// Event kinds a newer Desktop adds still advance the sequence; consumers
// ignore kinds they do not know.
const VOICE_EVENT_KIND_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;
const DESKTOP_ERROR_CODE_PATTERN = /^[a-z][a-z0-9_]*$/;

let cachedCapabilities = null;
let cachedBridgeApi = null;
let voiceAudioContext = null;

/** True when the WebUI was loaded through the Desktop accessor URL. */
export function isDesktopAccessor() {
  if (typeof window === 'undefined') {
    return false;
  }
  const params = new URLSearchParams(window.location.search);
  return params.get('accessor') === 'desktop';
}

/** True when the WebUI is loaded inside the vBot Desktop pywebview shell. */
export function isDesktop() {
  return isDesktopAccessor() && bridgeAvailable();
}

/** Return whether the pywebview bridge is reachable. */
function bridgeAvailable() {
  return typeof window !== 'undefined' && Boolean(window.pywebview?.api);
}

/**
 * Resolve once the pywebview bridge is ready, or false after a short timeout.
 *
 * pywebview creates `window.pywebview.api` asynchronously and announces it via
 * `pywebviewready`; Desktop boot must wait for that instead of treating the
 * first missing global as a permanent browser mode.
 */
export function waitForDesktopBridge(timeoutMs = BRIDGE_READY_TIMEOUT_MS) {
  if (!isDesktopAccessor()) {
    return Promise.resolve(false);
  }

  if (bridgeAvailable()) {
    return Promise.resolve(true);
  }

  return new Promise((resolve) => {
    let resolved = false;
    let timeoutId = null;

    const finish = () => {
      if (resolved) {
        return;
      }
      resolved = true;
      window.removeEventListener(BRIDGE_READY_EVENT, finish);
      if (timeoutId) {
        clearTimeout(timeoutId);
      }
      resolve(bridgeAvailable());
    };

    window.addEventListener(BRIDGE_READY_EVENT, finish, { once: true });
    timeoutId = setTimeout(finish, timeoutMs);
  });
}

/**
 * The stable error code of a rejected Desktop bridge call, or null.
 *
 * The Desktop rejects a known, user-actionable failure with an Error whose
 * message is exactly its code (for example `voice_config_invalid`); any other
 * failure carries a human-readable message instead.
 */
export function desktopErrorCode(error) {
  const message = error?.message;
  return typeof message === 'string' && DESKTOP_ERROR_CODE_PATTERN.test(message)
    ? message
    : null;
}

/** Call a bridge method by name, returning a Promise of the result. */
function callBridge(method, ...args) {
  if (!bridgeAvailable()) {
    return Promise.reject(new Error('Desktop bridge not available'));
  }
  return window.pywebview.api[method](...args);
}

/**
 * Fetch desktop capabilities from the bridge.
 * Result is cached after the first successful call from a live bridge.
 * Returns disabled capability flags when the bridge is absent, without caching.
 * `voiceApi` is the Desktop Voice bridge version (0 when none is offered).
 */
export async function getDesktopCapabilities() {
  if (!bridgeAvailable()) {
    return { ...DISABLED_DESKTOP_CAPABILITIES };
  }
  if (cachedCapabilities && cachedBridgeApi === window.pywebview.api) {
    return cachedCapabilities;
  }
  const caps = await callBridge('getDesktopCapabilities');
  cachedCapabilities = {
    wakeword: Boolean(caps?.wakeword),
    voiceApi:
      Number.isSafeInteger(caps?.voiceApi) && caps.voiceApi > 0
        ? caps.voiceApi
        : 0,
    serverSelection: Boolean(caps?.serverSelection),
    contextMenu: Boolean(caps?.contextMenu),
    liveHotkey: Boolean(caps?.liveHotkey),
    secureOrigins: Array.isArray(caps?.secureOrigins)
      ? caps.secureOrigins.filter((origin) => typeof origin === 'string')
      : [],
  };
  cachedBridgeApi = window.pywebview.api;
  return cachedCapabilities;
}

/** Disabled capability flags for accessors without a Desktop bridge. */
export function disabledDesktopCapabilities() {
  return { ...DISABLED_DESKTOP_CAPABILITIES, secureOrigins: [] };
}

/** True when the capabilities offer the Voice bridge this WebUI speaks. */
export function supportsDesktopVoice(capabilities) {
  return (
    capabilities?.wakeword === true &&
    capabilities?.voiceApi === DESKTOP_VOICE_API_VERSION
  );
}

/** Replace the Desktop host clipboard with plain text. */
export async function setDesktopClipboardText(text) {
  return callBridge('setClipboardText', String(text));
}

/** Read plain text from the Desktop host clipboard. */
export async function getDesktopClipboardText() {
  const text = await callBridge('getClipboardText');
  return typeof text === 'string' ? text : '';
}

/** Open an absolute HTTP(S) URL in the Desktop host's default browser. */
export async function openDesktopExternalUrl(url) {
  return callBridge('openExternalUrl', url);
}

/** Return Desktop-local remembered servers, including the active marker. */
export async function listDesktopServers() {
  const servers = await callBridge('listServers');
  return Array.isArray(servers) ? servers : [];
}

/** Remember a Desktop server without changing the current connection. */
export async function addDesktopServer(host, port, label = '') {
  return callBridge('addServer', host, port, label);
}

/** Forget an inactive remembered Desktop server. */
export async function removeDesktopServer(host, port) {
  return callBridge('removeServer', host, port);
}

/**
 * Probe a remembered Desktop server and navigate only after the bridge Promise
 * resolves. Replacing the page inside the Python bridge call destroys
 * pywebview's callback, so JavaScript deliberately owns the final navigation.
 */
export async function selectDesktopServer(host, port) {
  const result = await callBridge('selectServer', host, port);
  if (result?.url) {
    window.location.assign(result.url);
  }
  return result;
}

// -- Voice (bridge API v2) ----------------------------------------------------

const text = (value) =>
  typeof value === 'string' && value.trim().length > 0 ? value : null;
const finite = (value) =>
  typeof value === 'number' && Number.isFinite(value) ? value : null;
const sequenceNumber = (value) =>
  Number.isSafeInteger(value) && value >= 0 ? value : null;

function normalizeMicrophone(raw, { withSampleRate = false } = {}) {
  if (!isPlainObject(raw) || !Number.isInteger(raw.index) || !text(raw.name))
    return null;
  const microphone = {
    index: raw.index,
    name: raw.name,
    host_api: typeof raw.host_api === 'string' ? raw.host_api : '',
  };
  if (withSampleRate) microphone.sample_rate = finite(raw.sample_rate);
  return microphone;
}

function normalizeAction(raw) {
  if (!isPlainObject(raw)) return null;
  if (raw.type === 'live_voice') {
    return {
      type: 'live_voice',
      mode: LIVE_VOICE_MODES.has(raw.mode) ? raw.mode : 'toggle',
    };
  }
  if (raw.type === 'command') {
    return {
      type: 'command',
      agent_id: text(raw.agent_id),
      session_behavior: SESSION_BEHAVIORS.has(raw.session_behavior)
        ? raw.session_behavior
        : null,
    };
  }
  return null;
}

function normalizePhrases(raw) {
  if (!Array.isArray(raw)) return [];
  const seen = new Set();
  const phrases = [];
  for (const entry of raw) {
    const modelId = text(entry?.model_id);
    if (!modelId || seen.has(modelId)) continue;
    seen.add(modelId);
    phrases.push({
      model_id: modelId,
      label: text(entry.label) ?? modelId,
      sensitivity: finite(entry.sensitivity),
      // A phrase without a stored action is a command with the defaults.
      action: normalizeAction(entry.action) ?? {
        type: 'command',
        agent_id: null,
        session_behavior: null,
      },
      effective: normalizeAction(entry.effective),
      problem: text(entry.problem),
    });
  }
  return phrases;
}

function normalizeRecording(raw) {
  const commandId = text(raw?.command_id);
  if (!commandId) return null;
  return {
    command_id: commandId,
    model_id: text(raw.model_id),
    agent_id: text(raw.agent_id),
  };
}

function normalizeCommands(raw) {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((entry) => text(entry?.command_id))
    .map((entry) => ({
      command_id: entry.command_id,
      model_id: text(entry.model_id),
      stage: text(entry.stage),
    }));
}

function normalizeCalibration(raw) {
  const modelId = text(raw?.model_id);
  if (!modelId) return null;
  const count = (value) =>
    Number.isInteger(value) && value >= 0 ? value : null;
  return {
    model_id: modelId,
    phase: CALIBRATION_PHASES.has(raw.phase) ? raw.phase : 'noise',
    score: finite(raw.score) ?? 0,
    peak: finite(raw.peak) ?? 0,
    noise_level: finite(raw.noise_level) ?? 0,
    noise_high: raw.noise_high === true,
    sample_count: count(raw.sample_count) ?? 0,
    required_samples:
      Number.isInteger(raw.required_samples) && raw.required_samples > 0
        ? raw.required_samples
        : null,
    recommended_sensitivity: finite(raw.recommended_sensitivity),
    noise_seconds_remaining: Math.max(
      0,
      finite(raw.noise_seconds_remaining) ?? 0,
    ),
  };
}

function normalizeLimits(raw) {
  const maxActive = raw?.max_active_phrases;
  let minSensitivity = finite(raw?.min_sensitivity);
  let maxSensitivity = finite(raw?.max_sensitivity);
  if (
    minSensitivity === null ||
    maxSensitivity === null ||
    minSensitivity >= maxSensitivity
  ) {
    minSensitivity = null;
    maxSensitivity = null;
  }
  return {
    max_active_phrases:
      Number.isSafeInteger(maxActive) && maxActive > 0 ? maxActive : null,
    min_sensitivity: minSensitivity,
    max_sensitivity: maxSensitivity,
  };
}

/**
 * Validate one Voice status snapshot from the Desktop.
 *
 * Returns a snapshot with every documented field present (unknown values fall
 * back to safe defaults), or null when the value is not a snapshot at all.
 * Limits the Desktop did not report stay null; the UI then offers no control
 * that depends on them.
 */
export function normalizeVoiceStatus(raw) {
  if (!isPlainObject(raw)) return null;
  const sequence = sequenceNumber(raw.sequence);
  if (sequence === null) return null;
  const state = VOICE_STATES.has(raw.state) ? raw.state : 'off';
  const echo = isPlainObject(raw.echo_cancellation)
    ? raw.echo_cancellation
    : {};
  return {
    enabled: raw.enabled === true,
    mode: VOICE_MODES.has(raw.mode) ? raw.mode : 'real',
    state,
    error_code: text(raw.error_code),
    sequence,
    microphone: normalizeMicrophone(raw.microphone),
    active_microphone: normalizeMicrophone(raw.active_microphone, {
      withSampleRate: true,
    }),
    echo_cancellation: {
      enabled: echo.enabled !== false,
      state: ECHO_CANCELLATION_STATES.has(echo.state) ? echo.state : 'off',
    },
    default_agent_id: text(raw.default_agent_id),
    default_session_behavior: SESSION_BEHAVIORS.has(
      raw.default_session_behavior,
    )
      ? raw.default_session_behavior
      : 'active',
    phrases: normalizePhrases(raw.phrases),
    recording: normalizeRecording(raw.recording),
    commands: normalizeCommands(raw.commands),
    calibration: normalizeCalibration(raw.calibration),
    limits: normalizeLimits(raw.limits),
  };
}

/** Validate one pushed Voice event; null when it is not an event. */
export function normalizeVoiceEvent(raw) {
  if (!isPlainObject(raw)) return null;
  const sequence = sequenceNumber(raw.sequence);
  if (sequence === null || typeof raw.kind !== 'string') return null;
  if (!VOICE_EVENT_KIND_PATTERN.test(raw.kind)) return null;
  return {
    sequence,
    kind: raw.kind,
    model_id: text(raw.model_id),
    command_id: text(raw.command_id),
    agent_id: text(raw.agent_id),
    session_id: text(raw.session_id),
    error_code: text(raw.error_code),
  };
}

function requireVoiceStatus(raw) {
  const status = normalizeVoiceStatus(raw);
  if (!status) throw new Error('The Desktop returned an invalid Voice status');
  return status;
}

/** Read the current Voice status snapshot. */
export async function getVoiceStatus() {
  return requireVoiceStatus(await callBridge('getVoiceStatus'));
}

/**
 * Enable or disable Voice listening. Resolves `{enabled, error_code}`: the
 * setting the Desktop kept and, when it refused, the reason.
 */
export async function setVoiceEnabled(enabled) {
  const result = await callBridge('setVoiceEnabled', Boolean(enabled));
  return {
    enabled:
      typeof result?.enabled === 'boolean' ? result.enabled : Boolean(enabled),
    error_code: text(result?.error_code),
  };
}

/**
 * Apply a partial Voice configuration change and resolve the resulting status
 * snapshot. `model_sensitivities` and `phrase_actions` merge per phrase; a
 * `null` phrase action restores the default command.
 */
export async function updateVoiceConfig(changes) {
  return requireVoiceStatus(await callBridge('updateVoiceConfig', changes));
}

/** Enumerate Desktop-local microphone devices and compatibility. */
export async function listMicrophones() {
  const devices = await callBridge('listMicrophones');
  return Array.isArray(devices) ? devices : [];
}

/**
 * Enumerate curated and imported Desktop-local wakeword models. Each
 * descriptor carries `overlaps`: ids of models that can fire on the same words.
 */
export async function listWakewordModels() {
  const models = await callBridge('listWakewordModels');
  if (!Array.isArray(models)) return [];
  return models
    .filter((model) => text(model?.id))
    .map((model) => ({
      ...model,
      label: text(model.label) ?? model.id,
      overlaps: Array.isArray(model.overlaps)
        ? model.overlaps.filter((id) => typeof id === 'string')
        : [],
    }));
}

/** Validate and install one user-selected TFLite wakeword model. */
export async function importWakewordModel(filename, contentBase64) {
  return callBridge('importWakewordModel', filename, contentBase64);
}

/** Permanently remove one inactive imported wakeword model. */
export async function deleteWakewordModel(modelId) {
  return callBridge('deleteWakewordModel', modelId);
}

/** Restart Voice listening after an actionable error. */
export async function retryVoice() {
  return callBridge('retryVoice');
}

/** Stop the active command recording and send what was captured so far. */
export async function stopVoiceRecording() {
  return callBridge('stopVoiceRecording');
}

/**
 * Calibrate one active phrase: commands pause while the Desktop measures room
 * noise and repetitions. Resolves the resulting status snapshot.
 */
export async function startVoiceCalibration(modelId) {
  return requireVoiceStatus(await callBridge('startVoiceCalibration', modelId));
}

/** Discard the measurements and restart calibration from room noise. */
export async function restartVoiceCalibration() {
  return requireVoiceStatus(await callBridge('restartVoiceCalibration'));
}

/** Leave calibration without changing any sensitivity. */
export async function stopVoiceCalibration() {
  return requireVoiceStatus(await callBridge('stopVoiceCalibration'));
}

/**
 * Receive the Voice status snapshots and events the Desktop pushes.
 *
 * `handler` gets `{type: 'status', status}` or `{type: 'event', event}` with
 * validated values; anything else is dropped. Returns a cleanup function.
 */
export function onDesktopVoicePush(handler) {
  if (typeof window === 'undefined') return () => {};
  const listener = (domEvent) => {
    const detail = domEvent?.detail;
    if (detail?.type === 'status') {
      const status = normalizeVoiceStatus(detail.status);
      if (status) handler({ type: 'status', status });
    } else if (detail?.type === 'event') {
      const event = normalizeVoiceEvent(detail.event);
      if (event) handler({ type: 'event', event });
    }
  };
  window.addEventListener(VOICE_PUSH_EVENT, listener);
  return () => window.removeEventListener(VOICE_PUSH_EVENT, listener);
}

function withTimeout(promise, timeoutMs) {
  let timer = null;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(
      () => reject(new Error('Desktop bridge timed out')),
      timeoutMs,
    );
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

/**
 * Check whether this Desktop page may open the microphone for Live voice.
 *
 * Resolves null when it may, or `desktop_restart_required` when the server
 * is not a secure context because it was added after the Desktop app started.
 * Bridge failures are logged and never block Live voice.
 */
export async function desktopMicrophoneAccess({
  timeoutMs = MICROPHONE_ACCESS_TIMEOUT_MS,
} = {}) {
  if (typeof window === 'undefined' || window.isSecureContext !== false)
    return null;
  let capabilities = null;
  try {
    if (await waitForDesktopBridge(timeoutMs))
      capabilities = await withTimeout(getDesktopCapabilities(), timeoutMs);
  } catch (error) {
    console.warn('Desktop capabilities unavailable for Live voice', error);
  }
  // Desktop makes every server it knows at startup a secure context.
  if (!capabilities?.secureOrigins.includes(window.location.origin))
    return 'desktop_restart_required';
  console.warn(
    'Desktop marked this server secure, but the page is not a secure context',
  );
  return null;
}

/**
 * Handle Live voice requests the Desktop pushes to this page (a wake phrase
 * with the Live voice action or the global hotkey).
 *
 * `handler({action, source})` receives `start` or `toggle` from `wakeword` or
 * `hotkey`; returning `false` reports the request as not handled. Returns a
 * cleanup function.
 */
export function onDesktopLiveRequest(handler) {
  if (typeof window === 'undefined') return () => {};
  const listener = (event) => {
    const action = event?.detail?.action;
    const source = event?.detail?.source;
    if (!LIVE_REQUEST_ACTIONS.has(action) || !LIVE_REQUEST_SOURCES.has(source))
      return;
    // The Desktop reads a cancelled event as "handled".
    if (handler({ action, source }) !== false) event.preventDefault();
  };
  window.addEventListener(LIVE_REQUEST_EVENT, listener);
  return () => window.removeEventListener(LIVE_REQUEST_EVENT, listener);
}

/**
 * Read the Desktop's global Live voice shortcut:
 * `{supported, enabled, hotkey: {ctrl, alt, shift, win, key}, error_code}`.
 */
export async function getDesktopLiveHotkey() {
  return callBridge('getLiveHotkey');
}

/**
 * Change the Live voice shortcut (`enabled` and/or the key combination); the
 * Desktop saves and registers it and answers with the resulting state.
 */
export async function setDesktopLiveHotkey(changes) {
  return callBridge('setLiveHotkey', changes);
}

const VOICE_CUES = Object.freeze({
  detected: [760],
  sent: [660, 880],
  cancelled: [520, 360],
  no_speech: [360],
  transcription_failed: [320, 260],
  command_failed: [320, 260],
  error: [260, 220],
});

/**
 * Play the short non-verbal cue of one Voice event kind inside the Desktop
 * WebView; kinds without a cue play nothing.
 * Failures are deliberately silent: visual state remains authoritative when
 * the host has no output device or its autoplay policy suspends Web Audio.
 */
export async function playVoiceCue(kind) {
  if (typeof window === 'undefined') return;
  const frequencies = VOICE_CUES[kind];
  if (!frequencies) return;
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) return;

  try {
    voiceAudioContext ||= new AudioContextClass();
    if (voiceAudioContext.state === 'suspended') {
      await voiceAudioContext.resume();
    }
    const start = voiceAudioContext.currentTime;
    frequencies.forEach((frequency, index) => {
      const oscillator = voiceAudioContext.createOscillator();
      const gain = voiceAudioContext.createGain();
      const cueStart = start + index * 0.12;
      oscillator.frequency.value = frequency;
      oscillator.type = 'sine';
      gain.gain.setValueAtTime(0.0001, cueStart);
      gain.gain.exponentialRampToValueAtTime(0.12, cueStart + 0.01);
      gain.gain.exponentialRampToValueAtTime(0.0001, cueStart + 0.09);
      oscillator.connect(gain);
      gain.connect(voiceAudioContext.destination);
      oscillator.start(cueStart);
      oscillator.stop(cueStart + 0.1);
    });
  } catch {
    // Visual status remains available.
  }
}
