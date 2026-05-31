/** Desktop capability detection and pywebview bridge client.

 * The Desktop accessor injects `window.pywebview.api` when the WebUI
 * loads inside pywebview with `?accessor=desktop` in the URL.
 */

const DESKTOP_ACCESSOR_PARAM = 'accessor=desktop';
const POLL_INTERVAL_MS = 500;

let cachedCapabilities = null;

/** True when the WebUI is loaded inside the vBot Desktop pywebview shell. */
export function isDesktop() {
  if (typeof window === 'undefined') {
    return false;
  }
  const hasAccessorParam = window.location.search.includes(
    DESKTOP_ACCESSOR_PARAM,
  );
  const hasBridgeApi = Boolean(window.pywebview?.api);
  return hasAccessorParam && hasBridgeApi;
}

/** Return whether the pywebview bridge is reachable. */
function bridgeAvailable() {
  return Boolean(window.pywebview?.api);
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
 * Returns { wakeword: false } when the bridge is absent, without caching.
 */
export async function getDesktopCapabilities() {
  if (!bridgeAvailable()) {
    return { wakeword: false };
  }
  if (cachedCapabilities) {
    return cachedCapabilities;
  }
  try {
    const caps = await callBridge('getDesktopCapabilities');
    cachedCapabilities = caps;
    return caps;
  } catch {
    return { wakeword: false };
  }
}

/** True when the bridge reports wakeword capability. */
export async function hasWakeword() {
  const caps = await getDesktopCapabilities();
  return Boolean(caps?.wakeword);
}

/** Fetch the current wakeword status from the bridge. */
export async function getWakewordStatus() {
  try {
    return await callBridge('getWakewordStatus');
  } catch {
    return { enabled: false, state: 'off' };
  }
}

/** Enable or disable wakeword listening. */
export async function setWakewordEnabled(enabled) {
  return callBridge('setWakewordEnabled', Boolean(enabled));
}

/** Apply a partial wakeword configuration update. */
export async function setWakewordConfig(config) {
  return callBridge('setWakewordConfig', config);
}

/**
 * Start a polling subscription for wakeword status changes.
 *
 * Calls `callback(status)` on every poll with the full status object.
 * Returns a cleanup function that stops the interval.
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

  let lastState = '';
  let running = true;

  const poll = async () => {
    if (!running) return;
    try {
      const status = await getWakewordStatus();
      if (running && status.state !== lastState) {
        lastState = status.state;
        callback(status);
      }
    } catch {
      // Bridge call failed, silently skip this poll cycle
    }
  };

  // Immediate first poll
  poll();

  const intervalId = setInterval(poll, intervalMs);

  return () => {
    running = false;
    clearInterval(intervalId);
  };
}
