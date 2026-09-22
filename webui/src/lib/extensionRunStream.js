import { reconnectBackoffDelay } from './backoff.js';

const HEARTBEAT_TIMEOUT_MS = 25_000;
const TERMINAL_EVENTS = new Set([
  'run_completed',
  'run_cancelled',
  'run_failed',
  'run_interrupted',
]);

// The Extension host owns this lifecycle; page code still owns its History
// projection and refreshes it through the existing invalidation bridge.
export function createExtensionRunStream({
  opened,
  afterSequence = 0,
  openRun,
  subscribeRunEvents,
  onEvent,
  onResync,
}) {
  let closed = false;
  let generation = 0;
  let cursor = afterSequence;
  let attempt = 0;
  let stream = null;
  let watchdog = null;
  let retry = null;

  const current = (value) => !closed && value === generation;

  function stopTransport() {
    clearTimeout(watchdog);
    clearTimeout(retry);
    stream?.close();
    stream = null;
  }

  function close() {
    closed = true;
    generation += 1;
    stopTransport();
  }

  function armWatchdog(value) {
    clearTimeout(watchdog);
    watchdog = setTimeout(() => recover(value), HEARTBEAT_TIMEOUT_MS);
  }

  function recover(value) {
    if (!current(value)) return;
    generation += 1;
    stopTransport();
    retry = setTimeout(
      reopen,
      reconnectBackoffDelay(attempt++, {
        initialDelayMs: 500,
        maxDelayMs: 5_000,
      }),
    );
  }

  function attach(result, value) {
    const url = result?.stream?.url;
    if (typeof url !== 'string' || !url.startsWith('/api/extension-runs/')) {
      close();
      onResync();
      return;
    }
    armWatchdog(value);
    stream = subscribeRunEvents(
      url,
      {
        onOpen: () => {
          if (current(value)) armWatchdog(value);
        },
        onHeartbeat: () => {
          if (!current(value)) return;
          attempt = 0;
          armWatchdog(value);
        },
        onError: () => recover(value),
        onEvent: (event) => {
          if (!current(value)) return;
          attempt = 0;
          armWatchdog(value);
          const sequence = event.data?.sequence;
          if (Number.isInteger(sequence)) {
            if (sequence <= cursor) return;
            cursor = sequence;
          }
          onEvent(event);
          if (TERMINAL_EVENTS.has(event.type)) close();
        },
      },
      { afterSequence: cursor },
    );
  }

  async function reopen() {
    if (closed) return;
    const value = ++generation;
    // Bound a stalled capability refresh as well as a silent EventSource.
    armWatchdog(value);
    try {
      const result = await openRun(cursor);
      if (!current(value)) return;
      attach(result, value);
      if (current(value)) onResync();
    } catch {
      recover(value);
    }
  }

  try {
    attach(opened, generation);
  } catch (error) {
    close();
    throw error;
  }
  return { close };
}
