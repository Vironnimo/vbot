import { describe, expect, it, vi } from 'vitest';
import {
  RPC_ERROR_INVALID_CLIENT_REQUEST,
  SSE_ERROR_RESPONSE,
  RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
  RUN_EVENT_REASONING_DELTA,
  RUN_EVENT_PROVIDER_HEARTBEAT,
  RUN_EVENT_STREAM_ATTEMPT_RESTARTED,
  RUN_EVENT_TOOL_CALL_DELTA,
  RUN_EVENT_TOOL_CALL_STDERR,
  RUN_EVENT_TOOL_CALL_STDOUT,
  RUN_STREAM_HEARTBEAT_EVENT,
  RUN_EVENT_TYPES,
  WEBSOCKET_ERROR_RESPONSE,
  listTerminals,
  startTerminal,
  subscribeLogEvents,
  subscribeRunEvents,
  subscribeServerEvents,
  subscribeTerminalEvents,
  sendTerminalInput,
  resizeTerminal,
  killTerminal,
} from '../api.js';
import { jsonResponse, MockEventSource, MockWebSocket } from './api.support.js';

describe('subscribeRunEvents()', () => {
  it('includes streaming delta events in the default SSE subscription list', () => {
    expect(RUN_EVENT_TYPES).toContain(RUN_EVENT_ASSISTANT_OUTPUT_DELTA);
    expect(RUN_EVENT_TYPES).toContain(RUN_EVENT_REASONING_DELTA);
    expect(RUN_EVENT_TYPES).toContain(RUN_EVENT_STREAM_ATTEMPT_RESTARTED);
    expect(RUN_EVENT_TYPES).toContain(RUN_EVENT_TOOL_CALL_DELTA);
    expect(RUN_EVENT_TYPES).toContain(RUN_EVENT_TOOL_CALL_STDOUT);
    expect(RUN_EVENT_TYPES).toContain(RUN_EVENT_TOOL_CALL_STDERR);
    expect(RUN_EVENT_TYPES).toContain('model_fallback_activated');
    expect(RUN_EVENT_TYPES).toContain('error_message_persisted');
    expect(RUN_EVENT_TYPES).toContain('compaction_started');
    expect(RUN_EVENT_TYPES).toContain('compaction_aborted');
    expect(RUN_EVENT_TYPES).toContain('compaction_completed');
    expect(RUN_EVENT_TYPES).toContain('subagent_session_started');
    expect(RUN_EVENT_TYPES).toContain('subagent_status_changed');
    expect(RUN_EVENT_TYPES).toContain('model_step_usage');
  });

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

  it('delivers transport heartbeats without adding timeline events', () => {
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

  it('subscribes to delta SSE run events', () => {
    const onEvent = vi.fn();
    const connection = subscribeRunEvents(
      '/api/runs/run-one/events',
      { onEvent },
      { EventSource: MockEventSource },
    );

    connection.source.emit(RUN_EVENT_ASSISTANT_OUTPUT_DELTA, {
      data: JSON.stringify({ payload: { content_delta: 'hel' } }),
    });
    connection.source.emit(RUN_EVENT_REASONING_DELTA, {
      data: JSON.stringify({ payload: { reasoning_delta: 'think' } }),
    });
    connection.source.emit(RUN_EVENT_TOOL_CALL_DELTA, {
      data: JSON.stringify({
        payload: { tool_call_id: 'tool-one', name_delta: 'read' },
      }),
    });

    expect(onEvent).toHaveBeenCalledWith({
      type: RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
      data: { payload: { content_delta: 'hel' } },
      rawEvent: expect.any(Object),
    });
    expect(onEvent).toHaveBeenCalledWith({
      type: RUN_EVENT_REASONING_DELTA,
      data: { payload: { reasoning_delta: 'think' } },
      rawEvent: expect.any(Object),
    });
    expect(onEvent).toHaveBeenCalledWith({
      type: RUN_EVENT_TOOL_CALL_DELTA,
      data: {
        payload: { tool_call_id: 'tool-one', name_delta: 'read' },
      },
      rawEvent: expect.any(Object),
    });
  });

  it('subscribes to Provider heartbeat Run events as ordinary Run output', () => {
    const onEvent = vi.fn();
    const connection = subscribeRunEvents(
      '/api/runs/run-provider-heartbeat/events',
      { onEvent },
      { EventSource: MockEventSource },
    );

    connection.source.emit(RUN_EVENT_PROVIDER_HEARTBEAT, {
      data: JSON.stringify({
        payload: {
          idle_seconds: 75,
          state: 'waiting_for_model_delta',
        },
      }),
    });

    expect(onEvent).toHaveBeenCalledWith({
      type: RUN_EVENT_PROVIDER_HEARTBEAT,
      data: {
        payload: {
          idle_seconds: 75,
          state: 'waiting_for_model_delta',
        },
      },
      rawEvent: expect.any(Object),
    });
  });

  it('subscribes to sub-agent session started SSE run events', () => {
    const onEvent = vi.fn();
    const connection = subscribeRunEvents(
      '/api/runs/run-subagent/events',
      { onEvent },
      { EventSource: MockEventSource },
    );

    connection.source.emit('subagent_session_started', {
      data: JSON.stringify({
        payload: {
          tool_call: { id: 'call-subagent', index: 0, name: 'subagent' },
          data: {
            agent_id: 'beta',
            session_id: 'child-session',
            status: 'running',
          },
        },
      }),
    });

    expect(onEvent).toHaveBeenCalledWith({
      type: 'subagent_session_started',
      data: {
        payload: {
          tool_call: { id: 'call-subagent', index: 0, name: 'subagent' },
          data: {
            agent_id: 'beta',
            session_id: 'child-session',
            status: 'running',
          },
        },
      },
      rawEvent: expect.any(Object),
    });
  });

  it('delivers model-step usage events from the named SSE stream', () => {
    const onEvent = vi.fn();
    const connection = subscribeRunEvents(
      '/api/runs/run-usage/events',
      { onEvent },
      { EventSource: MockEventSource },
    );

    connection.source.emit('model_step_usage', {
      data: JSON.stringify({
        payload: {
          usage: { input_tokens: 12, output_tokens: 3 },
          session_usage: { measured_turns: 1, input_tokens: 12 },
          context_usage: { tokens: 15, estimated: false },
        },
      }),
    });

    expect(onEvent).toHaveBeenCalledWith({
      type: 'model_step_usage',
      data: {
        payload: {
          usage: { input_tokens: 12, output_tokens: 3 },
          session_usage: { measured_turns: 1, input_tokens: 12 },
          context_usage: { tokens: 15, estimated: false },
        },
      },
      rawEvent: expect.any(Object),
    });
  });

  it('adds optional after_sequence query param to SSE subscriptions', () => {
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

    // The /ws connect carries the per-window presence identity by default
    // (a minted connection_id + accessor type).
    expect(connection.socket.url).toContain('wss://localhost:8420/ws');
    expect(connection.socket.url).toContain('connection_id=');
    expect(connection.socket.url).toContain('accessor=browser');
    expect(onEvent).toHaveBeenCalledWith(
      { type: 'run_started' },
      expect.any(Object),
    );
    expect(connection.socket.closeCalls).toEqual([
      { code: 1000, reason: 'done' },
    ]);
  });

  it('sends explicit connection_id and accessor query params when provided', () => {
    const connection = subscribeServerEvents(
      { onEvent: vi.fn() },
      {
        WebSocket: MockWebSocket,
        baseUrl: 'https://localhost:8420/',
        connectionId: 'tab-xyz',
        accessor: 'desktop',
      },
    );

    expect(connection.socket.url).toContain('connection_id=tab-xyz');
    expect(connection.socket.url).toContain('accessor=desktop');

    connection.close();
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

  it('includes after_sequence query param when afterSequence is greater than 0', () => {
    const connection = subscribeServerEvents(
      { onEvent: vi.fn() },
      {
        WebSocket: MockWebSocket,
        baseUrl: 'https://localhost:8420/',
        afterSequence: 5,
      },
    );

    expect(connection.socket.url).toContain('after_sequence=5');

    connection.close();
  });

  it('omits after_sequence query param when afterSequence is 0', () => {
    const connection = subscribeServerEvents(
      { onEvent: vi.fn() },
      {
        WebSocket: MockWebSocket,
        baseUrl: 'https://localhost:8420/',
        afterSequence: 0,
      },
    );

    expect(connection.socket.url).not.toContain('after_sequence');

    connection.close();
  });

  it('omits after_sequence query param when afterSequence is omitted', () => {
    const connection = subscribeServerEvents(
      { onEvent: vi.fn() },
      { WebSocket: MockWebSocket, baseUrl: 'https://localhost:8420/' },
    );

    expect(connection.socket.url).not.toContain('after_sequence');

    connection.close();
  });

  it('includes epoch query param when epoch is non-empty', () => {
    const connection = subscribeServerEvents(
      { onEvent: vi.fn() },
      {
        WebSocket: MockWebSocket,
        baseUrl: 'https://localhost:8420/',
        epoch: 'abc123',
      },
    );

    expect(connection.socket.url).toContain('epoch=abc123');

    connection.close();
  });

  it('combines epoch and after_sequence when both are non-empty', () => {
    const connection = subscribeServerEvents(
      { onEvent: vi.fn() },
      {
        WebSocket: MockWebSocket,
        baseUrl: 'https://localhost:8420/',
        afterSequence: 5,
        epoch: 'abc123',
      },
    );

    expect(connection.socket.url).toContain('after_sequence=5');
    expect(connection.socket.url).toContain('epoch=abc123');

    connection.close();
  });

  it('omits epoch query param when epoch is the empty string', () => {
    const connection = subscribeServerEvents(
      { onEvent: vi.fn() },
      {
        WebSocket: MockWebSocket,
        baseUrl: 'https://localhost:8420/',
        epoch: '',
      },
    );

    expect(connection.socket.url).not.toContain('epoch=');

    connection.close();
  });

  it('omits epoch query param when epoch is omitted', () => {
    const connection = subscribeServerEvents(
      { onEvent: vi.fn() },
      { WebSocket: MockWebSocket, baseUrl: 'https://localhost:8420/' },
    );

    expect(connection.socket.url).not.toContain('epoch=');

    connection.close();
  });
});

describe('subscribeLogEvents()', () => {
  it('subscribes to the dedicated logs websocket with file query param', () => {
    const onEvent = vi.fn();
    const connection = subscribeLogEvents(
      '2026-05-11',
      { onEvent },
      { WebSocket: MockWebSocket, baseUrl: 'https://localhost:8420/' },
    );

    connection.socket.emit('message', {
      data: JSON.stringify({ type: 'append', file: '2026-05-11', entries: [] }),
    });

    expect(connection.socket.url).toBe(
      'wss://localhost:8420/ws/logs?file=2026-05-11',
    );
    expect(onEvent).toHaveBeenCalledWith(
      { type: 'append', file: '2026-05-11', entries: [] },
      expect.any(Object),
    );

    connection.close();
  });

  it('passes the explicit log cursor through to the logs websocket', () => {
    const connection = subscribeLogEvents(
      '2026-05-11',
      { onEvent: vi.fn() },
      {
        WebSocket: MockWebSocket,
        baseUrl: 'https://localhost:8420/',
        cursor: 'cursor-123',
      },
    );

    expect(connection.socket.url).toBe(
      'wss://localhost:8420/ws/logs?file=2026-05-11&cursor=cursor-123',
    );

    connection.close();
  });

  it('reports malformed log websocket messages through the error handler', () => {
    const onError = vi.fn();
    const connection = subscribeLogEvents(
      '2026-05-11',
      { onError },
      { WebSocket: MockWebSocket },
    );

    connection.socket.emit('message', { data: '{' });

    expect(onError).toHaveBeenCalledWith(
      expect.objectContaining({ code: WEBSOCKET_ERROR_RESPONSE }),
      expect.any(Object),
    );
  });

  it('rejects invalid log subscriptions before opening websocket', () => {
    expect(() =>
      subscribeLogEvents('', {}, { WebSocket: MockWebSocket }),
    ).toThrow(
      expect.objectContaining({ code: RPC_ERROR_INVALID_CLIENT_REQUEST }),
    );
  });
});

describe('terminal operator API', () => {
  it('wraps list, start, input, resize, and kill RPCs', async () => {
    const fetchFunction = vi
      .fn()
      .mockResolvedValue(jsonResponse({ ok: true, result: { ok: true } }));

    await listTerminals({ fetch: fetchFunction });
    await startTerminal(
      { command: 'codex', args: ['--profile', 'work'] },
      { fetch: fetchFunction },
    );
    await sendTerminalInput('term/one', 'hello\r', { fetch: fetchFunction });
    await resizeTerminal('term/one', 100, 30, { fetch: fetchFunction });
    await killTerminal('term/one', { fetch: fetchFunction });

    expect(
      fetchFunction.mock.calls.map((call) => JSON.parse(call[1].body)),
    ).toEqual([
      { method: 'terminal.list', params: {} },
      {
        method: 'terminal.start',
        params: { command: 'codex', args: ['--profile', 'work'] },
      },
      {
        method: 'terminal.input',
        params: { terminal_id: 'term/one', data: 'hello\r' },
      },
      {
        method: 'terminal.resize',
        params: { terminal_id: 'term/one', columns: 100, rows: 30 },
      },
      { method: 'terminal.kill', params: { terminal_id: 'term/one' } },
    ]);
  });

  it('streams one encoded terminal path and validates frames', () => {
    const onEvent = vi.fn();
    const onError = vi.fn();
    const connection = subscribeTerminalEvents(
      'term/one',
      { onEvent, onError },
      { WebSocket: MockWebSocket, baseUrl: 'https://localhost:8420/' },
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
    connection.close();
  });
});
