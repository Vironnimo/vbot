import {
  RPC_ERROR_NETWORK,
  ApiClientError,
  buildHttpUrl,
  buildHttpUrlWithAfterSequence,
  requireNonEmptyString,
  buildWebSocketUrl,
  buildWebSocketUrlWithParams,
} from './transport.js';
import {
  resolveAccessorType,
  resolveClientConnectionId,
} from '../clientIdentity.js';

const WEBSOCKET_ENDPOINT = '/ws';

const LOGS_WEBSOCKET_ENDPOINT = '/ws/logs';

const TERMINALS_WEBSOCKET_ENDPOINT = '/ws/terminals';

export const SSE_ERROR_RESPONSE = 'invalid_sse_event';

export const WEBSOCKET_ERROR_RESPONSE = 'invalid_websocket_event';

export const RUN_EVENT_ASSISTANT_OUTPUT_DELTA = 'assistant_output_delta';

export const RUN_EVENT_REASONING_DELTA = 'reasoning_delta';

export const RUN_EVENT_TOOL_CALL_DELTA = 'tool_call_delta';

export const RUN_EVENT_STREAM_ATTEMPT_RESTARTED = 'stream_attempt_restarted';

export const RUN_EVENT_TOOL_CALL_STDOUT = 'tool_call_stdout';

export const RUN_EVENT_TOOL_CALL_STDERR = 'tool_call_stderr';

export const RUN_EVENT_PROVIDER_HEARTBEAT = 'provider_heartbeat';

export const RUN_EVENT_CHANGE_STATS = 'run_change_stats';

export const RUN_STREAM_HEARTBEAT_EVENT = 'heartbeat';

export const RUN_EVENT_TYPES = [
  'run_started',
  'user_message_persisted',
  'model_fallback_activated',
  'error_message_persisted',
  'compaction_started',
  'run_controls_changed',
  'compaction_aborted',
  'compaction_completed',
  RUN_EVENT_REASONING_DELTA,
  'reasoning',
  RUN_EVENT_STREAM_ATTEMPT_RESTARTED,
  RUN_EVENT_TOOL_CALL_DELTA,
  'tool_call_started',
  RUN_EVENT_TOOL_CALL_STDOUT,
  RUN_EVENT_TOOL_CALL_STDERR,
  'tool_call_result',
  'subagent_session_started',
  'subagent_status_changed',
  RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
  'assistant_output',
  'model_step_usage',
  RUN_EVENT_CHANGE_STATS,
  RUN_EVENT_PROVIDER_HEARTBEAT,
  'run_completed',
  'run_cancelled',
  'run_failed',
  'run_interrupted',
];

const TERMINAL_RUN_EVENT_TYPES = new Set([
  'run_completed',
  'run_cancelled',
  'run_failed',
  'run_interrupted',
]);

export function subscribeRunEvents(sseUrl, handlers = {}, options = {}) {
  requireNonEmptyString(sseUrl, 'SSE URL must be a non-empty string');
  const EventSourceClass = options.EventSource ?? globalThis.EventSource;
  if (typeof EventSourceClass !== 'function') {
    throw new ApiClientError(RPC_ERROR_NETWORK, 'EventSource is not available');
  }

  const source = new EventSourceClass(
    buildHttpUrl(
      buildHttpUrlWithAfterSequence(sseUrl, options.afterSequence ?? 0),
      options.baseUrl,
    ),
  );
  const cleanupCallbacks = [];
  let closed = false;

  const close = () => {
    if (closed) {
      return;
    }
    closed = true;
    for (const cleanup of cleanupCallbacks) {
      cleanup();
    }
    source.close();
  };

  addListener(source, 'open', handlers.onOpen, cleanupCallbacks);
  addListener(source, 'error', handlers.onError, cleanupCallbacks);

  const heartbeatListener = (event) => handlers.onHeartbeat?.(event);
  source.addEventListener(RUN_STREAM_HEARTBEAT_EVENT, heartbeatListener);
  cleanupCallbacks.push(() =>
    source.removeEventListener(RUN_STREAM_HEARTBEAT_EVENT, heartbeatListener),
  );

  for (const eventType of options.eventTypes ?? RUN_EVENT_TYPES) {
    const listener = (event) => {
      const parsed = parseJsonEventData(
        event.data,
        SSE_ERROR_RESPONSE,
        'SSE event data must be JSON',
      );
      if (parsed instanceof ApiClientError) {
        handlers.onError?.(parsed, event);
        return;
      }
      handlers.onEvent?.({ type: eventType, data: parsed, rawEvent: event });
      if (
        (options.closeOnTerminal ?? true) &&
        TERMINAL_RUN_EVENT_TYPES.has(eventType)
      ) {
        close();
      }
    };
    source.addEventListener(eventType, listener);
    cleanupCallbacks.push(() =>
      source.removeEventListener(eventType, listener),
    );
  }

  return { close, source };
}

