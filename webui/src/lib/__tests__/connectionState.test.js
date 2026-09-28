import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  CONNECTION_STATUS_CONNECTED,
  CONNECTION_STATUS_DISCONNECTED,
  CONNECTION_STATUS_RECONNECTING,
  connect,
  createConnectionState,
  disconnect,
  handleVisibilityChange,
} from '../connectionState.js';
import { MockWebSocket } from './api.support.js';

// With Math.random() at 0.5 the reconnect jitter is zero: the delay starts at
// 1 s and doubles per failed attempt.
const FIRST_RECONNECT_MS = 1000;
const HEARTBEAT_TIMEOUT_MS = 60000;

let sockets;

class TrackedWebSocket extends MockWebSocket {
  constructor(url) {
    super(url);
    sockets.push(this);
  }
}

const latestSocket = () => sockets.at(-1);
const queryParam = (socket, key) => new URL(socket.url).searchParams.get(key);
const receive = (socket, event) =>
  socket.emit('message', { data: JSON.stringify(event) });
const handlersWith = (handlers = {}) => ({
  _WebSocket: TrackedWebSocket,
  _baseUrl: 'http://localhost:8420/',
  ...handlers,
});

function start(initial = {}, handlers = {}) {
  const state = Object.assign(createConnectionState(), initial);
  connect(state, handlersWith(handlers));
  return state;
}

