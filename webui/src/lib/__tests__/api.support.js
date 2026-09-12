import { afterEach, vi } from 'vitest';

afterEach(() => {
  vi.unstubAllGlobals();
});

function jsonResponse(body, options = {}) {
  return {
    ok: options.ok ?? true,
    status: options.status ?? 500,
    json: vi.fn().mockResolvedValue(body),
    headers: new Headers(options.headers),
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

export { jsonResponse, MockEventSource, MockWebSocket };
