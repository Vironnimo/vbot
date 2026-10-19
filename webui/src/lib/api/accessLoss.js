// Access loss: the server stopped accepting this page's credential, or it
// refuses this client outright. Every transport reports what it sees here;
// the boot gate (components/access/AccessGate.svelte) listens, takes the app
// down and asks the server what to show instead.
//
// Only the access guard's own refusals count: HTTP 401 `auth_required`, 429
// `rate_limited` (the guard answers it on a protected route only after
// presented credentials kept failing), 403 `access_denied`, 503
// `devices_unavailable` (the server cannot check this page's Device right now;
// the sign-in stays), and the WebSocket close codes 4401 / 4403 / 4503. A bare
// 403 without the JSON envelope (the browser-origin guard), a route's own 429
// and any other 503 are ordinary request failures.

export const ACCESS_LOSS_AUTH_REQUIRED = 'auth_required';
export const ACCESS_LOSS_ACCESS_DENIED = 'access_denied';
export const ACCESS_LOSS_DEVICES_UNAVAILABLE = 'devices_unavailable';

export const WEBSOCKET_CLOSE_AUTH_REQUIRED = 4401;
export const WEBSOCKET_CLOSE_ACCESS_DENIED = 4403;
export const WEBSOCKET_CLOSE_DEVICES_UNAVAILABLE = 4503;

const listeners = new Set();

// Calls `listener(reason, retryAfter)` on every reported loss; the optional
// wait is in seconds. Returns the unsubscribe.
export function onAccessLoss(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function signalAccessLoss(reason, retryAfter = null) {
  for (const listener of [...listeners]) {
    listener(reason, retryAfter);
  }
}

// The loss an HTTP refusal reports, or null. `payload` is the parsed body.
export function accessLossFromResponse(status, payload) {
  const code = payload?.ok === false ? payload.error?.code : undefined;
  if (status === 401 && code === 'auth_required') {
    return ACCESS_LOSS_AUTH_REQUIRED;
  }
  if (status === 429 && code === 'rate_limited') {
    return ACCESS_LOSS_AUTH_REQUIRED;
  }
  if (status === 403 && code === 'access_denied') {
    return ACCESS_LOSS_ACCESS_DENIED;
  }
  if (status === 503 && code === 'devices_unavailable') {
    return ACCESS_LOSS_DEVICES_UNAVAILABLE;
  }
  return null;
}

// The loss a WebSocket close event reports, or null.
export function accessLossFromClose(event) {
  if (event?.code === WEBSOCKET_CLOSE_AUTH_REQUIRED) {
    return ACCESS_LOSS_AUTH_REQUIRED;
  }
  if (event?.code === WEBSOCKET_CLOSE_ACCESS_DENIED) {
    return ACCESS_LOSS_ACCESS_DENIED;
  }
  if (event?.code === WEBSOCKET_CLOSE_DEVICES_UNAVAILABLE) {
    return ACCESS_LOSS_DEVICES_UNAVAILABLE;
  }
  return null;
}

// Reports a refused HTTP response; returns whether it was an access loss.
export function reportRefusedResponse(status, payload, retryAfter = null) {
  const loss = accessLossFromResponse(status, payload);
  if (loss) {
    signalAccessLoss(loss, retryAfterSeconds(retryAfter));
  }
  return loss !== null;
}

// Reports a refused HTTP response whose body was not read yet. The body is
// read from a clone, so the caller can still read the response.
export async function reportRefusedHttpResponse(response) {
  if (![401, 403, 429, 503].includes(response?.status)) {
    return false;
  }
  let payload;
  try {
    payload = await (response.clone?.() ?? response).json();
  } catch {
    return false;
  }
  return reportRefusedResponse(
    response.status,
    payload,
    response.headers?.get?.('retry-after'),
  );
}

// Close listener for a socket: reports a 4401 / 4403 / 4503 close.
export function reportClosedSocket(event) {
  const loss = accessLossFromClose(event);
  if (loss) {
    signalAccessLoss(loss);
  }
  return loss !== null;
}

// Retry-After permits either seconds or an HTTP date. A missing or invalid
// header leaves the owner's protocol-specific fallback in charge.
export function retryAfterSeconds(value, now = Date.now()) {
  if (typeof value !== 'string' || !value.trim()) return null;
  if (/^\d+$/.test(value.trim())) {
    const seconds = Number(value);
    return Number.isFinite(seconds) ? seconds : null;
  }
  const at = Date.parse(value);
  return Number.isFinite(at) ? Math.max(0, Math.ceil((at - now) / 1000)) : null;
}
