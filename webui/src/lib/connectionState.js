import { subscribeServerEvents } from './api.js';
import { reconnectBackoffDelay } from './backoff.js';

export const CONNECTION_STATUS_CONNECTED = 'connected';
export const CONNECTION_STATUS_RECONNECTING = 'reconnecting';
export const CONNECTION_STATUS_DISCONNECTED = 'disconnected';
export const CONNECTION_REPLAY_STATUS_RESUMED = 'resumed';
export const CONNECTION_REPLAY_STATUS_GAP = 'gap';
export const CONNECTION_REPLAY_STATUS_EPOCH_CHANGED = 'epoch_changed';

const RECONNECT_INITIAL_DELAY_MS = 1000;
const RECONNECT_MAX_DELAY_MS = 30000;
const WS_HEARTBEAT_TIMEOUT_MS = 60000;

// The public state carries only what callers read; the socket, timers and
// backoff bookkeeping stay private to this module, keyed by that state object.
const transports = new WeakMap();

function transportOf(state) {
  let transport = transports.get(state);
  if (!transport) {
    transport = {
      connection: null,
      reconnectTimer: null,
      reconnectAttempt: 0,
      heartbeatTimer: null,
      lastEventAt: 0,
    };
    transports.set(state, transport);
  }
  return transport;
}

export function createConnectionState() {
  return {
    status: CONNECTION_STATUS_RECONNECTING,
    lastSequence: 0,
    epoch: '',
  };
}

export function connect(state, handlers = {}) {
  _cleanup(state);

  const afterSequence = state.lastSequence;
  const resumeEpoch = state.epoch;
  let connection;
  try {
    connection = subscribeServerEvents(
      {
        onOpen: () => {
          state.status = CONNECTION_STATUS_CONNECTED;
          transportOf(state).reconnectAttempt = 0;
          handlers.onStatusChange?.();
          _armHeartbeatWatchdog(state);
        },
        onClose: () => {
          _clearHeartbeatWatchdog(state);
          _cleanup(state);
          state.status = CONNECTION_STATUS_DISCONNECTED;
          handlers.onStatusChange?.();
          _scheduleReconnect(state, handlers);
        },
        onEvent: (event) => {
          if (event.type === 'heartbeat') {
            _armHeartbeatWatchdog(state);
            return;
          }
          _armHeartbeatWatchdog(state);
          if (event.type === 'connection_ready') {
            const nextEpoch = event.epoch ?? '';
            const isReplayResume = event.replay_status
              ? event.replay_status === CONNECTION_REPLAY_STATUS_RESUMED
              : afterSequence > 0 &&
                typeof nextEpoch === 'string' &&
                nextEpoch.length > 0 &&
                nextEpoch === resumeEpoch;
            state.epoch = nextEpoch;
            if (!isReplayResume) {
              state.lastSequence = Number.isFinite(event.last_sequence)
                ? event.last_sequence
                : 0;
            }
            handlers.onEvent?.(event);
            return;
          }
          if (event.sequence > state.lastSequence) {
            state.lastSequence = event.sequence;
          }
          handlers.onEvent?.(event);
        },
      },
      {
        WebSocket: handlers._WebSocket,
        baseUrl: handlers._baseUrl,
        afterSequence,
        epoch: state.epoch,
      },
    );
  } catch (error) {
    state.status = CONNECTION_STATUS_DISCONNECTED;
    handlers.onStatusChange?.();
    handlers.onError?.(error);
    _scheduleReconnect(state, handlers);
    return;
  }

  transportOf(state).connection = connection;
  _armHeartbeatWatchdog(state);
}

export function disconnect(state) {
  _cleanup(state);
  state.status = CONNECTION_STATUS_DISCONNECTED;
}

function _cleanup(state) {
  const transport = transportOf(state);
  if (transport.reconnectTimer) {
    clearTimeout(transport.reconnectTimer);
    transport.reconnectTimer = null;
  }
  _clearHeartbeatWatchdog(state);
  if (transport.connection) {
    transport.connection.close();
    transport.connection = null;
  }
}

function _armHeartbeatWatchdog(state) {
  _clearHeartbeatWatchdog(state);
  const transport = transportOf(state);
  transport.lastEventAt = Date.now();
  transport.heartbeatTimer = setTimeout(() => {
    transport.heartbeatTimer = null;
    if (transport.connection) {
      try {
        // Close the underlying socket so subscribeServerEvents keeps its close
        // listener installed. Its wrapper close() intentionally removes that
        // listener for a user-requested disconnect, which is the wrong
        // lifecycle for a stalled connection that must reconnect.
        transport.connection.socket.close();
      } catch {
        // Close is best-effort; onClose will schedule reconnect.
      }
    }
  }, WS_HEARTBEAT_TIMEOUT_MS);
}

export function handleVisibilityChange(state) {
  if (
    typeof document === 'undefined' ||
    document.visibilityState !== 'visible'
  ) {
    return;
  }
  if (state.status !== CONNECTION_STATUS_CONNECTED) {
    return;
  }
  const transport = transportOf(state);
  const elapsed = Date.now() - transport.lastEventAt;
  if (elapsed > WS_HEARTBEAT_TIMEOUT_MS / 2) {
    if (transport.connection) {
      try {
        // See _armHeartbeatWatchdog: this is a recovery close, not an
        // intentional disconnect, so onClose must schedule a reconnect.
        transport.connection.socket.close();
      } catch {
        // Best-effort; onClose will schedule reconnect.
      }
    }
  }
}

function _clearHeartbeatWatchdog(state) {
  const transport = transportOf(state);
  if (transport.heartbeatTimer) {
    clearTimeout(transport.heartbeatTimer);
    transport.heartbeatTimer = null;
  }
}

function _scheduleReconnect(state, handlers) {
  const transport = transportOf(state);
  const delay = reconnectBackoffDelay(transport.reconnectAttempt, {
    initialDelayMs: RECONNECT_INITIAL_DELAY_MS,
    maxDelayMs: RECONNECT_MAX_DELAY_MS,
  });
  transport.reconnectAttempt += 1;
  transport.reconnectTimer = setTimeout(() => {
    transport.reconnectTimer = null;
    connect(state, handlers);
  }, delay);
}
