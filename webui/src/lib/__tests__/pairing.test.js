import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  PAIRING_APPROVED,
  PAIRING_DENIED,
  PAIRING_EXPIRED,
  PAIRING_FAILED,
  PAIRING_RATE_LIMITED,
  PAIRING_STARTING,
  PAIRING_WAITING,
  createPairing,
  describeBrowserDevice,
} from '../pairing.js';

const STARTED = {
  request_id: 'request-1',
  code: 'K7QM-4XPA',
  poll_secret: 'secret-1',
  expires_in: 600,
  poll_interval: 2,
};

const failure = (code, extra = {}) =>
  Object.assign(new Error(code), { code, ...extra });

function deferred() {
  let resolve;
  const promise = new Promise((done) => (resolve = done));
  return { promise, resolve };
}

let pairings;

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-17T12:00:00Z'));
  pairings = [];
});

afterEach(() => {
  for (const pairing of pairings) pairing.destroy();
  vi.useRealTimers();
});

// A pairing over scripted endpoints: each answer is a response object or an
// Error to reject with; the last answer repeats.
function pairingFixture({
  start = [STARTED],
  poll = [{ status: 'pending' }],
} = {}) {
  const script = (answers) =>
    vi.fn(() => {
      const answer = answers.length > 1 ? answers.shift() : answers[0];
      return answer instanceof Error
        ? Promise.reject(answer)
        : Promise.resolve(answer);
    });
  const startPairing = script(start);
  const pollPairing = script(poll);
  const onApproved = vi.fn();
  const onRecheck = vi.fn();
  const pairing = createPairing({
    startPairing,
    pollPairing,
    deviceName: 'Firefox on Linux',
    onApproved,
    onRecheck,
  });
  pairings.push(pairing);
  return { pairing, startPairing, pollPairing, onApproved, onRecheck };
}

