/** Desktop capability detection and pywebview bridge client.

 * The Desktop accessor injects `window.pywebview.api` when the WebUI
 * loads inside pywebview with `?accessor=desktop` in the URL.
 */

const POLL_INTERVAL_MS = 500;
const BRIDGE_READY_EVENT = 'pywebviewready';
const BRIDGE_READY_TIMEOUT_MS = 5000;
const DISABLED_DESKTOP_CAPABILITIES = Object.freeze({
  wakeword: false,
  serverSelection: false,
  contextMenu: false,
});

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
