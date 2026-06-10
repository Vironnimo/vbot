import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  CONNECTION_STATUS_CONNECTED,
  CONNECTION_STATUS_DISCONNECTED,
  CONNECTION_STATUS_RECONNECTING,
  connect,
  createConnectionState,
  disconnect,
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

  it('same-epoch hello does not block a later event from reaching the handler and bumping lastSequence (resume path unchanged)', () => {
    state.epoch = 'shared-epoch';
    state.lastSequence = 42;

    const onEvent = vi.fn();
    connect(state, {
      _WebSocket: MockWebSocket,
      _baseUrl: 'http://localhost:8420/',
      onEvent,
    });
    latestSocket.emit('open', {});

    // Same-epoch hello: epoch is confirmed, last_sequence is informational.
    // The hello frame must not stop the immediately-following event from being
    // delivered to the handler or from updating lastSequence.
    latestSocket.emit('message', {
      data: JSON.stringify({
        type: 'connection_ready',
        epoch: 'shared-epoch',
        last_sequence: 0,
        active_runs: [],
      }),
    });

    expect(state.epoch).toBe('shared-epoch');
    expect(state.lastSequence).toBe(0);

    latestSocket.emit('message', {
      data: JSON.stringify({ type: 'agent.created', sequence: 7 }),
    });

    expect(state.lastSequence).toBe(7);
    expect(onEvent).toHaveBeenLastCalledWith({
      type: 'agent.created',
      sequence: 7,
    });
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
