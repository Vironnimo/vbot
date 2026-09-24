import {
  RPC_ERROR_INVALID_CLIENT_REQUEST,
  RPC_ERROR_NETWORK,
  ApiClientError,
  buildWebSocketUrlWithParams,
  isNonEmptyString,
  requireNonEmptyString,
  rpc,
} from './transport.js';
import { isPlainObject } from '../values.js';

// Live voice call transport. The server owns the provider call; the page only
// exchanges its WebRTC offer/answer, receives call updates on the owner socket
// and answers UI requests.
const LIVE_WEBSOCKET_ENDPOINT = '/ws/live';
// Server close codes of the owner socket, named by what the caller should do:
// `lagged` and any other close (`lost`) may reattach; the rest end the call.
const LIVE_SOCKET_CLOSE_OUTCOMES = new Map([
  [1000, 'ended'],
  [1008, 'unknown_call'],
  [1013, 'lagged'],
  [4000, 'replaced'],
]);

export const LIVE_SOCKET_ERROR_RESPONSE = 'invalid_live_event';

export function getLiveVoiceStatus(options = {}) {
  return rpc('live.status', {}, options);
}

export function startLiveCall(sdp, options = {}) {
  requireNonEmptyString(
    sdp,
    'SDP offer must be a non-empty string',
    'live.start',
  );
  return rpc('live.start', { sdp }, options);
}

export function stopLiveCall(callId, options = {}) {
  requireNonEmptyString(
    callId,
    'Live call id must be a non-empty string',
    'live.stop',
  );
  return rpc('live.stop', { call_id: callId }, options);
}

// `outcome` is exactly one of `{ result }` (a JSON object) or `{ error }` (a
// stable error code).
export function sendLiveUiResult(callId, requestId, outcome, options = {}) {
  requireNonEmptyString(
    callId,
    'Live call id must be a non-empty string',
    'live.ui_result',
  );
  requireNonEmptyString(
    requestId,
    'Live UI request id must be a non-empty string',
    'live.ui_result',
  );
  const hasResult = isPlainObject(outcome) && Object.hasOwn(outcome, 'result');
  const hasError = isPlainObject(outcome) && Object.hasOwn(outcome, 'error');
  if (
    hasResult === hasError ||
    (hasResult && !isPlainObject(outcome.result)) ||
    (hasError && !isNonEmptyString(outcome.error))
  ) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Live UI outcome must be either a result object or an error code',
      { method: 'live.ui_result' },
    );
  }
  return rpc(
    'live.ui_result',
    {
      call_id: callId,
      request_id: requestId,
      ...(hasResult ? { result: outcome.result } : { error: outcome.error }),
    },
    options,
  );
}

// Receive-only owner socket for one Live call. Frames are JSON objects; a
// malformed frame reaches `onError` without closing the socket. `onClose`
// receives the close event and its outcome: `ended` (after the `closed` frame),
// `unknown_call`, `replaced` (a newer owner socket took over; do not reattach),
// `lagged` (the socket fell behind; reattach) or `lost`.
export function openLiveCallSocket(callId, handlers = {}, options = {}) {
  requireNonEmptyString(callId, 'Live call id must be a non-empty string');

  const WebSocketClass = options.WebSocket ?? globalThis.WebSocket;
  if (typeof WebSocketClass !== 'function') {
    throw new ApiClientError(RPC_ERROR_NETWORK, 'WebSocket is not available');
  }

  const path = `${options.path ?? LIVE_WEBSOCKET_ENDPOINT}/${encodeURIComponent(callId)}`;
  const socket = new WebSocketClass(
    buildWebSocketUrlWithParams(path, options.baseUrl),
  );
  const cleanupCallbacks = [];
  let closed = false;

  const listen = (eventName, listener) => {
    if (typeof listener !== 'function') return;
    socket.addEventListener(eventName, listener);
    cleanupCallbacks.push(() =>
      socket.removeEventListener(eventName, listener),
    );
  };

  listen('open', handlers.onOpen);
  listen('error', handlers.onError);
  if (typeof handlers.onClose === 'function') {
    listen('close', (event) =>
      handlers.onClose(
        event,
        LIVE_SOCKET_CLOSE_OUTCOMES.get(event?.code) ?? 'lost',
      ),
    );
  }
  listen('message', (event) => {
    let frame;
    try {
      frame = JSON.parse(event.data);
    } catch (error) {
      handlers.onError?.(
        new ApiClientError(
          LIVE_SOCKET_ERROR_RESPONSE,
          'Live call event data must be JSON',
          { cause: error, details: event.data },
        ),
        event,
      );
      return;
    }
    if (!isPlainObject(frame)) {
      handlers.onError?.(
        new ApiClientError(
          LIVE_SOCKET_ERROR_RESPONSE,
          'Live call event must be a JSON object',
          { details: event.data },
        ),
        event,
      );
      return;
    }
    handlers.onEvent?.(frame, event);
  });

  const close = (code, reason) => {
    if (closed) return;
    closed = true;
    for (const cleanup of cleanupCallbacks) cleanup();
    socket.close(code, reason);
  };

  return { close, socket };
}
