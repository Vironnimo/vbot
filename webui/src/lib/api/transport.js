import { isPlainObject } from '../values.js';

const RPC_ENDPOINT = '/api/rpc';

export const RPC_ERROR_INVALID_CLIENT_REQUEST = 'invalid_client_request';

export const RPC_ERROR_NETWORK = 'network_error';

export const RPC_ERROR_HTTP = 'http_error';

export const RPC_ERROR_RESPONSE = 'invalid_rpc_response';

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

export async function readJsonHttpPayload(response, method) {
  try {
    return await response.json();
  } catch (error) {
    throw new ApiClientError(
      RPC_ERROR_RESPONSE,
      'HTTP response body must be valid JSON',
      {
        method,
        status: response.status,
        cause: error,
      },
    );
  }
}

export function buildHttpUrl(path, baseUrl) {
  if (!baseUrl) {
    return path;
  }
  return new URL(path, baseUrl).toString();
}

export function buildHttpUrlWithAfterSequence(path, afterSequence = 0) {
  if (afterSequence <= 0) {
    return path;
  }
  const url = new URL(path, 'http://vbot.local');
  url.searchParams.set('after_sequence', String(afterSequence));
  if (path.startsWith('http://') || path.startsWith('https://')) {
    return url.toString();
  }
  return `${url.pathname}${url.search}${url.hash}`;
}

export function buildWebSocketUrl(
  path,
  baseUrl,
  afterSequence = 0,
  epoch,
  connectionId,
  accessor,
) {
  const params = {};
  if (afterSequence > 0) {
    params.after_sequence = String(afterSequence);
  }
  if (isNonEmptyString(epoch)) {
    params.epoch = epoch;
  }
  if (isNonEmptyString(connectionId)) {
    params.connection_id = connectionId;
  }
  if (isNonEmptyString(accessor)) {
    params.accessor = accessor;
  }
  return buildWebSocketUrlWithParams(path, baseUrl, params);
}

export function buildWebSocketUrlWithParams(path, baseUrl, params = {}) {
  if (path.startsWith('ws://') || path.startsWith('wss://')) {
    const url = new URL(path);
    appendSearchParams(url, params);
    return url.toString();
  }

  const browserBaseUrl = baseUrl ?? browserOrigin();
  if (!browserBaseUrl) {
    const url = new URL(path, 'ws://vbot.local');
    appendSearchParams(url, params);
    return `${url.pathname}${url.search}${url.hash}`;
  }

  const url = new URL(path, browserBaseUrl);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  appendSearchParams(url, params);
  return url.toString();
}

function appendSearchParams(url, params) {
  for (const [key, value] of Object.entries(params)) {
    if (value == null || value === '') {
      continue;
    }
    url.searchParams.set(key, String(value));
  }
}

function browserOrigin() {
  if (globalThis.location?.origin) {
    return globalThis.location.origin;
  }
  return null;
}

export function requireNonEmptyString(value, message, method) {
  if (!isNonEmptyString(value)) {
    throw new ApiClientError(RPC_ERROR_INVALID_CLIENT_REQUEST, message, {
      method,
    });
  }
  return value;
}

export function requireAgentMemoryMutation(agentId, scope, content, method) {
  requireNonEmptyString(agentId, 'Agent id must be a non-empty string', method);
  requireMemoryScope(scope, method);
  requireNonEmptyString(
    content,
    'Memory content must be a non-empty string',
    method,
  );
}

export function requireMemoryScope(scope, method) {
  if (scope !== 'agent' && scope !== 'user') {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Memory scope must be agent or user',
      { method },
    );
  }
}

export function requirePositiveInteger(value, label, method) {
  if (!Number.isInteger(value) || value <= 0) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      `${label} must be a positive integer`,
      { method },
    );
  }
}

export function requirePlainObject(value, message, method) {
  if (!isPlainObject(value)) {
    throw new ApiClientError(RPC_ERROR_INVALID_CLIENT_REQUEST, message, {
      method,
    });
  }
  return value;
}

export function isNonEmptyString(value) {
  return typeof value === 'string' && value.length > 0;
}
