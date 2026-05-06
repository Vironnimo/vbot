import { subscribeServerEvents } from './api.js';

export const CONNECTION_STATUS_CONNECTED = 'connected';
export const CONNECTION_STATUS_RECONNECTING = 'reconnecting';
export const CONNECTION_STATUS_DISCONNECTED = 'disconnected';

const RECONNECT_INITIAL_DELAY_MS = 1000;
const RECONNECT_MAX_DELAY_MS = 30000;
const RECONNECT_JITTER_FACTOR = 0.25;

export function createConnectionState() {
  return {
    status: CONNECTION_STATUS_RECONNECTING,
    lastSequence: 0,
    _connection: null,
    _reconnectTimer: null,
    _reconnectAttempt: 0,
  };
}

export function connect(state, handlers = {}) {
  _cleanup(state);

  const afterSequence = state.lastSequence;
  const connection = subscribeServerEvents(
    {
      onOpen: () => {
        state.status = CONNECTION_STATUS_CONNECTED;
        state._reconnectAttempt = 0;
        handlers.onStatusChange?.();
      },
      onClose: () => {
        _cleanup(state);
        state.status = CONNECTION_STATUS_DISCONNECTED;
        handlers.onStatusChange?.();
        _scheduleReconnect(state, handlers);
      },
      onEvent: (event) => {
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
    },
  );

  state._connection = connection;
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
  if (state._connection) {
    state._connection.close();
    state._connection = null;
  }
}

function _scheduleReconnect(state, handlers) {
  const delay = _reconnectDelay(state._reconnectAttempt);
  state._reconnectAttempt += 1;
  state._reconnectTimer = setTimeout(() => {
    state._reconnectTimer = null;
    connect(state, handlers);
  }, delay);
}

function _reconnectDelay(attempt) {
  const baseDelay = Math.min(
    RECONNECT_INITIAL_DELAY_MS * 2 ** attempt,
    RECONNECT_MAX_DELAY_MS,
  );
  const jitter = baseDelay * RECONNECT_JITTER_FACTOR;
  return baseDelay - jitter + Math.random() * jitter * 2;
}
