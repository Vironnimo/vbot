import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  GATE_APP,
  GATE_CHECKING,
  GATE_DENIED,
  GATE_LOOPING,
  GATE_NOTICE_NOT_KEPT,
  GATE_PAIRING,
  GATE_UNAVAILABLE,
  GATE_UNREACHABLE,
  createAccessGate,
} from '../accessGate.js';

const LOCAL = { mode: 'local', authenticated: true, principal: 'open' };
const SIGNED_OUT = {
  mode: 'authenticated',
  authenticated: false,
  principal: null,
};
const SIGNED_IN = {
  mode: 'authenticated',
  authenticated: true,
  principal: 'device',
};

const failure = (code) => Object.assign(new Error(code), { code });

let gates;

beforeEach(() => {
  vi.useFakeTimers();
  gates = [];
});

afterEach(() => {
  for (const gate of gates) gate.destroy();
  vi.useRealTimers();
});

// A gate over a scripted status endpoint: each answer is a status object, an
// Error to reject with, or `hang` for a request that never settles.
function gateFixture(...answers) {
  const lossListeners = new Set();
  const getAuthStatus = vi.fn(() => {
    const answer = answers.length > 1 ? answers.shift() : answers[0];
    if (answer === 'hang') return new Promise(() => {});
    return answer instanceof Error
      ? Promise.reject(answer)
      : Promise.resolve(answer);
  });
  const beforeLeaveApp = vi.fn();
  const gate = createAccessGate({
    getAuthStatus,
    onAccessLoss: (listener) => {
      lossListeners.add(listener);
      return () => lossListeners.delete(listener);
    },
    beforeLeaveApp,
  });
  gates.push(gate);
  return {
    gate,
    getAuthStatus,
    beforeLeaveApp,
    lose: async (reason = 'auth_required') => {
      for (const listener of lossListeners) listener(reason);
      await vi.advanceTimersByTimeAsync(0);
    },
    listening: () => lossListeners.size,
  };
}

describe('createAccessGate()', () => {
  it.each([
    ['a local server', LOCAL, GATE_APP],
    [
      'a home-network server',
      { mode: 'lan', authenticated: true, principal: 'open' },
      GATE_APP,
    ],
    ['a signed-in device', SIGNED_IN, GATE_APP],
    [
      'the host',
      { mode: 'authenticated', authenticated: true, principal: 'host' },
      GATE_APP,
    ],
    ['a browser without sign-in', SIGNED_OUT, GATE_PAIRING],
    ['a refused client', failure('access_denied'), GATE_DENIED],
    // Pairing again would not help: the sign-in stays and the gate waits.
    [
      'a server that cannot check devices',
      failure('devices_unavailable'),
      GATE_UNAVAILABLE,
    ],
    // The server stays the security boundary: the app shows its own
    // offline state and reconnects.
    ['an unreachable server', failure('network_error'), GATE_APP],
  ])('shows the screen for %s at boot', async (_label, answer, screen) => {
    const f = gateFixture(answer);

    const started = f.gate.start();
    expect(f.gate.state.screen).toBe(GATE_CHECKING);
    await started;

    expect(f.gate.state.screen).toBe(screen);
    expect(f.gate.state.notice).toBeNull();
  });

  it('mounts the app when the boot check does not answer in time', async () => {
    const f = gateFixture('hang');

    void f.gate.start();
    await vi.advanceTimersByTimeAsync(9_999);
    expect(f.gate.state.screen).toBe(GATE_CHECKING);
    await vi.advanceTimersByTimeAsync(1);

    expect(f.gate.state.screen).toBe(GATE_APP);
  });

  it('takes the app down on an access loss and shows what the server says', async () => {
    const f = gateFixture(SIGNED_IN, SIGNED_OUT);
    await f.gate.start();

    await f.lose();
    expect(f.beforeLeaveApp).toHaveBeenCalledOnce();
    expect(f.gate.state.screen).toBe(GATE_PAIRING);

    // Off the app, further losses are the gate screens' own business.
    await f.lose();
    expect(f.getAuthStatus).toHaveBeenCalledTimes(2);
    expect(f.beforeLeaveApp).toHaveBeenCalledOnce();
  });

  it('retries an unreachable server after a loss with a backoff', async () => {
    const f = gateFixture(
      SIGNED_IN,
      failure('network_error'),
      failure('network_error'),
      SIGNED_IN,
    );
    await f.gate.start();

    await f.lose();
    expect(f.gate.state.screen).toBe(GATE_UNREACHABLE);
    await vi.advanceTimersByTimeAsync(1_250);
    expect(f.getAuthStatus).toHaveBeenCalledTimes(3);
    expect(f.gate.state.screen).toBe(GATE_UNREACHABLE);
    // The second wait doubles (2 s ± 25 %).
    await vi.advanceTimersByTimeAsync(1_400);
    expect(f.getAuthStatus).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(1_200);

    expect(f.getAuthStatus).toHaveBeenCalledTimes(4);
    expect(f.gate.state.screen).toBe(GATE_APP);
  });

  it('waits as long as the server asks while it cannot check devices', async () => {
    const f = gateFixture(
      SIGNED_IN,
      Object.assign(failure('devices_unavailable'), { retryAfter: 30 }),
      SIGNED_IN,
    );
    await f.gate.start();

    await f.lose('devices_unavailable');
    expect(f.gate.state.screen).toBe(GATE_UNAVAILABLE);
    await vi.advanceTimersByTimeAsync(29_000);
    expect(f.getAuthStatus).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1_500);

    expect(f.getAuthStatus).toHaveBeenCalledTimes(3);
    expect(f.gate.state.screen).toBe(GATE_APP);
  });

  it('stops after repeated losses right after the server let the app in', async () => {
    const f = gateFixture(SIGNED_IN);
    await f.gate.start();

    await f.lose();
    await vi.advanceTimersByTimeAsync(20_000);
    await f.lose();
    expect(f.gate.state.screen).toBe(GATE_APP);
    await vi.advanceTimersByTimeAsync(20_000);
    await f.lose();

    expect(f.gate.state.screen).toBe(GATE_LOOPING);
    await vi.advanceTimersByTimeAsync(120_000);
    expect(f.getAuthStatus).toHaveBeenCalledTimes(3);

    await f.gate.retry();
    expect(f.gate.state.screen).toBe(GATE_APP);
    // Retry starts the count anew.
    await f.lose();
    expect(f.gate.state.screen).toBe(GATE_APP);
  });

  it('forgets losses older than a minute', async () => {
    const f = gateFixture(SIGNED_IN);
    await f.gate.start();

    for (let loss = 0; loss < 4; loss += 1) {
      await f.lose();
      await vi.advanceTimersByTimeAsync(31_000);
    }

    expect(f.gate.state.screen).toBe(GATE_APP);
  });

  it.each([
    ['kept its sign-in', SIGNED_IN, GATE_APP, null],
    [
      'did not keep its sign-in',
      SIGNED_OUT,
      GATE_PAIRING,
      GATE_NOTICE_NOT_KEPT,
    ],
  ])(
    'checks again after an approved pairing that %s',
    async (_label, answer, screen, notice) => {
      const f = gateFixture(SIGNED_OUT, answer);
      await f.gate.start();

      await f.gate.pairingApproved();

      expect(f.gate.state).toMatchObject({ screen, notice });
    },
  );

  it('stops listening for losses once destroyed', async () => {
    const f = gateFixture(SIGNED_IN);
    await f.gate.start();

    f.gate.destroy();

    expect(f.listening()).toBe(0);
  });
});
