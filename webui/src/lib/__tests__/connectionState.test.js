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
