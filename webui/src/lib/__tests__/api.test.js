import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  ApiClientError,
  RPC_ERROR_HTTP,
  RPC_ERROR_INVALID_CLIENT_REQUEST,
  RPC_ERROR_NETWORK,
  RPC_ERROR_RESPONSE,
  SSE_ERROR_RESPONSE,
  WEBSOCKET_ERROR_RESPONSE,
  createRpcEnvelope,
  normalizeRpcError,
  rpc,
  subscribeRunEvents,
  subscribeServerEvents,
} from '../api.js';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('createRpcEnvelope()', () => {
  it('creates the server RPC envelope', () => {
    expect(createRpcEnvelope('agent.list')).toEqual({
      method: 'agent.list',
      params: {},
    });
  });

  it('rejects invalid method and params before sending', () => {
    expect(() => createRpcEnvelope('', {})).toThrow(
      expect.objectContaining({ code: RPC_ERROR_INVALID_CLIENT_REQUEST }),
    );
    expect(() => createRpcEnvelope('agent.list', [])).toThrow(
      expect.objectContaining({ code: RPC_ERROR_INVALID_CLIENT_REQUEST }),
    );
  });
});

describe('rpc()', () => {
  it('posts an RPC envelope and returns the result', async () => {
    const fetchFunction = vi
      .fn()
      .mockResolvedValue(jsonResponse({ ok: true, result: { agents: [] } }));

    const result = await rpc(
      'agent.list',
      { visible: true },
      { baseUrl: 'http://localhost:8420/', fetch: fetchFunction },
    );

    expect(result).toEqual({ agents: [] });
    expect(fetchFunction).toHaveBeenCalledWith(
      'http://localhost:8420/api/rpc',
      {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          method: 'agent.list',
          params: { visible: true },
        }),
        signal: undefined,
      },
    );
  });

  it('normalizes server RPC errors', async () => {
    const fetchFunction = vi.fn().mockResolvedValue(
      jsonResponse(
        {
          ok: false,
          error: { code: 'active_run', message: 'session is busy' },
        },
        { status: 200 },
      ),
    );

    await expect(
      rpc('chat.stream', {}, { fetch: fetchFunction }),
    ).rejects.toMatchObject({
      name: 'ApiClientError',
      code: 'active_run',
      message: 'session is busy',
      method: 'chat.stream',
      status: 200,
    });
  });

  it('normalizes HTTP errors even when the body is an RPC error envelope', async () => {
    const fetchFunction = vi.fn().mockResolvedValue(
      jsonResponse(
        {
          ok: false,
          error: { code: 'domain_error', message: 'agent does not exist' },
        },
        { ok: false, status: 404 },
      ),
    );

    await expect(
      rpc('agent.update', {}, { fetch: fetchFunction }),
    ).rejects.toMatchObject({
      code: 'domain_error',
      message: 'agent does not exist',
      status: 404,
    });
  });

  it('uses a predictable fallback for non-RPC HTTP errors', async () => {
    const fetchFunction = vi
      .fn()
      .mockResolvedValue(jsonResponse({ detail: 'Not Found' }, { ok: false }));

    await expect(
      rpc('agent.list', {}, { fetch: fetchFunction }),
    ).rejects.toMatchObject({
      code: RPC_ERROR_HTTP,
      message: 'RPC request failed with HTTP 500',
      status: 500,
    });
  });

  it('normalizes network and malformed response failures', async () => {
    const networkFetch = vi.fn().mockRejectedValue(new Error('offline'));
    const malformedFetch = vi
      .fn()
      .mockResolvedValue(jsonResponse({ result: {} }));

    await expect(
      rpc('agent.list', {}, { fetch: networkFetch }),
    ).rejects.toMatchObject({
      code: RPC_ERROR_NETWORK,
    });
    await expect(
      rpc('agent.list', {}, { fetch: malformedFetch }),
    ).rejects.toMatchObject({
      code: RPC_ERROR_RESPONSE,
    });
  });
});

describe('normalizeRpcError()', () => {
  it('turns unknown error shapes into ApiClientError', () => {
    const error = normalizeRpcError(null, {
      method: 'agent.list',
      status: 200,
    });

    expect(error).toBeInstanceOf(ApiClientError);
    expect(error).toMatchObject({
      code: 'rpc_error',
      message: 'RPC request failed',
    });
  });
});

