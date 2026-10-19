import {
  RPC_ERROR_HTTP,
  RPC_ERROR_NETWORK,
  ApiClientError,
  buildHttpUrl,
  readJsonHttpPayload,
  requireNonEmptyString,
  rpc,
} from './transport.js';
import {
  ACCESS_LOSS_ACCESS_DENIED,
  ACCESS_LOSS_AUTH_REQUIRED,
  ACCESS_LOSS_DEVICES_UNAVAILABLE,
  retryAfterSeconds,
  signalAccessLoss,
} from './accessLoss.js';
import { isPlainObject } from '../values.js';

// Access: the server's access mode, browser pairing and Device management.
// The pairing endpoints answer before sign-in, so they are plain HTTP calls
// that never report an access loss; their caller handles every refusal.

const AUTH_STATUS_ENDPOINT = '/api/auth/status';
const PAIR_START_ENDPOINT = '/api/auth/pair/start';
const PAIR_POLL_ENDPOINT = '/api/auth/pair/poll';

export const DEVICE_KIND_BROWSER = 'browser';

// `{mode, mode_label, authenticated, principal, device}`; `principal` is
// `host`, `device`, `open` (local and lan modes) or null.
export function getAuthStatus(options = {}) {
  return requestAccess('GET', AUTH_STATUS_ENDPOINT, undefined, options);
}

// `{request_id, code, poll_secret, expires_in, poll_interval}`.
export function startPairing({ kind = DEVICE_KIND_BROWSER, name }, options) {
  requireNonEmptyString(name, 'Device name must be a non-empty string');
  return requestAccess('POST', PAIR_START_ENDPOINT, { kind, name }, options);
}

// `{status: 'pending', expires_in}`, `{status: 'approved', device}` (the
// response sets the browser's device cookie), `{status: 'denied'}` or
// `{status: 'expired'}`.
export function pollPairing({ requestId, pollSecret }, options) {
  return requestAccess(
    'POST',
    PAIR_POLL_ENDPOINT,
    { request_id: requestId, poll_secret: pollSecret },
    options,
  );
}

// Errors carry the server's code (`rate_limited`, `auth_disabled`,
// `devices_unavailable`, `access_denied`, `invalid_request`), the HTTP status
// and, for `rate_limited` and `devices_unavailable`, `retryAfter` in seconds.
async function requestAccess(method, path, body, options = {}) {
  const fetchFunction = options.fetch ?? globalThis.fetch;
  let response;
  try {
    response = await fetchFunction(buildHttpUrl(path, options.baseUrl), {
      method,
      headers:
        body === undefined ? undefined : { 'content-type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: 'no-store',
      signal: options.signal,
    });
  } catch (cause) {
    throw new ApiClientError(
      RPC_ERROR_NETWORK,
      'Access request failed before a response arrived',
      { method: path, cause },
    );
  }
  const payload = await readJsonHttpPayload(response, path);
  if (!response.ok || !isPlainObject(payload) || payload.ok === false) {
    const error = isPlainObject(payload?.error) ? payload.error : {};
    const failure = new ApiClientError(
      typeof error.code === 'string' && error.code
        ? error.code
        : RPC_ERROR_HTTP,
      typeof error.message === 'string' && error.message
        ? error.message
        : `Access request failed with HTTP ${response.status}`,
      { method: path, status: response.status, details: error },
    );
    failure.retryAfter = retryAfterSeconds(
      response.headers?.get?.('retry-after'),
    );
    throw failure;
  }
  return payload;
}

// One shared check whether this page still has access, for transports that
// cannot read the refusal themselves (an EventSource sees no status code).
// Reports a loss when the server refuses this client, no longer knows its
// credential or cannot check it right now; a failed check reports nothing.
let accessCheck = null;

export function verifyAccess(options = {}) {
  accessCheck ??= getAuthStatus(options)
    .then(
      (status) => {
        if (status.mode === 'authenticated' && status.authenticated !== true) {
          signalAccessLoss(ACCESS_LOSS_AUTH_REQUIRED);
        }
      },
      (error) => {
        if (error?.code === 'access_denied') {
          signalAccessLoss(ACCESS_LOSS_ACCESS_DENIED);
        } else if (error?.code === 'devices_unavailable') {
          signalAccessLoss(ACCESS_LOSS_DEVICES_UNAVAILABLE, error.retryAfter);
        }
      },
    )
    .finally(() => {
      accessCheck = null;
    });
  return accessCheck;
}

// Device management. Every list entry is `{id, name, kind, created_at,
// last_used_at, last_client, approved_by, current}`, oldest first.
export function listDevices(options = {}) {
  return rpc('devices.list', {}, options);
}

export function renameDevice(id, name, options = {}) {
  requireNonEmptyString(id, 'Device id must be a non-empty string');
  return rpc('devices.rename', { id, name }, options);
}

export function revokeDevice(id, options = {}) {
  requireNonEmptyString(id, 'Device id must be a non-empty string');
  return rpc('devices.revoke', { id }, options);
}

export function revokeOtherDevices(options = {}) {
  return rpc('devices.revoke_others', {}, options);
}

// Signs out the Device this page belongs to; only a Device principal can.
export function logOutDevice(options = {}) {
  return rpc('devices.logout', {}, options);
}

// `{code, name, kind, client, requested_at, expires_in}` of the device waiting
// with this pairing code.
export function getPendingPairing(code, options = {}) {
  requireNonEmptyString(code, 'Pairing code must be a non-empty string');
  return rpc('devices.pending', { code }, options);
}

export function approvePairing(code, options = {}) {
  requireNonEmptyString(code, 'Pairing code must be a non-empty string');
  return rpc('devices.approve', { code }, options);
}

export function denyPairing(code, options = {}) {
  requireNonEmptyString(code, 'Pairing code must be a non-empty string');
  return rpc('devices.deny', { code }, options);
}
