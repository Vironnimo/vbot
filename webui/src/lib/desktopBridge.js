/** Desktop capability detection and pywebview bridge client.

 * The Desktop accessor injects `window.pywebview.api` when the WebUI
 * loads inside pywebview with `?accessor=desktop` in the URL.
 */

const POLL_INTERVAL_MS = 500;
const BRIDGE_READY_EVENT = 'pywebviewready';
const BRIDGE_READY_TIMEOUT_MS = 5000;

let cachedCapabilities = null;
let cachedBridgeApi = null;

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
 * Returns { wakeword: false } when the bridge is absent, without caching.
 */
export async function getDesktopCapabilities() {
  if (!bridgeAvailable()) {
    return { wakeword: false };
  }
  if (cachedCapabilities && cachedBridgeApi === window.pywebview.api) {
    return cachedCapabilities;
  }
  try {
    const caps = await callBridge('getDesktopCapabilities');
    cachedCapabilities = caps;
    cachedBridgeApi = window.pywebview.api;
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

  let lastStatusKey = '';
  let running = true;

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