beforeEach(() => {
  sockets = [];
  vi.useFakeTimers();
  vi.spyOn(Math, 'random').mockReturnValue(0.5);
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('connect()', () => {
  it('starts reconnecting from an empty cursor with only public fields', () => {
    expect(createConnectionState()).toEqual({
      status: CONNECTION_STATUS_RECONNECTING,
      lastSequence: 0,
      epoch: '',
    });
  });

  it('reports open and close, and reconnects with a backoff that resets after an open', () => {
    const onStatusChange = vi.fn();
    const state = start({}, { onStatusChange });
    expect(state.status).toBe(CONNECTION_STATUS_RECONNECTING);

    latestSocket().emit('open', {});
    expect(state.status).toBe(CONNECTION_STATUS_CONNECTED);
    latestSocket().emit('close', {});
    expect(state.status).toBe(CONNECTION_STATUS_DISCONNECTED);
    expect(onStatusChange).toHaveBeenCalledTimes(2);

    vi.advanceTimersByTime(FIRST_RECONNECT_MS - 1);
    expect(sockets).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(sockets).toHaveLength(2);

    // A second failure without an open waits twice as long.
    latestSocket().emit('close', {});
    vi.advanceTimersByTime(2 * FIRST_RECONNECT_MS - 1);
    expect(sockets).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(sockets).toHaveLength(3);

    // An open resets the backoff.
    latestSocket().emit('open', {});
    latestSocket().emit('close', {});
    vi.advanceTimersByTime(FIRST_RECONNECT_MS);
    expect(sockets).toHaveLength(4);
  });

  it('keeps reconnecting when the WebSocket constructor throws', () => {
    let attempts = 0;
    class ThrowOnceWebSocket extends TrackedWebSocket {
      constructor(url) {
        attempts += 1;
        if (attempts === 1) throw new Error('constructor failed');
        super(url);
      }
    }
    const onError = vi.fn();
    const onStatusChange = vi.fn();
    const state = createConnectionState();

    connect(state, {
      ...handlersWith({ onError, onStatusChange }),
      _WebSocket: ThrowOnceWebSocket,
    });

    expect(state.status).toBe(CONNECTION_STATUS_DISCONNECTED);
    expect(onStatusChange).toHaveBeenCalledOnce();
    expect(onError).toHaveBeenCalledOnce();
    vi.advanceTimersByTime(FIRST_RECONNECT_MS);
    expect(sockets).toHaveLength(1);
  });

  it.each([
    ['a fresh state', {}, { after_sequence: null, epoch: null }],
    [
      'a known cursor and epoch',
      { lastSequence: 5, epoch: 'epoch-xyz' },
      { after_sequence: '5', epoch: 'epoch-xyz' },
    ],
  ])('connects %s with its resume position', (_label, initial, query) => {
    start(initial);

    for (const [key, value] of Object.entries(query)) {
      expect(queryParam(latestSocket(), key)).toBe(value);
    }
  });

  it('advances the cursor only to higher sequences and resumes after it', () => {
    const onEvent = vi.fn();
    const state = start({}, { onEvent });
    latestSocket().emit('open', {});

    for (const sequence of [3, 7, 5]) {
      receive(latestSocket(), { type: 'agent.updated', sequence });
    }

    expect(state.lastSequence).toBe(7);
    expect(onEvent.mock.calls.map(([event]) => event.sequence)).toEqual([
      3, 7, 5,
    ]);
    latestSocket().emit('close', {});
    vi.advanceTimersByTime(FIRST_RECONNECT_MS);
    expect(queryParam(latestSocket(), 'after_sequence')).toBe('7');
  });

  it('replaces a pending reconnect when connected explicitly', () => {
    const state = start();
    latestSocket().emit('open', {});
    latestSocket().emit('close', {});

    connect(state, handlersWith());
    vi.advanceTimersByTime(10 * FIRST_RECONNECT_MS);

    expect(sockets).toHaveLength(2);
  });
});

describe('disconnect()', () => {
  it('closes an open socket and cancels a pending reconnect', () => {
    const state = start();
    latestSocket().emit('open', {});

    disconnect(state);
    expect(state.status).toBe(CONNECTION_STATUS_DISCONNECTED);
    expect(latestSocket().closeCalls).toHaveLength(1);

    connect(state, handlersWith());
    latestSocket().emit('open', {});
    latestSocket().emit('close', {});
    disconnect(state);
    vi.advanceTimersByTime(HEARTBEAT_TIMEOUT_MS);
    expect(sockets).toHaveLength(2);
  });
});

describe('stalled connections', () => {
  it('closes a socket that received nothing for 60 s and reconnects', () => {
    const onEvent = vi.fn();
    start({}, { onEvent });
    const stalled = latestSocket();
    stalled.emit('open', {});

    // A heartbeat restarts the watchdog without reaching the handler.
    vi.advanceTimersByTime(HEARTBEAT_TIMEOUT_MS / 2);
    receive(stalled, { type: 'heartbeat' });
    vi.advanceTimersByTime(HEARTBEAT_TIMEOUT_MS / 2);
    expect(stalled.closeCalls).toHaveLength(0);
    expect(onEvent).not.toHaveBeenCalled();

    vi.advanceTimersByTime(HEARTBEAT_TIMEOUT_MS / 2);
    expect(stalled.closeCalls).toHaveLength(1);
    stalled.emit('close', {});
    vi.advanceTimersByTime(FIRST_RECONNECT_MS);
    expect(latestSocket()).not.toBe(stalled);
  });

  it.each([
    ['visible', 1],
    ['hidden', 0],
  ])(
    'closes the socket of a %s tab silent for over 30 s %i time(s) on a visibility change',
    (visibilityState, closes) => {
      vi.stubGlobal('document', { visibilityState });
      const state = start();
      latestSocket().emit('open', {});

      vi.advanceTimersByTime(HEARTBEAT_TIMEOUT_MS / 2 + 1);
      handleVisibilityChange(state);

      expect(latestSocket().closeCalls).toHaveLength(closes);
    },
  );
});

describe('connection_ready', () => {
  // Each case: the cursor before the hello, the hello, the cursor it leaves,
  // then the next live event, which must be delivered and becomes the resume
  // position of the following reconnect.
  it.each([
    [
      'a new epoch resets a long-lived cursor',
      { lastSequence: 3000 },
      { epoch: 'new-epoch', last_sequence: 0, active_runs: [] },
      0,
      1,
    ],
    [
      'a same-epoch hello keeps the cursor until the replay arrives',
      { lastSequence: 42, epoch: 'shared-epoch' },
      { epoch: 'shared-epoch', last_sequence: 50, active_runs: [] },
      42,
      43,
    ],
    [
      'a resumed replay keeps the cursor',
      { lastSequence: 42, epoch: 'shared-epoch' },
      { epoch: 'shared-epoch', last_sequence: 50, replay_status: 'resumed' },
      42,
      43,
    ],
    [
      'a replay gap adopts the hello high-water mark',
      { lastSequence: 42, epoch: 'shared-epoch' },
      { epoch: 'shared-epoch', last_sequence: 50, replay_status: 'gap' },
      50,
      51,
    ],
    [
      'a hello without last_sequence resets the cursor',
      { lastSequence: 99 },
      { epoch: 'partial-epoch' },
      0,
      1,
    ],
  ])('%s', (_label, initial, hello, cursorAfterHello, nextSequence) => {
    const onEvent = vi.fn();
    const state = start(initial, { onEvent });
    latestSocket().emit('open', {});

    receive(latestSocket(), { type: 'connection_ready', ...hello });
    expect(state.epoch).toBe(hello.epoch);
    expect(state.lastSequence).toBe(cursorAfterHello);
    expect(onEvent).toHaveBeenCalledExactlyOnceWith({
      type: 'connection_ready',
      ...hello,
    });

    const next = { type: 'agent.created', sequence: nextSequence };
    receive(latestSocket(), next);
    expect(onEvent).toHaveBeenLastCalledWith(next);
    latestSocket().emit('close', {});
    vi.advanceTimersByTime(FIRST_RECONNECT_MS);
    expect(queryParam(latestSocket(), 'after_sequence')).toBe(
      String(nextSequence),
    );
    expect(queryParam(latestSocket(), 'epoch')).toBe(hello.epoch);
  });
});
