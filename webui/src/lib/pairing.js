import { reconnectBackoffDelay } from './backoff.js';

// Pairing one browser: request a code, show it, poll until the owner approves
// or denies it on a signed-in device or the host, or until it expires.
//
// Phases: `starting`, `waiting` (`code`, `expiresAt`; `unstable` while polls
// fail), `approved`, `denied`, `expired`, `rate_limited` (`retryAt`; start
// again once it passed) and `failed` (`error`: the server's code). The poll
// secret lives only in this closure, never in state or storage.

export const PAIRING_STARTING = 'starting';
export const PAIRING_WAITING = 'waiting';
export const PAIRING_APPROVED = 'approved';
export const PAIRING_DENIED = 'denied';
export const PAIRING_EXPIRED = 'expired';
export const PAIRING_RATE_LIMITED = 'rate_limited';
export const PAIRING_FAILED = 'failed';

const DEFAULT_POLL_INTERVAL_S = 2;
const DEFAULT_EXPIRES_IN_S = 600;
const MAX_POLL_INTERVAL_S = 30;
const MAX_POLL_RETRY_DELAY_MS = 15_000;
const DEFAULT_RETRY_WAIT_S = 30;
const REQUEST_TIMEOUT_MS = 10_000;
// Polls normally see the server's `expired`; the local deadline ends a
// request whose polls keep failing.
const EXPIRY_GRACE_MS = 5_000;

export function createPairing({
  startPairing,
  pollPairing,
  deviceName,
  kind = 'browser',
  onChange = () => {},
  onApproved = () => {},
  // The server's access changed under the pairing screen: the mode is no
  // longer `authenticated`, or it refuses this client.
  onRecheck = () => {},
  now = () => Date.now(),
  setTimeoutFn = globalThis.setTimeout,
  clearTimeoutFn = globalThis.clearTimeout,
}) {
  let state = { phase: null };
  let generation = 0;
  let request = null;
  let pollIntervalMs = DEFAULT_POLL_INTERVAL_S * 1000;
  let pollFailures = 0;
  let pollTimer = null;
  let expiryTimer = null;
  let retryTimer = null;
  let activeRequest = null;
  let destroyed = false;

  function set(next) {
    state = next;
    onChange(state);
  }

  function clearTimers() {
    for (const timer of [pollTimer, expiryTimer, retryTimer]) {
      if (timer !== null) clearTimeoutFn(timer);
    }
    pollTimer = null;
    expiryTimer = null;
    retryTimer = null;
  }

  function end(next) {
    generation += 1;
    clearTimers();
    activeRequest?.cancel();
    request = null;
    set(next);
  }

  // A stalled response must leave time to retry a claim within the server's
  // claim grace. Cancellation also releases fetch when this screen goes away.
  function ask(endpoint, payload) {
    return new Promise((resolve, reject) => {
      const controller = new AbortController();
      let settled = false;
      const operation = {
        cancel: () => finish(reject, requestFailure('cancelled'), true),
      };
      const timer = setTimeoutFn(
        () => finish(reject, requestFailure('timed out'), true),
        REQUEST_TIMEOUT_MS,
      );
      activeRequest = operation;

      function finish(complete, value, abort = false) {
        if (settled) return;
        settled = true;
        clearTimeoutFn(timer);
        if (activeRequest === operation) activeRequest = null;
        if (abort) controller.abort();
        complete(value);
      }

      Promise.resolve()
        .then(() => {
          if (!settled) return endpoint(payload, { signal: controller.signal });
        })
        .then(
          (answer) => finish(resolve, answer),
          (error) => finish(reject, error),
        );
    });
  }

  function seconds(value, fallback, max = Number.POSITIVE_INFINITY) {
    return Number.isFinite(value) && value > 0
      ? Math.min(value, max)
      : fallback;
  }

  async function start() {
    if (
      destroyed ||
      state.phase === PAIRING_STARTING ||
      state.phase === PAIRING_WAITING ||
      state.phase === PAIRING_APPROVED
    ) {
      return;
    }
    if (now() < state.retryAt) {
      return;
    }
    const current = ++generation;
    clearTimers();
    request = null;
    set({ phase: PAIRING_STARTING });
    let answer;
    try {
      answer = await ask(startPairing, { kind, name: deviceName });
    } catch (error) {
      if (current === generation) refused(error, { starting: true });
      return;
    }
    if (current !== generation) {
      return;
    }
    if (
      typeof answer?.code !== 'string' ||
      typeof answer.request_id !== 'string' ||
      typeof answer.poll_secret !== 'string'
    ) {
      end({ phase: PAIRING_FAILED, error: 'invalid_response' });
      return;
    }
    request = { id: answer.request_id, secret: answer.poll_secret };
    pollIntervalMs =
      seconds(
        answer.poll_interval,
        DEFAULT_POLL_INTERVAL_S,
        MAX_POLL_INTERVAL_S,
      ) * 1000;
    pollFailures = 0;
    const expiresAt =
      now() + seconds(answer.expires_in, DEFAULT_EXPIRES_IN_S) * 1000;
    set({
      phase: PAIRING_WAITING,
      code: answer.code,
      expiresAt,
      unstable: false,
    });
    armExpiry(expiresAt);
    schedulePoll(pollIntervalMs);
  }

  function armExpiry(expiresAt) {
    if (expiryTimer !== null) clearTimeoutFn(expiryTimer);
    expiryTimer = setTimeoutFn(
      () => {
        expiryTimer = null;
        end({ phase: PAIRING_EXPIRED });
      },
      Math.max(0, expiresAt - now()) + EXPIRY_GRACE_MS,
    );
  }

  function schedulePoll(delay) {
    if (pollTimer !== null) clearTimeoutFn(pollTimer);
    pollTimer = setTimeoutFn(() => {
      pollTimer = null;
      void poll();
    }, delay);
  }

  async function poll() {
    const current = generation;
    if (!request) {
      return;
    }
    let answer;
    try {
      answer = await ask(pollPairing, {
        requestId: request.id,
        pollSecret: request.secret,
      });
    } catch (error) {
      if (current === generation) refused(error, { starting: false });
      return;
    }
    if (current !== generation) {
      return;
    }
    switch (answer?.status) {
      case 'pending': {
        pollFailures = 0;
        const expiresAt = Number.isFinite(answer.expires_in)
          ? now() + Math.max(0, answer.expires_in) * 1000
          : state.expiresAt;
        // The shown deadline moves only by what the minute display can show.
        const shownExpiry =
          Math.abs(expiresAt - state.expiresAt) > 30_000
            ? expiresAt
            : state.expiresAt;
        if (state.unstable || shownExpiry !== state.expiresAt) {
          set({ ...state, expiresAt: shownExpiry, unstable: false });
        }
        armExpiry(expiresAt);
        schedulePoll(pollIntervalMs);
        return;
      }
      case 'approved':
        end({ phase: PAIRING_APPROVED, device: answer.device ?? null });
        onApproved(answer.device ?? null);
        return;
      case 'denied':
        end({ phase: PAIRING_DENIED });
        return;
      case 'expired':
        end({ phase: PAIRING_EXPIRED });
        return;
      default:
        end({ phase: PAIRING_FAILED, error: 'invalid_response' });
    }
  }

  function refused(error, { starting }) {
    const code = typeof error?.code === 'string' ? error.code : 'network_error';
    if (code === 'auth_disabled' || code === 'access_denied') {
      end({ phase: null });
      onRecheck();
      return;
    }
    if (code === 'rate_limited') {
      const waitMs = seconds(error.retryAfter, DEFAULT_RETRY_WAIT_S) * 1000;
      if (!starting) {
        // Polled too fast: wait as told, then keep polling.
        schedulePoll(waitMs);
        return;
      }
      waitBeforeRestart({ phase: PAIRING_RATE_LIMITED }, waitMs);
      return;
    }
    if (code === 'devices_unavailable') {
      waitBeforeRestart(
        { phase: PAIRING_FAILED, error: code },
        seconds(error.retryAfter, DEFAULT_RETRY_WAIT_S) * 1000,
      );
      return;
    }
    if (!starting && isTransient(error)) {
      // The server may be restarting or the network flapping: keep polling
      // with a backoff until the request expires.
      const delay = reconnectBackoffDelay(pollFailures, {
        initialDelayMs: pollIntervalMs,
        maxDelayMs: MAX_POLL_RETRY_DELAY_MS,
      });
      pollFailures += 1;
      if (!state.unstable) set({ ...state, unstable: true });
      schedulePoll(delay);
      return;
    }
    end({ phase: PAIRING_FAILED, error: code });
  }

  function waitBeforeRestart(next, waitMs) {
    end({ ...next, retryAt: now() + waitMs, canRetry: false });
    retryTimer = setTimeoutFn(() => {
      retryTimer = null;
      set({ ...state, canRetry: true });
    }, waitMs);
  }

  return {
    get state() {
      return state;
    },
    start,
    destroy() {
      destroyed = true;
      generation += 1;
      clearTimers();
      activeRequest?.cancel();
      request = null;
    },
  };
}

