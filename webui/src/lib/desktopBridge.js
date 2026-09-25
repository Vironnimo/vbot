/** Desktop capability detection and pywebview bridge client.

 * The Desktop accessor injects `window.pywebview.api` when the WebUI
 * loads inside pywebview with `?accessor=desktop` in the URL.
 */

const POLL_INTERVAL_MS = 500;
const BRIDGE_READY_EVENT = 'pywebviewready';
const BRIDGE_READY_TIMEOUT_MS = 5000;
const LIVE_REQUEST_EVENT = 'vbot-desktop-live';
const LIVE_REQUEST_ACTIONS = new Set(['start', 'toggle']);
const LIVE_REQUEST_SOURCES = new Set(['wakeword', 'hotkey']);
// A Live voice start never waits longer than this for the Desktop to pause
// wakeword listening; the microphone is shared, so a slow bridge only delays.
const LIVE_VOICE_LEASE_TIMEOUT_MS = 3000;
// A bridge call that never answers must not stall later state changes.
const LIVE_VOICE_SYNC_CALL_TIMEOUT_MS = 10000;
const DISABLED_DESKTOP_CAPABILITIES = Object.freeze({
  wakeword: false,
  serverSelection: false,
  contextMenu: false,
  liveWakeword: false,
  liveHotkey: false,
  secureOrigins: Object.freeze([]),
});

let cachedCapabilities = null;
let cachedBridgeApi = null;
let voiceAudioContext = null;
// Whether this page holds the microphone for Live voice, and the value the
// Desktop last confirmed (null: unknown, so the next sync sends it).
let desiredLiveVoiceActive = false;
let confirmedLiveVoiceActive = null;
let liveVoiceActiveSync = null;

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
    serverSelection: Boolean(caps?.serverSelection),
    contextMenu: Boolean(caps?.contextMenu),
    liveWakeword: Boolean(caps?.liveWakeword),
    liveHotkey: Boolean(caps?.liveHotkey),
    secureOrigins: Array.isArray(caps?.secureOrigins)
      ? caps.secureOrigins.filter((origin) => typeof origin === 'string')
      : [],
  };
  cachedBridgeApi = window.pywebview.api;
  return cachedCapabilities;
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

/** Fetch the current wakeword status from the bridge. */
export async function getWakewordStatus() {
  return callBridge('getWakewordStatus');
}

/** Enable or disable wakeword listening. */
export async function setWakewordEnabled(enabled) {
  return callBridge('setWakewordEnabled', Boolean(enabled));
}

/** Apply a partial wakeword configuration update. */
export async function setWakewordConfig(config) {
  return callBridge('setWakewordConfig', config);
}

/** Enumerate Desktop-local microphone devices and compatibility. */
export async function listMicrophones() {
  const devices = await callBridge('listMicrophones');
  return Array.isArray(devices) ? devices : [];
}

/** Enumerate curated and imported Desktop-local wakeword models. */
export async function listWakewordModels() {
  const models = await callBridge('listWakewordModels');
  return Array.isArray(models) ? models : [];
}

/** Validate and install one user-selected TFLite wakeword model. */
export async function importWakewordModel(filename, contentBase64) {
  return callBridge('importWakewordModel', filename, contentBase64);
}

/** Permanently remove one inactive imported wakeword model. */
export async function deleteWakewordModel(modelId) {
  return callBridge('deleteWakewordModel', modelId);
}

/** Retry the enabled worker after an actionable error. */
export async function retryWakeword() {
  return callBridge('retryWakeword');
}

/** Stop the active voice recording and send what was captured so far. */
export async function stopWakewordRecording() {
  return callBridge('stopWakewordRecording');
}

/** Enter transient detector calibration without recording or sending commands. */
export async function startWakewordCalibration() {
  return callBridge('startWakewordCalibration');
}

/** Leave detector calibration and resume normal wakeword activation. */
export async function stopWakewordCalibration() {
  return callBridge('stopWakewordCalibration');
}

/** Restart guided calibration from ambient-noise measurement. */
export async function restartWakewordCalibration() {
  return callBridge('restartWakewordCalibration');
}

/** Retry calibration for one specific model, discarding only its samples. */
export async function retryWakewordModelCalibration(modelId) {
  return callBridge('retryWakewordModelCalibration', modelId);
}

/**
 * Tell the Desktop whether a Live voice call holds the microphone, so it
 * pauses wakeword listening meanwhile.
 *
 * Calls are serialized and only the latest value is sent, so a quick
 * start/stop can never leave the Desktop with a stale state. Rejects with the
 * bridge failure when the latest value could not be delivered.
 */
export function setDesktopLiveVoiceActive(active) {
  desiredLiveVoiceActive = active === true;
  return syncDesktopLiveVoiceActive();
}

/**
 * Send this page's Live voice state to the Desktop unless it already has it.
 * A new page calls this once the bridge is discovered, which also ends a pause
 * left behind by a previous page.
 */
