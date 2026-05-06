const RPC_ENDPOINT = '/api/rpc';
const WEBSOCKET_ENDPOINT = '/ws';

export const RPC_ERROR_INVALID_CLIENT_REQUEST = 'invalid_client_request';
export const RPC_ERROR_NETWORK = 'network_error';
export const RPC_ERROR_HTTP = 'http_error';
export const RPC_ERROR_RESPONSE = 'invalid_rpc_response';
export const SSE_ERROR_RESPONSE = 'invalid_sse_event';
export const WEBSOCKET_ERROR_RESPONSE = 'invalid_websocket_event';

export const RUN_EVENT_TYPES = [
  'run_started',
  'user_message_persisted',
  'reasoning',
  'tool_call_started',
  'tool_call_result',
  'assistant_output',
  'run_completed',
  'run_cancelled',
  'run_failed',
];

const TERMINAL_RUN_EVENT_TYPES = new Set([
  'run_completed',
  'run_cancelled',
  'run_failed',
]);

export class ApiClientError extends Error {
  constructor(code, message, options = {}) {
    super(message);
    this.name = 'ApiClientError';
    this.code = code;
    this.status = options.status ?? null;
    this.method = options.method ?? null;
    this.details = options.details ?? null;
    this.cause = options.cause ?? null;
  }
}

export function createRpcEnvelope(method, params = {}) {
  if (typeof method !== 'string' || method.length === 0) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'RPC method must be a non-empty string',
    );
  }
  if (!isPlainObject(params)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'RPC params must be an object',
      {
        method,
      },
    );
  }
  return { method, params };
}

export async function rpc(method, params = {}, options = {}) {
  const envelope = createRpcEnvelope(method, params);
  const fetchFunction = options.fetch ?? globalThis.fetch;
  if (typeof fetchFunction !== 'function') {
    throw new ApiClientError(RPC_ERROR_NETWORK, 'fetch is not available', {
      method,
    });
  }

  let response;
  try {
    response = await fetchFunction(
      buildHttpUrl(options.rpcPath ?? RPC_ENDPOINT, options.baseUrl),
      {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          ...(options.headers ?? {}),
        },
        body: JSON.stringify(envelope),
        signal: options.signal,
      },
    );
  } catch (error) {
    throw new ApiClientError(
      RPC_ERROR_NETWORK,
      'RPC request failed before a response arrived',
      {
        method,
        cause: error,
      },
    );
  }

  const payload = await readRpcPayload(response, method);
  if (!response.ok) {
    throw normalizeRpcError(payload.error, {
      method,
      status: response.status,
      fallbackCode: RPC_ERROR_HTTP,
      fallbackMessage: `RPC request failed with HTTP ${response.status}`,
    });
  }
  if (!isPlainObject(payload) || typeof payload.ok !== 'boolean') {
    throw new ApiClientError(
      RPC_ERROR_RESPONSE,
      'RPC response must include an ok flag',
      {
        method,
        status: response.status,
        details: payload,
      },
    );
  }
  if (!payload.ok) {
    throw normalizeRpcError(payload.error, { method, status: response.status });
  }
  return payload.result;
}

export function normalizeRpcError(error, options = {}) {
  const code = isNonEmptyString(error?.code)
    ? error.code
    : (options.fallbackCode ?? 'rpc_error');
  const message = isNonEmptyString(error?.message)
    ? error.message
    : (options.fallbackMessage ?? 'RPC request failed');
  return new ApiClientError(code, message, {
    status: options.status,
    method: options.method,
    details: isPlainObject(error) ? error : null,
  });
}

export function subscribeRunEvents(sseUrl, handlers = {}, options = {}) {
  if (!isNonEmptyString(sseUrl)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'SSE URL must be a non-empty string',
    );
  }
  const EventSourceClass = options.EventSource ?? globalThis.EventSource;
  if (typeof EventSourceClass !== 'function') {
    throw new ApiClientError(RPC_ERROR_NETWORK, 'EventSource is not available');
  }

  const source = new EventSourceClass(buildHttpUrl(sseUrl, options.baseUrl));
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

async function readRpcPayload(response, method) {
  try {
    return await response.json();
  } catch (error) {
    throw new ApiClientError(
      RPC_ERROR_RESPONSE,
      'RPC response body must be valid JSON',
      {
        method,
        status: response.status,
        cause: error,
      },
    );
  }
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

function buildHttpUrl(path, baseUrl) {
  if (!baseUrl) {
    return path;
  }
  return new URL(path, baseUrl).toString();
}

function buildWebSocketUrl(path, baseUrl, afterSequence = 0) {
  if (path.startsWith('ws://') || path.startsWith('wss://')) {
    if (afterSequence > 0) {
      const url = new URL(path);
      url.searchParams.set('after_sequence', String(afterSequence));
      return url.toString();
    }
    return path;
  }
  const browserBaseUrl = baseUrl ?? browserOrigin();
  if (!browserBaseUrl) {
    if (afterSequence > 0) {
      const separator = path.includes('?') ? '&' : '?';
      return `${path}${separator}after_sequence=${afterSequence}`;
    }
    return path;
  }
  const url = new URL(path, browserBaseUrl);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  if (afterSequence > 0) {
    url.searchParams.set('after_sequence', String(afterSequence));
  }
  return url.toString();
}

function browserOrigin() {
  if (globalThis.location?.origin) {
    return globalThis.location.origin;
  }
  return null;
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isNonEmptyString(value) {
  return typeof value === 'string' && value.length > 0;
}
