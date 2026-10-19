import { reconnectBackoffDelay } from './backoff.js';

// The boot gate's state machine. Before the app mounts, and again whenever a
// transport reports an access loss, it asks the server (`/api/auth/status`)
// what this page may do and names the screen to show:
//
// - `app`: mount the app.
// - `pairing`: `authenticated` mode without a principal; pair this browser.
// - `denied`: the server refuses this client (403 `access_denied`).
// - `unavailable`: the server cannot check signed-in Devices right now (503
//   `devices_unavailable`); this browser's sign-in stays. It retries on its
//   own, no sooner than the server's `Retry-After`, and offers Retry.
// - `unreachable`: after an access loss the status check failed; it retries
//   on its own with a backoff and offers Retry.
// - `looping`: the app lost access again and again right after the status
//   check let it in; it stops and offers Retry instead of remounting forever.
// - `checking`: the status check runs.
//
// The first check at boot mounts the app when the server cannot be asked, so
// the app shows its usual offline state and reconnects; the server is the
// security boundary either way.

export const GATE_CHECKING = 'checking';
export const GATE_APP = 'app';
export const GATE_PAIRING = 'pairing';
export const GATE_DENIED = 'denied';
export const GATE_UNAVAILABLE = 'unavailable';
export const GATE_UNREACHABLE = 'unreachable';
export const GATE_LOOPING = 'looping';

// Shown on the pairing screen when an approved browser still has no access:
// it did not keep the device cookie.
export const GATE_NOTICE_NOT_KEPT = 'not_kept';

const STATUS_TIMEOUT_MS = 10_000;
const RETRY_INITIAL_DELAY_MS = 1_000;
const RETRY_MAX_DELAY_MS = 30_000;
const DEVICES_UNAVAILABLE_RETRY_S = 30;
const LOSS_LOOP_LIMIT = 3;
const LOSS_LOOP_WINDOW_MS = 60_000;

// The screen one `/api/auth/status` answer calls for.
export function decideGate(status) {
  if (status?.authenticated === true) {
    return GATE_APP;
  }
  return status?.mode === 'authenticated' ? GATE_PAIRING : GATE_APP;
}

export function createAccessGate({
  getAuthStatus,
  onAccessLoss,
  onChange = () => {},
  // Runs before the app is taken down (for example to keep unsent drafts).
  beforeLeaveApp = () => {},
  now = () => Date.now(),
  setTimeoutFn = globalThis.setTimeout,
  clearTimeoutFn = globalThis.clearTimeout,
}) {
  let state = { screen: GATE_CHECKING, status: null, notice: null };
  let generation = 0;
  let retryTimer = null;
  let retryAttempt = 0;
  let losses = [];
  let stopListening = null;
  let destroyed = false;
  let retryNotBefore = 0;

  function show(next) {
    state = { status: null, notice: null, retryAt: null, ...next };
    onChange(state);
  }

  function clearRetry() {
    if (retryTimer !== null) {
      clearTimeoutFn(retryTimer);
      retryTimer = null;
    }
  }

  function askServer() {
    return new Promise((resolve, reject) => {
      const controller =
        typeof AbortController === 'function' ? new AbortController() : null;
      const timer = setTimeoutFn(() => {
        controller?.abort();
        reject(
          Object.assign(new Error('Status check timed out'), {
            code: 'network_error',
          }),
        );
      }, STATUS_TIMEOUT_MS);
      Promise.resolve()
        .then(() => getAuthStatus({ signal: controller?.signal }))
        .then(resolve, reject)
        .finally(() => clearTimeoutFn(timer));
    });
  }

  // The check's reason selects its one exception: boot may mount the app
  // offline; an approval that still needs pairing reports a lost cookie.
  async function check(reason = 'recheck') {
    if (destroyed) {
      return;
    }
    const current = ++generation;
    clearRetry();
    show({ screen: GATE_CHECKING });
    let status;
    try {
      status = await askServer();
    } catch (error) {
      if (current !== generation || destroyed) {
        return;
      }
      if (error?.code === 'access_denied') {
        show({ screen: GATE_DENIED });
      } else if (error?.code === 'devices_unavailable') {
        scheduleRetry(
          GATE_UNAVAILABLE,
          error.retryAfter ?? DEVICES_UNAVAILABLE_RETRY_S,
        );
      } else if (reason === 'boot') {
        show({ screen: GATE_APP });
      } else {
        scheduleRetry(GATE_UNREACHABLE);
      }
      return;
    }
    if (current !== generation || destroyed) {
      return;
    }
    retryAttempt = 0;
    const screen = decideGate(status);
    show({
      screen,
      status,
      notice:
        reason === 'pairing-approved' && screen === GATE_PAIRING
          ? GATE_NOTICE_NOT_KEPT
          : null,
    });
  }

  // Shows `screen` and checks again after a backoff, or after `retryAfter`
  // seconds when the server asked for a longer wait.
  function scheduleRetry(screen, retryAfter = null) {
    retryNotBefore = now() + (retryAfter ?? 0) * 1000;
    show({ screen, retryAt: retryAfter > 0 ? retryNotBefore : null });
    const backoff = reconnectBackoffDelay(retryAttempt, {
      initialDelayMs: RETRY_INITIAL_DELAY_MS,
      maxDelayMs: RETRY_MAX_DELAY_MS,
    });
    const delay = Math.max(backoff, (retryAfter ?? 0) * 1000);
    retryAttempt += 1;
    retryTimer = setTimeoutFn(() => {
      retryTimer = null;
      void check();
    }, delay);
  }

  function handleLoss(reason, retryAfter) {
    // Only the mounted app can lose access; the gate's own screens handle
    // their refusals themselves.
    if (destroyed || state.screen !== GATE_APP) {
      return;
    }
    beforeLeaveApp();
    if (reason === 'devices_unavailable') {
      generation += 1;
      clearRetry();
      scheduleRetry(
        GATE_UNAVAILABLE,
        retryAfter ?? DEVICES_UNAVAILABLE_RETRY_S,
      );
      return;
    }
    const at = now();
    losses = [...losses.filter((time) => at - time < LOSS_LOOP_WINDOW_MS), at];
    if (losses.length >= LOSS_LOOP_LIMIT) {
      generation += 1;
      clearRetry();
      show({ screen: GATE_LOOPING });
      return;
    }
    void check();
  }

  return {
    get state() {
      return state;
    },
    start() {
      stopListening ??= onAccessLoss(handleLoss);
      return check('boot');
    },
    // Retry from `unavailable`, `unreachable`, `looping` or `denied`.
    retry() {
      if (now() < retryNotBefore) return;
      losses = [];
      retryAttempt = 0;
      return check();
    },
    // The pairing screen saw its request approved.
    pairingApproved() {
      return check('pairing-approved');
    },
    // The pairing screen learned the server's access changed (the mode is no
    // longer `authenticated`, or this client is refused).
    recheck() {
      return check();
    },
    destroy() {
      destroyed = true;
      generation += 1;
      clearRetry();
      stopListening?.();
      stopListening = null;
    },
  };
}