export function syncDesktopLiveVoiceActive() {
  liveVoiceActiveSync ??= (async () => {
    // Yield first: the promise must be stored before this body can finish.
    await null;
    let failure = null;
    try {
      while (confirmedLiveVoiceActive !== desiredLiveVoiceActive) {
        const active = desiredLiveVoiceActive;
        try {
          await withTimeout(
            callBridge('setLiveVoiceActive', active),
            LIVE_VOICE_SYNC_CALL_TIMEOUT_MS,
          );
          confirmedLiveVoiceActive = active;
          failure = null;
        } catch (error) {
          confirmedLiveVoiceActive = null;
          failure = error;
          if (active === desiredLiveVoiceActive) break;
        }
      }
    } finally {
      liveVoiceActiveSync = null;
    }
    if (failure) throw failure;
  })();
  return liveVoiceActiveSync;
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
 * Create the Desktop microphone lease for Live voice.
 *
 * `acquire()` resolves `null` when the call may open the microphone, after
 * asking the Desktop to pause wakeword listening, or a Live voice notice code
 * when it cannot: `desktop_restart_required` when this server is not a secure
 * context because it was added after the Desktop app started. Bridge failures
 * are logged and never block Live voice. Pair every successful acquisition
 * with one `release()`; wakeword resumes after the last holder releases.
 */
export function createDesktopLiveVoiceLease({
  timeoutMs = LIVE_VOICE_LEASE_TIMEOUT_MS,
} = {}) {
  async function capabilities() {
    try {
      if (!(await waitForDesktopBridge(timeoutMs))) return null;
      return await withTimeout(getDesktopCapabilities(), timeoutMs);
    } catch (error) {
      console.warn('Desktop capabilities unavailable for Live voice', error);
      return null;
    }
  }

  // Whether this lease asked the Desktop to pause, so release resumes it.
  let paused = false;
  let holders = 0;

  return {
    async acquire() {
      const caps = await capabilities();
      if (window.isSecureContext === false) {
        // Desktop makes every server it knows at startup a secure context.
        if (!caps?.secureOrigins.includes(window.location.origin))
          return 'desktop_restart_required';
        console.warn(
          'Desktop marked this server secure, but the page is not a secure context',
        );
      }
      // A cancelled start can finish acquiring after a newer call has started.
      // Its release must not resume wakeword while that call holds the lease.
      holders += 1;
      if (!caps?.liveWakeword) return null;
      paused = true;
      try {
        await withTimeout(setDesktopLiveVoiceActive(true), timeoutMs);
      } catch (error) {
        console.warn('Desktop could not pause wakeword for Live voice', error);
      }
      return null;
    },
    release() {
      if (holders === 0) return;
      holders -= 1;
      if (holders > 0) return;
      if (!paused) return;
      paused = false;
      setDesktopLiveVoiceActive(false).catch((error) => {
        console.warn(
          'Desktop could not resume wakeword after Live voice',
          error,
        );
      });
    },
  };
}

/**
 * Handle Live voice requests the Desktop pushes to this page (a wakeword
 * model with the Live voice action or the global hotkey).
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

/**
 * Play a short non-verbal Voice cue inside the Desktop WebView.
 * Failures are deliberately silent: visual state remains authoritative when
 * the host has no output device or its autoplay policy suspends Web Audio.
 */
export async function playWakewordCue(state) {
  if (typeof window === 'undefined') return;
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) return;

  try {
    voiceAudioContext ||= new AudioContextClass();
    if (voiceAudioContext.state === 'suspended') {
      await voiceAudioContext.resume();
    }
    const patterns = {
      wakeword_detected: [760],
      sent: [660, 880],
      cancelled: [520, 360],
      no_speech: [360],
      transcription_failed: [320, 260],
      error: [260, 220],
    };
    const frequencies = patterns[state];
    if (!frequencies) return;
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

/**
 * Start a polling subscription for wakeword status changes.
 *
 * Calls `callback(status)` on every poll with the full status object.
 * Returns a cleanup function that stops future polls.
 *
 * @param {Function} callback — receives the full wakeword status object.
 * @param {number} [intervalMs=500]
 * @returns {Function} cleanup — call to stop polling.
 */
export function onWakewordStatusChange(
  callback,
  intervalMs = POLL_INTERVAL_MS,
) {
  if (!isDesktop()) {
    return () => {};
  }

  let lastStatusKey = '';
  let running = true;
  let timeoutId = null;

  const poll = async () => {
    if (!running) return;
    try {
      const status = await getWakewordStatus();
      const statusKey = JSON.stringify(status);
      if (running && statusKey !== lastStatusKey) {
        lastStatusKey = statusKey;
        callback(status);
      }
    } catch {
      // Bridge call failed, silently skip this poll cycle
    } finally {
      if (running) {
        timeoutId = setTimeout(poll, intervalMs);
      }
    }
  };

  // Immediate first poll
  void poll();

  return () => {
    running = false;
    if (timeoutId !== null) {
      clearTimeout(timeoutId);
      timeoutId = null;
    }
  };
}