describe('subscribeRunEvents()', () => {
  it('subscribes to named SSE run events and closes on terminal events', () => {
    const onEvent = vi.fn();
    const onError = vi.fn();
    const connection = subscribeRunEvents(
      '/api/runs/run-one/events',
      { onEvent, onError },
      { EventSource: MockEventSource, baseUrl: 'http://localhost:8420/' },
    );

    connection.source.emit('reasoning', {
      data: JSON.stringify({ payload: { text: 'thinking' } }),
    });
    connection.source.emit('run_completed', {
      data: JSON.stringify({ payload: { status: 'done' } }),
    });
    connection.close();

    expect(connection.source.url).toBe(
      'http://localhost:8420/api/runs/run-one/events',
    );
    expect(onEvent).toHaveBeenCalledWith({
      type: 'reasoning',
      data: { payload: { text: 'thinking' } },
      rawEvent: expect.any(Object),
    });
    expect(connection.source.closeCount).toBe(1);
    expect(onError).not.toHaveBeenCalled();
  });

  it('reports malformed SSE JSON through the error handler', () => {
    const onError = vi.fn();
    const connection = subscribeRunEvents(
      '/events',
      { onError },
      { EventSource: MockEventSource },
    );

    connection.source.emit('reasoning', { data: 'not json' });

    expect(onError).toHaveBeenCalledWith(
      expect.objectContaining({ code: SSE_ERROR_RESPONSE }),
      expect.any(Object),
    );
  });
});

describe('subscribeServerEvents()', () => {
  it('subscribes to /ws messages and parses JSON events', () => {
    const onEvent = vi.fn();
    const connection = subscribeServerEvents(
      { onEvent },
      { WebSocket: MockWebSocket, baseUrl: 'https://localhost:8420/' },
    );

    connection.socket.emit('message', {
      data: JSON.stringify({ type: 'run_started' }),
    });
    connection.close(1000, 'done');
    connection.close(1000, 'done');

    expect(connection.socket.url).toBe('wss://localhost:8420/ws');
    expect(onEvent).toHaveBeenCalledWith(
      { type: 'run_started' },
      expect.any(Object),
    );
    expect(connection.socket.closeCalls).toEqual([
      { code: 1000, reason: 'done' },
    ]);
  });

  it('reports malformed WebSocket messages through the error handler', () => {
    const onError = vi.fn();
    const connection = subscribeServerEvents(
      { onError },
      { WebSocket: MockWebSocket },
    );

    connection.socket.emit('message', { data: '{' });

    expect(onError).toHaveBeenCalledWith(
      expect.objectContaining({ code: WEBSOCKET_ERROR_RESPONSE }),
      expect.any(Object),
    );
  });
});

function jsonResponse(body, options = {}) {
  return {
    ok: options.ok ?? true,
    status: options.status ?? 500,
    json: vi.fn().mockResolvedValue(body),
  };
}

class MockEventSource {
  constructor(url) {
    this.url = url;
    this.closeCount = 0;
    this.listeners = new Map();
  }

  addEventListener(eventName, listener) {
    this.listeners.set(eventName, [
      ...(this.listeners.get(eventName) ?? []),
      listener,
    ]);
  }

  removeEventListener(eventName, listener) {
    this.listeners.set(
      eventName,
      (this.listeners.get(eventName) ?? []).filter(
        (storedListener) => storedListener !== listener,
      ),
    );
  }

  emit(eventName, event) {
    for (const listener of this.listeners.get(eventName) ?? []) {
      listener({ type: eventName, ...event });
    }
  }

  close() {
    this.closeCount += 1;
  }
}

class MockWebSocket {
  constructor(url) {
    this.url = url;
    this.closeCalls = [];
    this.listeners = new Map();
  }

  addEventListener(eventName, listener) {
    this.listeners.set(eventName, [
      ...(this.listeners.get(eventName) ?? []),
      listener,
    ]);
  }

  removeEventListener(eventName, listener) {
    this.listeners.set(
      eventName,
      (this.listeners.get(eventName) ?? []).filter(
        (storedListener) => storedListener !== listener,
      ),
    );
  }

  emit(eventName, event) {
    for (const listener of this.listeners.get(eventName) ?? []) {
      listener({ type: eventName, ...event });
    }
  }

  close(code, reason) {
    this.closeCalls.push({ code, reason });
  }
}
