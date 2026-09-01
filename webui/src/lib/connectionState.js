import { subscribeServerEvents } from './api.js';
import { reconnectBackoffDelay } from './backoff.js';

export const CONNECTION_STATUS_CONNECTED = 'connected';
export const CONNECTION_STATUS_RECONNECTING = 'reconnecting';
export const CONNECTION_STATUS_DISCONNECTED = 'disconnected';
export const CONNECTION_REPLAY_STATUS_FRESH = 'fresh';
export const CONNECTION_REPLAY_STATUS_RESUMED = 'resumed';
export const CONNECTION_REPLAY_STATUS_GAP = 'gap';
export const CONNECTION_REPLAY_STATUS_EPOCH_CHANGED = 'epoch_changed';

const RECONNECT_INITIAL_DELAY_MS = 1000;
const RECONNECT_MAX_DELAY_MS = 30000;
const WS_HEARTBEAT_TIMEOUT_MS = 60000;

export function createConnectionState() {
  return {
    status: CONNECTION_STATUS_RECONNECTING,
    lastSequence: 0,
    epoch: '',
    _connection: null,
    _reconnectTimer: null,
    _reconnectAttempt: 0,
    _heartbeatTimer: null,
    _lastEventAt: 0,
  };
}

export function connect(state, handlers = {}) {
  _cleanup(state);

  const afterSequence = state.lastSequence;
  const resumeEpoch = state.epoch;
  const connection = subscribeServerEvents(
    {
      onOpen: () => {
        state.status = CONNECTION_STATUS_CONNECTED;
        state._reconnectAttempt = 0;
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

  state._connection = connection;
  _armHeartbeatWatchdog(state);
}

export function disconnect(state) {
  _cleanup(state);
  state.status = CONNECTION_STATUS_DISCONNECTED;
}

function _cleanup(state) {
  if (state._reconnectTimer) {
    clearTimeout(state._reconnectTimer);
    state._reconnectTimer = null;
  }
  _clearHeartbeatWatchdog(state);
  if (state._connection) {
    state._connection.close();
    state._connection = null;
  }
}

function _armHeartbeatWatchdog(state) {
  _clearHeartbeatWatchdog(state);
  state._lastEventAt = Date.now();
  state._heartbeatTimer = setTimeout(() => {
    state._heartbeatTimer = null;
    if (state._connection) {
      try {
        // Close the underlying socket so subscribeServerEvents keeps its close
        // listener installed. Its wrapper close() intentionally removes that
        // listener for a user-requested disconnect, which is the wrong
        // lifecycle for a stalled connection that must reconnect.
        state._connection.socket.close();
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
  const elapsed = Date.now() - (state._lastEventAt || 0);
  if (elapsed > WS_HEARTBEAT_TIMEOUT_MS / 2) {
    if (state._connection) {
      try {
        // See _armHeartbeatWatchdog: this is a recovery close, not an
        // intentional disconnect, so onClose must schedule a reconnect.
        state._connection.socket.close();
      } catch {
        // Best-effort; onClose will schedule reconnect.
      }
    }
  }
}

function _clearHeartbeatWatchdog(state) {
  if (state._heartbeatTimer) {
    clearTimeout(state._heartbeatTimer);
    state._heartbeatTimer = null;
  }
}

function _scheduleReconnect(state, handlers) {
  const delay = reconnectBackoffDelay(state._reconnectAttempt, {
    initialDelayMs: RECONNECT_INITIAL_DELAY_MS,
    maxDelayMs: RECONNECT_MAX_DELAY_MS,
  });
  state._reconnectAttempt += 1;
  state._reconnectTimer = setTimeout(() => {
    state._reconnectTimer = null;
    connect(state, handlers);
  }, delay);
}
