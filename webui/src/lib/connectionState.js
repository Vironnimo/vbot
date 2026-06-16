import { subscribeServerEvents } from './api.js';
import { reconnectBackoffDelay } from './backoff.js';

export const CONNECTION_STATUS_CONNECTED = 'connected';
export const CONNECTION_STATUS_RECONNECTING = 'reconnecting';
export const CONNECTION_STATUS_DISCONNECTED = 'disconnected';

const RECONNECT_INITIAL_DELAY_MS = 1000;
const RECONNECT_MAX_DELAY_MS = 30000;

export function createConnectionState() {
  return {
    status: CONNECTION_STATUS_RECONNECTING,
    lastSequence: 0,
    epoch: '',
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
        if (event.type === 'connection_ready') {
          state.epoch = event.epoch ?? '';
          state.lastSequence = Number.isFinite(event.last_sequence)
            ? event.last_sequence
            : 0;
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
