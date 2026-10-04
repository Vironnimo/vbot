import { describe, expect, it, vi } from 'vitest';
import {
  LIVE_SOCKET_ERROR_RESPONSE,
  RPC_ERROR_INVALID_CLIENT_REQUEST,
  RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
  RUN_EVENT_PROVIDER_HEARTBEAT,
  RUN_EVENT_REASONING_DELTA,
  RUN_EVENT_STREAM_ATTEMPT_RESTARTED,
  RUN_EVENT_TOOL_CALL_DELTA,
  RUN_EVENT_TOOL_CALL_STDERR,
  RUN_EVENT_TOOL_CALL_STDOUT,
  RUN_EVENT_TYPES,
  RUN_STREAM_HEARTBEAT_EVENT,
  SSE_ERROR_RESPONSE,
  WEBSOCKET_ERROR_RESPONSE,
  openLiveCallSocket,
  subscribeLogEvents,
  subscribeRunEvents,
  subscribeServerEvents,
  subscribeTerminalEvents,
} from '../api.js';
import { MockEventSource, MockWebSocket } from './api.support.js';

const BASE_URL = 'https://localhost:8420/';

describe('subscribeRunEvents()', () => {
  it('subscribes to every streamed Run event type by default', () => {
    expect(RUN_EVENT_TYPES).toEqual(
      expect.arrayContaining([
        RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
        RUN_EVENT_REASONING_DELTA,
        RUN_EVENT_STREAM_ATTEMPT_RESTARTED,
        RUN_EVENT_TOOL_CALL_DELTA,
        RUN_EVENT_TOOL_CALL_STDOUT,
        RUN_EVENT_TOOL_CALL_STDERR,
        RUN_EVENT_PROVIDER_HEARTBEAT,
        'model_fallback_activated',
        'error_message_persisted',
        'compaction_started',
        'compaction_aborted',
        'compaction_completed',
        'subagent_session_started',
        'subagent_status_changed',
        'model_step_usage',
      ]),
    );
  });

  it('delivers named SSE Run events and closes on a terminal event', () => {
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
    expect(onEvent.mock.calls.map(([event]) => event)).toEqual([
      {
        type: 'reasoning',
        data: { payload: { text: 'thinking' } },
        rawEvent: expect.any(Object),
      },
      {
        type: 'run_completed',
        data: { payload: { status: 'done' } },
        rawEvent: expect.any(Object),
      },
    ]);
    expect(connection.source.closeCount).toBe(1);
    expect(onError).not.toHaveBeenCalled();
  });

  it.each([
    [RUN_EVENT_ASSISTANT_OUTPUT_DELTA, { content_delta: 'hel' }],
    [RUN_EVENT_REASONING_DELTA, { reasoning_delta: 'think' }],
    [
      RUN_EVENT_TOOL_CALL_DELTA,
      { tool_call_id: 'tool-one', name_delta: 'read' },
    ],
    [
      RUN_EVENT_PROVIDER_HEARTBEAT,
      { idle_seconds: 75, state: 'waiting_for_model_delta' },
    ],
    [
      'subagent_session_started',
      {
        tool_call: { id: 'call-subagent', index: 0, name: 'subagent' },
        data: { agent_id: 'beta', session_id: 'child-session' },
      },
    ],
    [
      'model_step_usage',
      {
        usage: { input_tokens: 12, output_tokens: 3 },
        session_usage: { measured_turns: 1, input_tokens: 12 },
        context_usage: { tokens: 15, estimated: false },
      },
    ],
  ])('delivers %s events as Run output', (type, payload) => {
    const onEvent = vi.fn();
    const connection = subscribeRunEvents(
      '/api/runs/run-one/events',
      { onEvent },
      { EventSource: MockEventSource },
    );

    connection.source.emit(type, { data: JSON.stringify({ payload }) });

    expect(onEvent).toHaveBeenCalledExactlyOnceWith({
      type,
      data: { payload },
      rawEvent: expect.any(Object),
    });
  });

  it('delivers transport heartbeats without adding Run events', () => {
    const onEvent = vi.fn();
    const onHeartbeat = vi.fn();
    const connection = subscribeRunEvents(
      '/api/runs/run-heartbeat/events',
      { onEvent, onHeartbeat },
      { EventSource: MockEventSource },
    );

    connection.source.emit(RUN_STREAM_HEARTBEAT_EVENT, { data: '{}' });

    expect(onHeartbeat).toHaveBeenCalledOnce();
    expect(onEvent).not.toHaveBeenCalled();
  });

  it('resumes after a sequence through the after_sequence query', () => {
    const connection = subscribeRunEvents(
      '/api/runs/run-one/events?mode=live',
      { onEvent: vi.fn() },
      {
        EventSource: MockEventSource,
        baseUrl: 'http://localhost:8420/',
        afterSequence: 12,
      },
    );

    expect(connection.source.url).toBe(
      'http://localhost:8420/api/runs/run-one/events?mode=live&after_sequence=12',
    );
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
  it('connects /ws with the window presence identity and parses JSON events', () => {
    const onEvent = vi.fn();
    const connection = subscribeServerEvents(
      { onEvent },
      { WebSocket: MockWebSocket, baseUrl: BASE_URL },
    );

    connection.socket.emit('message', {
      data: JSON.stringify({ type: 'run_started' }),
    });
    connection.close(1000, 'done');
    connection.close(1000, 'done');

    const url = new URL(connection.socket.url);
    expect(`${url.origin}${url.pathname}`).toBe('wss://localhost:8420/ws');
    expect(url.searchParams.get('connection_id')).toBeTruthy();
    expect(url.searchParams.get('accessor')).toBe('browser');
    expect(onEvent).toHaveBeenCalledWith(
      { type: 'run_started' },
      expect.any(Object),
    );
    expect(connection.socket.closeCalls).toEqual([
      { code: 1000, reason: 'done' },
    ]);
  });

  it.each([
    [
      'resume cursor, epoch and explicit identity',
      {
        afterSequence: 5,
        epoch: 'abc123',
        connectionId: 'tab-xyz',
        accessor: 'desktop',
      },
      {
        after_sequence: '5',
        epoch: 'abc123',
        connection_id: 'tab-xyz',
        accessor: 'desktop',
      },
    ],
    [
      'no resume position',
      { afterSequence: 0, epoch: '' },
      { after_sequence: null, epoch: null },
    ],
    ['defaults', {}, { after_sequence: null, epoch: null }],
  ])('builds the /ws query from %s', (_label, options, query) => {
    const connection = subscribeServerEvents(
      { onEvent: vi.fn() },
      { WebSocket: MockWebSocket, baseUrl: BASE_URL, ...options },
    );

    const params = new URL(connection.socket.url).searchParams;
    for (const [key, value] of Object.entries(query)) {
      expect(params.get(key)).toBe(value);
    }
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

describe('subscribeLogEvents()', () => {
  it('streams one log file from the logs websocket', () => {
    const onEvent = vi.fn();
    const onError = vi.fn();
    const connection = subscribeLogEvents(
      '2026-05-11',
      { onEvent, onError },
      { WebSocket: MockWebSocket, baseUrl: BASE_URL },
    );

    connection.socket.emit('message', {
      data: JSON.stringify({ type: 'append', file: '2026-05-11', entries: [] }),
    });
    connection.socket.emit('message', { data: '{' });

    expect(connection.socket.url).toBe(
      'wss://localhost:8420/ws/logs?file=2026-05-11',
    );
    expect(onEvent).toHaveBeenCalledWith(
      { type: 'append', file: '2026-05-11', entries: [] },
      expect.any(Object),
    );
    expect(onError).toHaveBeenCalledWith(
      expect.objectContaining({ code: WEBSOCKET_ERROR_RESPONSE }),
      expect.any(Object),
    );
    connection.close();
  });

  it('passes the log cursor to the logs websocket', () => {
    const connection = subscribeLogEvents(
      '2026-05-11',
      { onEvent: vi.fn() },
      { WebSocket: MockWebSocket, baseUrl: BASE_URL, cursor: 'cursor-123' },
    );

    expect(connection.socket.url).toBe(
      'wss://localhost:8420/ws/logs?file=2026-05-11&cursor=cursor-123',
    );
  });

  it('rejects an empty log file before opening the websocket', () => {
    expect(() =>
      subscribeLogEvents('', {}, { WebSocket: MockWebSocket }),
    ).toThrow(
      expect.objectContaining({ code: RPC_ERROR_INVALID_CLIENT_REQUEST }),
    );
  });
});

describe('subscribeTerminalEvents()', () => {
  it('streams one encoded terminal path and validates frames', () => {
    const onEvent = vi.fn();
    const onError = vi.fn();
    const connection = subscribeTerminalEvents(
      'term/one',
      { onEvent, onError },
      { WebSocket: MockWebSocket, baseUrl: BASE_URL },
    );

    connection.socket.emit('message', {
      data: JSON.stringify({
        type: 'terminal_output',
        sequence: 2,
        data: 'hello',
      }),
    });
    connection.socket.emit('message', { data: '{' });

    expect(connection.socket.url).toBe(
      'wss://localhost:8420/ws/terminals/term%2Fone',
    );
    expect(onEvent).toHaveBeenCalledWith(
      { type: 'terminal_output', sequence: 2, data: 'hello' },
      expect.any(Object),
    );
    expect(onError).toHaveBeenCalledWith(
      expect.objectContaining({ code: WEBSOCKET_ERROR_RESPONSE }),
      expect.any(Object),
    );

    // Requests go out as JSON only while the socket is open.
    connection.socket.send = vi.fn();
    expect(connection.send({ type: 'input', data: 'ls\r' })).toBe(false);
    connection.socket.readyState = 1;
    expect(connection.send({ type: 'input', data: 'ls\r' })).toBe(true);
    expect(connection.socket.send).toHaveBeenCalledWith(
      JSON.stringify({ type: 'input', data: 'ls\r' }),
    );
    connection.close();
    expect(connection.send({ type: 'input', data: 'x' })).toBe(false);
    expect(connection.socket.send).toHaveBeenCalledTimes(1);
  });
});

describe('openLiveCallSocket()', () => {
  it('opens the encoded owner socket and delivers only object frames', () => {
    const onEvent = vi.fn();
    const onError = vi.fn();
    const onClose = vi.fn();
    const connection = openLiveCallSocket(
      'rtc/one',
      { onEvent, onError, onClose },
      { WebSocket: MockWebSocket, baseUrl: BASE_URL },
    );
    expect(connection.socket.url).toBe(
      'wss://localhost:8420/ws/live/rtc%2Fone',
    );

    connection.socket.emit('message', {
      data: JSON.stringify({ type: 'state', phase: 'live' }),
    });
    connection.socket.emit('message', { data: '{' });
    connection.socket.emit('message', { data: '[1]' });
    expect(onEvent).toHaveBeenCalledExactlyOnceWith(
      { type: 'state', phase: 'live' },
      expect.any(Object),
    );
    expect(onError).toHaveBeenCalledTimes(2);
    expect(onError).toHaveBeenCalledWith(
      expect.objectContaining({ code: LIVE_SOCKET_ERROR_RESPONSE }),
      expect.any(Object),
    );

    connection.close();
    connection.close();
    expect(connection.socket.closeCalls).toHaveLength(1);
    connection.socket.emit('close', {});
    expect(onClose).not.toHaveBeenCalled();
  });

  it('receives relay audio as binary frames and sends audio only while open', () => {
    class AudioSocket extends MockWebSocket {
      static OPEN = 1;
      constructor(url) {
        super(url);
        this.readyState = 0;
        this.sent = [];
      }
      send(data) {
        this.sent.push(data);
      }
    }
    const onEvent = vi.fn();
    const onAudio = vi.fn();
    const connection = openLiveCallSocket(
      'call-1',
      { onEvent, onAudio },
      { WebSocket: AudioSocket, baseUrl: BASE_URL },
    );
    expect(connection.socket.binaryType).toBe('arraybuffer');

    const speech = new ArrayBuffer(4);
    connection.socket.emit('message', { data: speech });
    expect(onAudio).toHaveBeenCalledExactlyOnceWith(speech, expect.any(Object));
    expect(onEvent).not.toHaveBeenCalled();

    const frame = new ArrayBuffer(2);
    const report = { type: 'context', view: 'chat' };
    expect(connection.sendAudio(frame)).toBe(false);
    expect(connection.sendJson(report)).toBe(false);
    connection.socket.readyState = AudioSocket.OPEN;
    expect(connection.sendAudio(frame)).toBe(true);
    expect(connection.sendJson(report)).toBe(true);
    connection.close();
    expect(connection.sendAudio(frame)).toBe(false);
    expect(connection.sendJson(report)).toBe(false);
    expect(connection.socket.sent).toEqual([frame, JSON.stringify(report)]);
  });

  it.each([
    [1000, 'ended'],
    [1008, 'unknown_call'],
    [1013, 'lagged'],
    [4000, 'replaced'],
    [1006, 'lost'],
  ])('reports server close code %i as %s', (code, outcome) => {
    const onClose = vi.fn();
    const connection = openLiveCallSocket(
      'call-1',
      { onClose },
      { WebSocket: MockWebSocket, baseUrl: BASE_URL },
    );

    connection.socket.emit('close', { code });

    expect(onClose).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({ code }),
      outcome,
    );
  });
});