describe('createPairing()', () => {
  it('requests a code as a browser and polls it until approval', async () => {
    const device = { id: 'dev-1', name: 'Firefox on Linux' };
    const f = pairingFixture({
      poll: [
        { status: 'pending', expires_in: 598 },
        { status: 'approved', device },
      ],
    });

    const started = f.pairing.start();
    expect(f.pairing.state.phase).toBe(PAIRING_STARTING);
    await started;

    expect(f.startPairing).toHaveBeenCalledExactlyOnceWith(
      { kind: 'browser', name: 'Firefox on Linux' },
      { signal: expect.any(AbortSignal) },
    );
    expect(f.pairing.state).toEqual({
      phase: PAIRING_WAITING,
      code: 'K7QM-4XPA',
      expiresAt: Date.now() + 600_000,
      unstable: false,
    });
    // The poll secret stays out of the state.
    expect(JSON.stringify(f.pairing.state)).not.toContain('secret-1');

    await vi.advanceTimersByTimeAsync(1_999);
    expect(f.pollPairing).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(f.pollPairing).toHaveBeenCalledWith(
      { requestId: 'request-1', pollSecret: 'secret-1' },
      { signal: expect.any(AbortSignal) },
    );
    expect(f.pairing.state.phase).toBe(PAIRING_WAITING);
    await vi.advanceTimersByTimeAsync(2_000);

    expect(f.pairing.state).toEqual({ phase: PAIRING_APPROVED, device });
    expect(f.onApproved).toHaveBeenCalledExactlyOnceWith(device);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(f.pollPairing).toHaveBeenCalledTimes(2);
  });

  it.each([
    ['denied', { status: 'denied' }, PAIRING_DENIED],
    ['expired', { status: 'expired' }, PAIRING_EXPIRED],
    ['malformed', { status: 'maybe' }, PAIRING_FAILED],
  ])(
    'ends a request the server answers as %s',
    async (_label, answer, phase) => {
      const f = pairingFixture({ poll: [answer] });
      await f.pairing.start();

      await vi.advanceTimersByTimeAsync(2_000);

      expect(f.pairing.state.phase).toBe(phase);
      await vi.advanceTimersByTimeAsync(60_000);
      expect(f.pollPairing).toHaveBeenCalledOnce();
      expect(f.onApproved).not.toHaveBeenCalled();
    },
  );

  it('starts again with a new code after a request ended', async () => {
    const f = pairingFixture({
      start: [
        STARTED,
        { ...STARTED, request_id: 'request-2', code: 'ZZZZ-2222' },
      ],
      poll: [{ status: 'expired' }],
    });
    await f.pairing.start();
    await vi.advanceTimersByTimeAsync(2_000);

    await f.pairing.start();

    expect(f.pairing.state).toMatchObject({
      phase: PAIRING_WAITING,
      code: 'ZZZZ-2222',
    });
  });

  it('waits out a rate-limited start before it can start again', async () => {
    const f = pairingFixture({
      start: [
        failure('rate_limited', { status: 429, retryAfter: 60 }),
        STARTED,
      ],
    });

    await f.pairing.start();
    expect(f.pairing.state).toEqual({
      phase: PAIRING_RATE_LIMITED,
      retryAt: Date.now() + 60_000,
      canRetry: false,
    });
    await f.pairing.start();
    expect(f.startPairing).toHaveBeenCalledOnce();

    await vi.advanceTimersByTimeAsync(60_000);
    expect(f.pairing.state.canRetry).toBe(true);
    await f.pairing.start();
    expect(f.pairing.state.phase).toBe(PAIRING_WAITING);
  });

  it('slows its polling as a rate-limited poll asks', async () => {
    const f = pairingFixture({
      poll: [
        failure('rate_limited', { status: 429, retryAfter: 10 }),
        { status: 'pending' },
      ],
    });
    await f.pairing.start();

    await vi.advanceTimersByTimeAsync(2_000);
    expect(f.pairing.state.phase).toBe(PAIRING_WAITING);
    await vi.advanceTimersByTimeAsync(9_999);
    expect(f.pollPairing).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(1);

    expect(f.pollPairing).toHaveBeenCalledTimes(2);
  });

  it('keeps polling through server outages until the code expires', async () => {
    const stalled = deferred();
    const f = pairingFixture({
      start: [{ ...STARTED, expires_in: 60 }],
      poll: [
        stalled.promise,
        { status: 'pending' },
        failure('network_error', { status: 502 }),
      ],
    });
    await f.pairing.start();

    await vi.advanceTimersByTimeAsync(2_000);
    const pollSignal = f.pollPairing.mock.calls[0][1].signal;
    await vi.advanceTimersByTimeAsync(10_000);
    expect(pollSignal.aborted).toBe(true);
    expect(f.pairing.state).toMatchObject({
      phase: PAIRING_WAITING,
      unstable: true,
    });
    await vi.advanceTimersByTimeAsync(2_500);
    expect(f.pollPairing).toHaveBeenCalledTimes(2);
    expect(f.pollPairing.mock.calls.map(([payload]) => payload)).toEqual([
      { requestId: 'request-1', pollSecret: 'secret-1' },
      { requestId: 'request-1', pollSecret: 'secret-1' },
    ]);
    expect(f.pairing.state.unstable).toBe(false);

    // A response from the timed-out attempt cannot end the current request.
    stalled.resolve({ status: 'denied' });
    await vi.advanceTimersByTimeAsync(0);
    expect(f.pairing.state.phase).toBe(PAIRING_WAITING);

    // Every later poll fails: the local deadline ends the request.
    await vi.advanceTimersByTimeAsync(45_000);
    expect(f.pairing.state.phase).toBe(PAIRING_WAITING);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(f.pairing.state.phase).toBe(PAIRING_EXPIRED);
  });

  it('releases a stalled start for retry and ignores its late response', async () => {
    const stalled = deferred();
    const f = pairingFixture({ start: [stalled.promise, STARTED] });
    const starting = f.pairing.start();
    await vi.advanceTimersByTimeAsync(10_000);
    await starting;

    expect(f.startPairing.mock.calls[0][1].signal.aborted).toBe(true);
    expect(f.pairing.state).toEqual({
      phase: PAIRING_FAILED,
      error: 'network_error',
    });
    await f.pairing.start();
    stalled.resolve({ ...STARTED, code: 'OLD2-OLD2' });
    await vi.advanceTimersByTimeAsync(0);
    expect(f.pairing.state).toMatchObject({
      phase: PAIRING_WAITING,
      code: STARTED.code,
    });
  });

  it.each([
    [
      'a start the server cannot store',
      'start',
      failure('devices_unavailable', { status: 503 }),
      'devices_unavailable',
    ],
    [
      'an unreachable server at start',
      'start',
      failure('network_error'),
      'network_error',
    ],
    [
      'a malformed start answer',
      'start',
      { code: 'K7QM-4XPA' },
      'invalid_response',
    ],
    [
      'a poll the server cannot store',
      'poll',
      failure('devices_unavailable', { status: 503 }),
      'devices_unavailable',
    ],
  ])('fails on %s', async (_label, endpoint, answer, error) => {
    const f = pairingFixture({ [endpoint]: [answer] });

    await f.pairing.start();
    await vi.advanceTimersByTimeAsync(2_000);

    expect(f.pairing.state).toMatchObject({ phase: PAIRING_FAILED, error });
    if (error === 'devices_unavailable') {
      expect(f.pairing.state.canRetry).toBe(false);
      await f.pairing.start();
      expect(f.startPairing).toHaveBeenCalledOnce();
      await vi.advanceTimersByTimeAsync(30_000);
      expect(f.pairing.state.canRetry).toBe(true);
    }
  });

  it.each([
    ['sign-in turned off at start', 'start', 'auth_disabled'],
    ['sign-in turned off while waiting', 'poll', 'auth_disabled'],
    ['a refused client', 'start', 'access_denied'],
  ])('hands back to the gate on %s', async (_label, endpoint, code) => {
    const f = pairingFixture({ [endpoint]: [failure(code, { status: 409 })] });

    await f.pairing.start();
    await vi.advanceTimersByTimeAsync(2_000);

    expect(f.onRecheck).toHaveBeenCalledOnce();
    expect(f.pairing.state.phase).toBeNull();
  });

  it.each(['start', 'poll'])(
    'aborts an in-flight %s and stops once destroyed',
    async (endpoint) => {
      const stalled = deferred();
      const f = pairingFixture({ [endpoint]: [stalled.promise] });
      const starting = f.pairing.start();
      await vi.advanceTimersByTimeAsync(endpoint === 'poll' ? 2_000 : 0);
      const request = endpoint === 'start' ? f.startPairing : f.pollPairing;
      const signal = request.mock.calls[0][1].signal;

      f.pairing.destroy();
      expect(signal.aborted).toBe(true);
      await starting;
      stalled.resolve(endpoint === 'start' ? STARTED : { status: 'approved' });
      await vi.advanceTimersByTimeAsync(60_000);
      expect(f.pollPairing).toHaveBeenCalledTimes(endpoint === 'poll' ? 1 : 0);
      expect(f.onApproved).not.toHaveBeenCalled();
      await f.pairing.start();
      expect(f.startPairing).toHaveBeenCalledOnce();
    },
  );
});

describe('describeBrowserDevice()', () => {
  it.each([
    [
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0',
      undefined,
      { browser: 'Edge', os: 'Windows' },
    ],
    [
      'Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0',
      undefined,
      { browser: 'Firefox', os: 'Linux' },
    ],
    [
      'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1',
      undefined,
      { browser: 'Safari', os: 'iOS' },
    ],
    [
      'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36',
      { platform: 'Android' },
      { browser: 'Chrome', os: 'Android' },
    ],
    [
      'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36',
      { platform: 'macOS' },
      { browser: 'Chrome', os: 'macOS' },
    ],
    ['curl/8.0', undefined, { browser: '', os: '' }],
  ])(
    'names the browser and system of %s',
    (userAgent, userAgentData, expected) => {
      expect(describeBrowserDevice({ userAgent, userAgentData })).toEqual(
        expected,
      );
    },
  );
});
