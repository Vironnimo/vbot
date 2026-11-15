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