export function subscribeServerEvents(handlers = {}, options = {}) {
  const WebSocketClass = options.WebSocket ?? globalThis.WebSocket;
  if (typeof WebSocketClass !== 'function') {
    throw new ApiClientError(RPC_ERROR_NETWORK, 'WebSocket is not available');
  }

  const socket = new WebSocketClass(
    buildWebSocketUrl(
      options.path ?? WEBSOCKET_ENDPOINT,
      options.baseUrl,
      options.afterSequence ?? 0,
      options.epoch,
      // Per-window presence identity sent on connect (overridable for tests);
      // the server registers the window and the General panel marks its own row.
      options.connectionId ?? resolveClientConnectionId(),
      options.accessor ?? resolveAccessorType(),
    ),
  );
  const cleanupCallbacks = [];
  let closed = false;

  addListener(socket, 'open', handlers.onOpen, cleanupCallbacks);
  addListener(socket, 'error', handlers.onError, cleanupCallbacks);
  addListener(socket, 'close', handlers.onClose, cleanupCallbacks);
  addListener(
    socket,
    'message',
    (event) => {
      const parsed = parseJsonEventData(
        event.data,
        WEBSOCKET_ERROR_RESPONSE,
        'WebSocket event data must be JSON',
      );
      if (parsed instanceof ApiClientError) {
        handlers.onError?.(parsed, event);
        return;
      }
      handlers.onEvent?.(parsed, event);
    },
    cleanupCallbacks,
  );

  const close = (code, reason) => {
    if (closed) {
      return;
    }
    closed = true;
    for (const cleanup of cleanupCallbacks) {
      cleanup();
    }
    socket.close(code, reason);
  };

  return { close, socket };
}

export function subscribeLogEvents(file, handlers = {}, options = {}) {
  requireNonEmptyString(file, 'Log file must be a non-empty string');

  const WebSocketClass = options.WebSocket ?? globalThis.WebSocket;
  if (typeof WebSocketClass !== 'function') {
    throw new ApiClientError(RPC_ERROR_NETWORK, 'WebSocket is not available');
  }

  const socket = new WebSocketClass(
    buildWebSocketUrlWithParams(
      options.path ?? LOGS_WEBSOCKET_ENDPOINT,
      options.baseUrl,
      {
        file,
        cursor: options.cursor,
      },
    ),
  );
  const cleanupCallbacks = [];
  let closed = false;

  addListener(socket, 'open', handlers.onOpen, cleanupCallbacks);
  addListener(socket, 'error', handlers.onError, cleanupCallbacks);
  addListener(socket, 'close', handlers.onClose, cleanupCallbacks);
  addListener(
    socket,
    'message',
    (event) => {
      const parsed = parseJsonEventData(
        event.data,
        WEBSOCKET_ERROR_RESPONSE,
        'WebSocket event data must be JSON',
      );
      if (parsed instanceof ApiClientError) {
        handlers.onError?.(parsed, event);
        return;
      }
      handlers.onEvent?.(parsed, event);
    },
    cleanupCallbacks,
  );

  const close = (code, reason) => {
    if (closed) {
      return;
    }
    closed = true;
    for (const cleanup of cleanupCallbacks) {
      cleanup();
    }
    socket.close(code, reason);
  };

  return { close, socket };
}

export function subscribeTerminalEvents(
  terminalId,
  handlers = {},
  options = {},
) {
  requireNonEmptyString(terminalId, 'Terminal id must be a non-empty string');

  const WebSocketClass = options.WebSocket ?? globalThis.WebSocket;
  if (typeof WebSocketClass !== 'function') {
    throw new ApiClientError(RPC_ERROR_NETWORK, 'WebSocket is not available');
  }

  const path = `${options.path ?? TERMINALS_WEBSOCKET_ENDPOINT}/${encodeURIComponent(terminalId)}`;
  const socket = new WebSocketClass(
    buildWebSocketUrlWithParams(path, options.baseUrl),
  );
  const cleanupCallbacks = [];
  let closed = false;

  addListener(socket, 'open', handlers.onOpen, cleanupCallbacks);
  addListener(socket, 'error', handlers.onError, cleanupCallbacks);
  addListener(socket, 'close', handlers.onClose, cleanupCallbacks);
  addListener(
    socket,
    'message',
    (event) => {
      const parsed = parseJsonEventData(
        event.data,
        WEBSOCKET_ERROR_RESPONSE,
        'Terminal WebSocket event data must be JSON',
      );
      if (parsed instanceof ApiClientError) {
        handlers.onError?.(parsed, event);
        return;
      }
      handlers.onEvent?.(parsed, event);
    },
    cleanupCallbacks,
  );

  const close = (code, reason) => {
    if (closed) {
      return;
    }
    closed = true;
    for (const cleanup of cleanupCallbacks) {
      cleanup();
    }
    socket.close(code, reason);
  };

  return { close, socket };
}

function parseJsonEventData(data, code, message) {
  try {
    return JSON.parse(data);
  } catch (error) {
    return new ApiClientError(code, message, { cause: error, details: data });
  }
}

function addListener(target, eventName, listener, cleanupCallbacks) {
  if (typeof listener !== 'function') {
    return;
  }
  target.addEventListener(eventName, listener);
  cleanupCallbacks.push(() => target.removeEventListener(eventName, listener));
}