function requestFailure(reason) {
  return Object.assign(new Error(`Pairing request ${reason}`), {
    code: 'network_error',
  });
}

function isTransient(error) {
  const status = error?.status;
  return (
    error?.code === 'network_error' ||
    error?.code === 'invalid_rpc_response' ||
    (Number.isInteger(status) &&
      status >= 500 &&
      error?.code !== 'devices_unavailable')
  );
}

// The browser and operating system this page runs in, for the name a new
// browser Device gets. Either is '' when it cannot be told.
export function describeBrowserDevice(nav = globalThis.navigator) {
  const agent = typeof nav?.userAgent === 'string' ? nav.userAgent : '';
  const platform =
    typeof nav?.userAgentData?.platform === 'string'
      ? nav.userAgentData.platform
      : '';
  return { browser: browserName(agent), os: osName(platform, agent) };
}

function browserName(agent) {
  const browsers = [
    [/\bEdgA?\//, 'Edge'],
    [/\bOPR\//, 'Opera'],
    [/\bVivaldi\//, 'Vivaldi'],
    [/\bSamsungBrowser\//, 'Samsung Internet'],
    [/\b(?:Firefox|FxiOS)\//, 'Firefox'],
    [/\b(?:Chrome|CriOS|Chromium)\//, 'Chrome'],
    [/\bVersion\/[\d.]+.*\bSafari\//, 'Safari'],
  ];
  return browsers.find(([pattern]) => pattern.test(agent))?.[1] ?? '';
}

function osName(platform, agent) {
  const platforms = {
    Windows: 'Windows',
    macOS: 'macOS',
    Linux: 'Linux',
    Android: 'Android',
    'Chrome OS': 'ChromeOS',
    ChromeOS: 'ChromeOS',
    iOS: 'iOS',
  };
  if (platforms[platform]) {
    return platforms[platform];
  }
  const systems = [
    [/\bWindows\b/, 'Windows'],
    [/\b(?:iPhone|iPod)\b/, 'iOS'],
    [/\biPad\b/, 'iPadOS'],
    [/\bAndroid\b/, 'Android'],
    [/\bCrOS\b/, 'ChromeOS'],
    [/\bMac OS X\b|\bMacintosh\b/, 'macOS'],
    [/\bLinux\b/, 'Linux'],
  ];
  return systems.find(([pattern]) => pattern.test(agent))?.[1] ?? '';
}
