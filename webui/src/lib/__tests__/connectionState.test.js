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

let latestSocket;

class MockWebSocket {
  constructor(url) {
    this.url = url;
    this.closeCalls = [];
    this.listeners = new Map();
    latestSocket = this;
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

describe('createConnectionState()', () => {
  it('returns default state with status RECONNECTING and lastSequence 0', () => {
    const state = createConnectionState();
    expect(state.status).toBe(CONNECTION_STATUS_RECONNECTING);
    expect(state.lastSequence).toBe(0);
    expect(state.epoch).toBe('');
    expect(state._connection).toBeNull();
    expect(state._reconnectTimer).toBeNull();
    expect(state._reconnectAttempt).toBe(0);
  });
});

describe('connect()', () => {
  let state;

  beforeEach(() => {
    vi.useFakeTimers();
    state = createConnectionState();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('opens a connection and sets status to CONNECTED on open', () => {
    const onStatusChange = vi.fn();
    connect(state, {
      _WebSocket: MockWebSocket,
      _baseUrl: 'http://localhost:8420/',
      onStatusChange,
    });

    expect(state.status).toBe(CONNECTION_STATUS_RECONNECTING);

    latestSocket.emit('open', {});
    expect(state.status).toBe(CONNECTION_STATUS_CONNECTED);
    expect(onStatusChange).toHaveBeenCalledTimes(1);
  });

  it('keeps reconnecting when the WebSocket constructor throws', () => {
    let attempts = 0;
    const onError = vi.fn();

    class ThrowOnceWebSocket extends MockWebSocket {
      constructor(url) {
        attempts += 1;
        if (attempts === 1) {
          throw new Error('constructor failed');
        }
        super(url);
      }
    }

    connect(state, {
      _WebSocket: ThrowOnceWebSocket,
      _baseUrl: 'http://localhost:8420/',
      onError,
    });

    expect(state.status).toBe(CONNECTION_STATUS_DISCONNECTED);
    expect(state._reconnectTimer).not.toBeNull();
    expect(onError).toHaveBeenCalledOnce();

    vi.advanceTimersByTime(2000);

    expect(attempts).toBe(2);
    expect(state._connection).not.toBeNull();
  });

  it('resets reconnect attempt counter on open', () => {
    state._reconnectAttempt = 3;
    connect(state, {
      _WebSocket: MockWebSocket,
      _baseUrl: 'http://localhost:8420/',
    });

    latestSocket.emit('open', {});
    expect(state._reconnectAttempt).toBe(0);
  });

  it('passes afterSequence based on lastSequence in WebSocket URL', () => {
    state.lastSequence = 5;
    connect(state, {
      _WebSocket: MockWebSocket,
      _baseUrl: 'http://localhost:8420/',
    });

    expect(latestSocket.url).toContain('after_sequence=5');
  });

  it('does not include after_sequence when lastSequence is 0', () => {
    connect(state, {
      _WebSocket: MockWebSocket,
      _baseUrl: 'http://localhost:8420/',
    });

    expect(latestSocket.url).not.toContain('after_sequence');
  });

  it('updates lastSequence on event with higher sequence', () => {
    const onEvent = vi.fn();
    connect(state, {
      _WebSocket: MockWebSocket,
      _baseUrl: 'http://localhost:8420/',
      onEvent,
    });

    latestSocket.emit('open', {});

    latestSocket.emit('message', {
      data: JSON.stringify({ type: 'agent.created', sequence: 5 }),
    });

    expect(state.lastSequence).toBe(5);
    expect(onEvent).toHaveBeenCalledWith({
      type: 'agent.created',
      sequence: 5,
    });
  });

  it('does not downgrade lastSequence on event with lower sequence', () => {
    state.lastSequence = 10;
    connect(state, {
      _WebSocket: MockWebSocket,
      _baseUrl: 'http://localhost:8420/',
    });

    latestSocket.emit('open', {});

    latestSocket.emit('message', {
      data: JSON.stringify({ type: 'agent.updated', sequence: 3 }),
    });

    expect(state.lastSequence).toBe(10);
  });

  it('updates lastSequence only when event.sequence is greater', () => {
    connect(state, {
      _WebSocket: MockWebSocket,
      _baseUrl: 'http://localhost:8420/',
    });

    latestSocket.emit('open', {});

    latestSocket.emit('message', {
      data: JSON.stringify({ type: 'agent.created', sequence: 3 }),
    });
    expect(state.lastSequence).toBe(3);

    latestSocket.emit('message', {
      data: JSON.stringify({ type: 'agent.updated', sequence: 7 }),
    });
    expect(state.lastSequence).toBe(7);

    latestSocket.emit('message', {
      data: JSON.stringify({ type: 'agent.deleted', sequence: 5 }),
    });
    expect(state.lastSequence).toBe(7);
  });
});

describe('disconnect()', () => {
  let state;

  beforeEach(() => {
    vi.useFakeTimers();
    state = createConnectionState();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('sets status to DISCONNECTED and cleans up connection', () => {
    connect(state, {
      _WebSocket: MockWebSocket,
      _baseUrl: 'http://localhost:8420/',
    });
    latestSocket.emit('open', {});

    expect(state.status).toBe(CONNECTION_STATUS_CONNECTED);

    disconnect(state);

    expect(state.status).toBe(CONNECTION_STATUS_DISCONNECTED);
    expect(state._connection).toBeNull();
    expect(state._reconnectTimer).toBeNull();
  });

  it('cancels pending reconnect timer', () => {
    connect(state, {
      _WebSocket: MockWebSocket,
      _baseUrl: 'http://localhost:8420/',
    });
    latestSocket.emit('open', {});
    latestSocket.emit('close', {});

    expect(state._reconnectTimer).not.toBeNull();

    disconnect(state);

    expect(state._reconnectTimer).toBeNull();
  });
});

describe('reconnect', () => {
  let state;

  beforeEach(() => {
    vi.useFakeTimers();
    state = createConnectionState();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('schedules reconnect after close', () => {
    const onStatusChange = vi.fn();
    connect(state, {
      _WebSocket: MockWebSocket,
      _baseUrl: 'http://localhost:8420/',
      onStatusChange,
    });

    const initialSocket = latestSocket;
    initialSocket.emit('open', {});
    expect(state.status).toBe(CONNECTION_STATUS_CONNECTED);

    initialSocket.emit('close', {});
    expect(state.status).toBe(CONNECTION_STATUS_DISCONNECTED);
    expect(state._reconnectTimer).not.toBeNull();

    vi.advanceTimersByTime(2000);

    expect(state._reconnectTimer).toBeNull();
    expect(latestSocket).not.toBe(initialSocket);
    expect(state._connection).not.toBeNull();
  });

  it('increments reconnect attempt on each scheduled reconnect', () => {
    connect(state, {
      _WebSocket: MockWebSocket,
      _baseUrl: 'http://localhost:8420/',
    });
    latestSocket.emit('open', {});
    latestSocket.emit('close', {});

    expect(state._reconnectAttempt).toBe(1);

    vi.advanceTimersByTime(2000);

    latestSocket.emit('close', {});
    expect(state._reconnectAttempt).toBe(2);
  });

  it('resets reconnect attempt after successful connection', () => {
    connect(state, {
      _WebSocket: MockWebSocket,
      _baseUrl: 'http://localhost:8420/',
    });
    latestSocket.emit('open', {});
    latestSocket.emit('close', {});

    expect(state._reconnectAttempt).toBe(1);

    vi.advanceTimersByTime(2000);
    latestSocket.emit('open', {});

    expect(state._reconnectAttempt).toBe(0);
  });

  it('cancels pending reconnect timer on connect', () => {
    connect(state, {
      _WebSocket: MockWebSocket,
      _baseUrl: 'http://localhost:8420/',
    });
    latestSocket.emit('open', {});
    latestSocket.emit('close', {});

    expect(state._reconnectTimer).not.toBeNull();

    connect(state, {
      _WebSocket: MockWebSocket,
      _baseUrl: 'http://localhost:8420/',
    });

    expect(state._reconnectTimer).toBeNull();
  });

  it('stops reconnecting after disconnect', () => {
    connect(state, {
      _WebSocket: MockWebSocket,
      _baseUrl: 'http://localhost:8420/',
    });
    latestSocket.emit('open', {});
    latestSocket.emit('close', {});

    expect(state._reconnectTimer).not.toBeNull();

    disconnect(state);

    expect(state._reconnectTimer).toBeNull();

    vi.advanceTimersByTime(60000);

    expect(latestSocket.url).not.toContain('after_sequence');
  });

  it('uses afterSequence from lastSequence on reconnect', () => {
    connect(state, {
      _WebSocket: MockWebSocket,
      _baseUrl: 'http://localhost:8420/',
    });

    latestSocket.emit('open', {});

    latestSocket.emit('message', {
      data: JSON.stringify({ type: 'agent.created', sequence: 42 }),
    });
    expect(state.lastSequence).toBe(42);

    latestSocket.emit('close', {});

    vi.advanceTimersByTime(2000);

    expect(latestSocket.url).toContain('after_sequence=42');
  });

  it('reconnects after the heartbeat watchdog closes a stalled socket', () => {
    connect(state, {
      _WebSocket: MockWebSocket,
      _baseUrl: 'http://localhost:8420/',
    });
    const initialSocket = latestSocket;
    initialSocket.emit('open', {});

    vi.advanceTimersByTime(60000);
    expect(initialSocket.closeCalls).toHaveLength(1);

    initialSocket.emit('close', {});
    expect(state._reconnectTimer).not.toBeNull();
    vi.advanceTimersByTime(2000);
    expect(latestSocket).not.toBe(initialSocket);
  });

  it('reconnects when returning to a visible stalled tab', () => {
    vi.stubGlobal('document', { visibilityState: 'visible' });
    try {
      connect(state, {
        _WebSocket: MockWebSocket,
        _baseUrl: 'http://localhost:8420/',
      });
      const initialSocket = latestSocket;
      initialSocket.emit('open', {});

      vi.advanceTimersByTime(30001);
      handleVisibilityChange(state);
      expect(initialSocket.closeCalls).toHaveLength(1);
      initialSocket.emit('close', {});
      expect(state._reconnectTimer).not.toBeNull();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe('connection_ready handling', () => {
  let state;

  beforeEach(() => {
    vi.useFakeTimers();
    state = createConnectionState();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('sets epoch and lastSequence from a different-epoch connection_ready, then a sequence-1 event is delivered and bumps lastSequence (regression for B1 client half)', () => {
    // Simulate a long-lived tab that saw events up to sequence 3000.
    state.lastSequence = 3000;

    const onEvent = vi.fn();
    connect(state, {
      _WebSocket: MockWebSocket,
      _baseUrl: 'http://localhost:8420/',
      onEvent,
    });
    latestSocket.emit('open', {});

    // Server restarted; new epoch, last_sequence reset to 0.
    latestSocket.emit('message', {
      data: JSON.stringify({
        type: 'connection_ready',
        epoch: 'new-epoch-abc',
        last_sequence: 0,
        active_runs: [],
      }),
    });

    expect(state.epoch).toBe('new-epoch-abc');
    expect(state.lastSequence).toBe(0);
    expect(onEvent).toHaveBeenCalledWith({
      type: 'connection_ready',
      epoch: 'new-epoch-abc',
      last_sequence: 0,
      active_runs: [],
    });

    // The next live event from the new epoch is sequence 1; it must reach the
    // handler and bump lastSequence — proves the client didn't get stuck at 3000.
    latestSocket.emit('message', {
      data: JSON.stringify({ type: 'agent.created', sequence: 1 }),
    });

    expect(state.lastSequence).toBe(1);
    expect(onEvent).toHaveBeenLastCalledWith({
      type: 'agent.created',
      sequence: 1,
    });
  });

  it('same-epoch hello does not acknowledge replay events before they arrive', () => {
    state.epoch = 'shared-epoch';
    state.lastSequence = 42;

    const onEvent = vi.fn();
    connect(state, {
      _WebSocket: MockWebSocket,
      _baseUrl: 'http://localhost:8420/',
      onEvent,
    });
    latestSocket.emit('open', {});

    // Same-epoch hello: the high-water mark includes replay events 43..50 that
    // have not reached this client yet, so it must not advance the cursor.
    latestSocket.emit('message', {
      data: JSON.stringify({
        type: 'connection_ready',
        epoch: 'shared-epoch',
        last_sequence: 50,
        active_runs: [],
      }),
    });

    expect(state.epoch).toBe('shared-epoch');
    expect(state.lastSequence).toBe(42);

    latestSocket.emit('message', {
      data: JSON.stringify({ type: 'agent.created', sequence: 43 }),
    });

    expect(state.lastSequence).toBe(43);
    expect(onEvent).toHaveBeenLastCalledWith({
      type: 'agent.created',
      sequence: 43,
    });

    // If this replay connection drops now, resume after the last event that
    // actually arrived, not the hello's unacknowledged high-water mark.
    latestSocket.emit('close', {});
    vi.advanceTimersByTime(2000);
    expect(latestSocket.url).toContain('after_sequence=43');
    expect(latestSocket.url).not.toContain('after_sequence=50');
  });

  it('preserves the client cursor when replay_status confirms a complete resume', () => {
    state.epoch = 'shared-epoch';
    state.lastSequence = 42;

    connect(state, {
      _WebSocket: MockWebSocket,
      _baseUrl: 'http://localhost:8420/',
    });
    latestSocket.emit('open', {});
    latestSocket.emit('message', {
      data: JSON.stringify({
        type: 'connection_ready',
        epoch: 'shared-epoch',
        last_sequence: 50,
        replay_status: 'resumed',
      }),
    });

    expect(state.lastSequence).toBe(42);
  });

  it('accepts the hello high-water mark when replay_status reports a gap', () => {
    state.epoch = 'shared-epoch';
    state.lastSequence = 42;

    connect(state, {
      _WebSocket: MockWebSocket,
      _baseUrl: 'http://localhost:8420/',
    });
    latestSocket.emit('open', {});
    latestSocket.emit('message', {
      data: JSON.stringify({
        type: 'connection_ready',
        epoch: 'shared-epoch',
        last_sequence: 50,
        replay_status: 'gap',
      }),
    });

    expect(state.lastSequence).toBe(50);
  });

  it('treats a missing last_sequence on connection_ready as 0 and still updates epoch', () => {
    state.lastSequence = 99;

    const onEvent = vi.fn();
    connect(state, {
      _WebSocket: MockWebSocket,
      _baseUrl: 'http://localhost:8420/',
      onEvent,
    });
    latestSocket.emit('open', {});

    latestSocket.emit('message', {
      data: JSON.stringify({
        type: 'connection_ready',
        epoch: 'partial-epoch',
        // no last_sequence
      }),
    });

    expect(state.epoch).toBe('partial-epoch');
    expect(state.lastSequence).toBe(0);
    expect(onEvent).toHaveBeenCalledTimes(1);
  });

  it('passes state.epoch through to the WebSocket URL when non-empty', () => {
    state.epoch = 'epoch-xyz';

    connect(state, {
      _WebSocket: MockWebSocket,
      _baseUrl: 'http://localhost:8420/',
    });

    expect(latestSocket.url).toContain('epoch=epoch-xyz');
  });

  it('omits the epoch query param when state.epoch is empty', () => {
    connect(state, {
      _WebSocket: MockWebSocket,
      _baseUrl: 'http://localhost:8420/',
    });

    expect(latestSocket.url).not.toContain('epoch=');
  });
});
