import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '../api.js';
import { jsonResponse, MockEventSource, MockWebSocket } from './api.support.js';

const BASE_URL = 'https://localhost:8420/';

let losses;
let stopListening;

beforeEach(() => {
  losses = [];
  stopListening = api.onAccessLoss((reason) => losses.push(reason));
});

afterEach(() => {
  stopListening();
});

const refusal = (status, code) =>
  jsonResponse(
    { ok: false, error: { code, message: `${code} sentinel` } },
    { ok: false, status },
  );

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('access loss', () => {
  it.each([
    ['an unknown credential', refusal(401, 'auth_required'), ['auth_required']],
    [
      'credentials that kept failing',
      refusal(429, 'rate_limited'),
      ['auth_required'],
    ],
    ['a refused client', refusal(403, 'access_denied'), ['access_denied']],
    [
      'the browser-origin guard',
      jsonResponse({ detail: 'Forbidden' }, { ok: false, status: 403 }),
      [],
    ],
    ['a route’s own rate limit', refusal(429, 'too_many_runs'), []],
  ])('reports %s refusing an RPC', async (_label, response, expected) => {
    await expect(
      api.rpc('agent.list', {}, { fetch: vi.fn().mockResolvedValue(response) }),
    ).rejects.toBeInstanceOf(api.ApiClientError);

    expect(losses).toEqual(expected);
  });

  it('reports refused uploads and speech playback', async () => {
    await expect(
      api.uploadAttachment(new File(['hello'], 'notes.txt'), {
        fetch: vi.fn().mockResolvedValue(refusal(401, 'auth_required')),
      }),
    ).rejects.toMatchObject({ status: 401 });
    const playback = new Response(
      JSON.stringify({ ok: false, error: { code: 'access_denied' } }),
      { status: 403 },
    );
    await expect(
      api
        .readSpeechPlayback('/api/speech/playback/test', {
          fetch: vi.fn().mockResolvedValue(playback),
        })
        .next(),
    ).rejects.toMatchObject({ status: 403 });

    expect(losses).toEqual(['auth_required', 'access_denied']);
  });

  it.each([
    [
      '/ws',
      (handlers, options) => api.subscribeServerEvents(handlers, options),
    ],
    [
      '/ws/logs',
      (handlers, options) =>
        api.subscribeLogEvents('2026-05-11', handlers, options),
    ],
    [
      '/ws/terminals',
      (handlers, options) =>
        api.subscribeTerminalEvents('term-1', handlers, options),
    ],
    [
      '/ws/live',
      (handlers, options) =>
        api.openLiveCallSocket('call-1', handlers, options),
    ],
  ])('reports the %s socket closed for lost access', (_path, open) => {
    const onClose = vi.fn();
    const connection = open(
      { onClose },
      { WebSocket: MockWebSocket, baseUrl: BASE_URL },
    );

    connection.socket.emit('close', { code: 4401 });

    expect(losses).toEqual(['auth_required']);
    expect(onClose).toHaveBeenCalledOnce();
  });

  it.each([
    [4403, ['access_denied']],
    [1006, []],
  ])('reports a socket close code %i as %j', (code, expected) => {
    const connection = api.subscribeServerEvents(
      {},
      { WebSocket: MockWebSocket, baseUrl: BASE_URL },
    );

    connection.socket.emit('close', { code });

    expect(losses).toEqual(expected);
  });

  it.each([
    [
      'a signed-out page',
      2,
      { mode: 'authenticated', authenticated: false },
      ['auth_required'],
    ],
    [
      'a signed-in page',
      2,
      { mode: 'authenticated', authenticated: true, principal: 'device' },
      [],
    ],
    ['a stream the browser still retries', 0, null, []],
  ])(
    'asks the server once after a Run stream failure for %s',
    async (_label, readyState, status, expected) => {
      const fetch = vi
        .fn()
        .mockResolvedValue(jsonResponse(status, { status: 200 }));
      vi.stubGlobal('fetch', fetch);
      const connection = api.subscribeRunEvents(
        '/api/runs/run-one/events',
        { onError: vi.fn() },
        { EventSource: MockEventSource, baseUrl: BASE_URL },
      );
      connection.source.readyState = readyState;

      connection.source.emit('error', {});
      connection.source.emit('error', {});
      await settle();

      expect(fetch.mock.calls.map(([url]) => url)).toEqual(
        status ? ['https://localhost:8420/api/auth/status'] : [],
      );
      expect(losses).toEqual(expected);
    },
  );
});

describe('pairing endpoints', () => {
  it('starts pairing as a browser and polls with the request secret', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse(
          {
            request_id: 'request-1',
            code: 'K7QM-4XPA',
            poll_secret: 'secret-1',
            expires_in: 600,
            poll_interval: 2,
          },
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse({ status: 'pending', expires_in: 590 }, { status: 200 }),
      );
    const options = { fetch, baseUrl: BASE_URL };

    await expect(
      api.startPairing({ name: 'Firefox on Linux' }, options),
    ).resolves.toMatchObject({ code: 'K7QM-4XPA' });
    await expect(
      api.pollPairing(
        { requestId: 'request-1', pollSecret: 'secret-1' },
        options,
      ),
    ).resolves.toEqual({ status: 'pending', expires_in: 590 });

    expect(
      fetch.mock.calls.map(([url, init]) => [
        url,
        init.method,
        init.cache,
        JSON.parse(init.body),
      ]),
    ).toEqual([
      [
        'https://localhost:8420/api/auth/pair/start',
        'POST',
        'no-store',
        { kind: 'browser', name: 'Firefox on Linux' },
      ],
      [
        'https://localhost:8420/api/auth/pair/poll',
        'POST',
        'no-store',
        { request_id: 'request-1', poll_secret: 'secret-1' },
      ],
    ]);
  });

  it('rejects a refused pairing request with its code, status and Retry-After', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(
        jsonResponse(
          { ok: false, error: { code: 'rate_limited', message: 'slow down' } },
          { ok: false, status: 429, headers: { 'Retry-After': '42' } },
        ),
      );

    await expect(
      api.startPairing({ name: 'Firefox on Linux' }, { fetch }),
    ).rejects.toMatchObject({
      name: 'ApiClientError',
      code: 'rate_limited',
      status: 429,
      retryAfter: 42,
    });
    // A pairing refusal is the pairing screen's to handle, not an access loss.
    expect(losses).toEqual([]);
  });

  it('rejects an unreachable server as a network error', async () => {
    await expect(
      api.getAuthStatus({
        fetch: vi.fn().mockRejectedValue(new Error('offline')),
      }),
    ).rejects.toMatchObject({ code: api.RPC_ERROR_NETWORK });
  });
});
