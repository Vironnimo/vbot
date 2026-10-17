// Access loss: the server stopped accepting this page's credential, or it
// refuses this client outright. Every transport reports what it sees here;
// the boot gate (components/access/AccessGate.svelte) listens, takes the app
// down and asks the server what to show instead.
//
// Only the access guard's own refusals count: HTTP 401 `auth_required`, 429
// `rate_limited` (the guard answers it on a protected route only after
// presented credentials kept failing), 403 `access_denied`, and the WebSocket
// close codes 4401 / 4403. A bare 403 without the JSON envelope (the
// browser-origin guard) and a route's own 429 are ordinary request failures.

export const ACCESS_LOSS_AUTH_REQUIRED = 'auth_required';
export const ACCESS_LOSS_ACCESS_DENIED = 'access_denied';

export const WEBSOCKET_CLOSE_AUTH_REQUIRED = 4401;
export const WEBSOCKET_CLOSE_ACCESS_DENIED = 4403;

const listeners = new Set();

// Calls `listener(reason)` on every reported loss; returns the unsubscribe.
export function onAccessLoss(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function signalAccessLoss(reason) {
  for (const listener of [...listeners]) {
    listener(reason);
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
  return null;
}

// Reports a refused HTTP response; returns whether it was an access loss.
export function reportRefusedResponse(status, payload) {
  const loss = accessLossFromResponse(status, payload);
  if (loss) {
    signalAccessLoss(loss);
  }
  return loss !== null;
}

// Reports a refused HTTP response whose body was not read yet. The body is
// read from a clone, so the caller can still read the response.
export async function reportRefusedHttpResponse(response) {
  if (![401, 403, 429].includes(response?.status)) {
    return false;
  }
  let payload;
  try {
    payload = await (response.clone?.() ?? response).json();
  } catch {
    return false;
  }
  return reportRefusedResponse(response.status, payload);
}

// Close listener for a socket: reports a 4401 / 4403 close.
export function reportClosedSocket(event) {
  const loss = accessLossFromClose(event);
  if (loss) {
    signalAccessLoss(loss);
  }
  return loss !== null;
}
