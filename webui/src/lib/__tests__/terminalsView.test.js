import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  TERMINAL_STREAM_CONNECTED,
  TERMINAL_STREAM_CONNECTING,
  TERMINAL_STREAM_RECONNECTING,
  TERMINAL_STREAM_SNAPSHOT,
  createTerminalsController,
  createTerminalsViewState,
  layoutForCount,
  reconcileTerminalList,
  reconcileTerminalLaunchHistory,
  selectedTerminal,
} from '../terminalsView.js';

afterEach(() => {
  vi.useRealTimers();
});

describe('terminal list projection', () => {
  it('keeps a valid group and terminal selection, including retained finished terminals', () => {
    const state = createTerminalsViewState();
    state.selectedTerminalId = 'term-2';

    reconcileTerminalList(state, {
      groups: [manual()],
      terminals: [
        terminal('term-1'),
        terminal('term-2', { state: 'ready' }),
        terminal('term-3', { state: 'exited' }),
      ],
    });

    expect(state.selectedGroupId).toBe('auto:manual');
    expect(state.selectedTerminalId).toBe('term-2');
    expect(state.terminals.map((item) => item.terminal_id)).toEqual([
      'term-1',
      'term-2',
      'term-3',
    ]);
    expect(selectedTerminal(state)?.state).toBe('ready');
  });

  it('keeps the server launch history in newest-first order', () => {
    const state = createTerminalsViewState();
    const history = [
      launchHistory('recent', { command: 'codex' }),
      launchHistory('older', { command: 'python' }),
    ];

    reconcileTerminalLaunchHistory(state, { launch_history: history });

    expect(state.launchHistory).toEqual(history);
  });
});

describe('canvas layout', () => {
  it('computes the fixed grid shapes for 1 to 9 terminals', () => {
    expect(layoutForCount(1)).toEqual({
      rows: 1,
      columns: 1,
      spans: [span(0, 0)],
    });
    expect(layoutForCount(2)).toEqual({
      rows: 1,
      columns: 2,
      spans: [span(0, 0), span(0, 1)],
    });
    expect(layoutForCount(3)).toEqual({
      rows: 2,
      columns: 2,
      spans: [{ ...span(0, 0), columnSpan: 2 }, span(1, 0), span(1, 1)],
    });
    expect(layoutForCount(4)).toEqual({
      rows: 2,
      columns: 2,
      spans: [span(0, 0), span(0, 1), span(1, 0), span(1, 1)],
    });
    expect(layoutForCount(5)).toEqual({
      rows: 2,
      columns: 3,
      spans: [span(0, 0), span(0, 1), span(0, 2), span(1, 0), span(1, 1)],
    });
    expect(layoutForCount(6)).toEqual({
      rows: 2,
      columns: 3,
      spans: [
        span(0, 0),
        span(0, 1),
        span(0, 2),
        span(1, 0),
        span(1, 1),
        span(1, 2),
      ],
    });
    expect(layoutForCount(8)).toEqual({
      rows: 2,
      columns: 4,
      spans: [
        span(0, 0),
        span(0, 1),
        span(0, 2),
        span(0, 3),
        span(1, 0),
        span(1, 1),
        span(1, 2),
        span(1, 3),
      ],
    });
    expect(layoutForCount(9)).toEqual({
      rows: 3,
      columns: 4,
      spans: [
        span(0, 0),
        span(0, 1),
        span(0, 2),
        span(0, 3),
        span(1, 0),
        span(1, 1),
        span(1, 2),
        span(1, 3),
        span(2, 0),
      ],
    });
  });

  it('returns an empty layout for zero or invalid counts', () => {
    expect(layoutForCount(0)).toEqual({ rows: 0, columns: 0, spans: [] });
    expect(layoutForCount(-2)).toEqual({ rows: 0, columns: 0, spans: [] });
    expect(layoutForCount('three')).toEqual({ rows: 0, columns: 0, spans: [] });
  });
});

describe('terminal live controller', () => {
  it('opens one stream per listed terminal and applies per-terminal events', async () => {
    vi.useFakeTimers();
    const state = createTerminalsViewState();
    const snapshots = [];
    const output = [];
    const streams = [];
    const api = fakeApi({
      streams,
      terminals: [terminal('term-1'), terminal('term-2')],
    });
    const controller = createTerminalsController({
      state,
      api,
      onSnapshot: (terminalId, ansi) => snapshots.push([terminalId, ansi]),
      onOutput: (terminalId, data) => output.push([terminalId, data]),
    });

    await controller.start();
    expect(state.selectedTerminalId).toBe('term-1');
    expect(streams).toHaveLength(2);
    expect(Object.keys(state.streams)).toEqual(['term-1', 'term-2']);

    streams[0].emit({
      type: 'terminal_ready',
      sequence: 4,
      terminal: terminal('term-1'),
      ansi: '\u001b[2Jready',
    });
    streams[1].emit({
      type: 'terminal_ready',
      sequence: 10,
      terminal: terminal('term-2'),
      ansi: '\u001b[2Jsecond',
    });
    streams[1].emit({ type: 'terminal_output', sequence: 11, data: 'next' });
    streams[0].emit({
      type: 'terminal_snapshot',
      sequence: 5,
      terminal: terminal('term-1', { title: 'Codex tests' }),
      ansi: '\u001b[2Jshell restored',
    });

    expect(state.streams['term-1'].status).toBe(TERMINAL_STREAM_CONNECTED);
    expect(state.streams['term-2'].status).toBe(TERMINAL_STREAM_CONNECTED);
    expect(snapshots).toEqual([
      ['term-1', '\u001b[2Jready'],
      ['term-2', '\u001b[2Jsecond'],
      ['term-1', '\u001b[2Jshell restored'],
    ]);
    expect(output).toEqual([['term-2', 'next']]);
    expect(selectedTerminal(state)?.title).toBe('Codex tests');
    controller.destroy();
  });

  it('tracks sequences per terminal and reconnects only the gapped terminal', async () => {
    vi.useFakeTimers();
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({
      streams,
      terminals: [terminal('term-1'), terminal('term-2')],
    });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    streams[0].emit({
      type: 'terminal_ready',
      sequence: 4,
      terminal: terminal('term-1'),
      ansi: '\u001b[2Jone',
    });
    streams[1].emit({
      type: 'terminal_ready',
      sequence: 10,
      terminal: terminal('term-2'),
      ansi: '\u001b[2Jtwo',
    });
    streams[1].emit({ type: 'terminal_output', sequence: 11, data: 'ok' });

    streams[0].emit({ type: 'terminal_output', sequence: 9, data: 'gap' });
    expect(state.streams['term-1'].status).toBe(TERMINAL_STREAM_RECONNECTING);
    expect(state.streams['term-1'].errorCode).toBe('gap');
    expect(streams[0].connection.close).toHaveBeenCalled();
    expect(state.streams['term-2'].status).toBe(TERMINAL_STREAM_CONNECTED);

    await vi.runAllTimersAsync();
    expect(streams).toHaveLength(3);
    expect(state.streams['term-2'].status).toBe(TERMINAL_STREAM_CONNECTED);
    expect(state.streams['term-2'].errorCode).toBe('');
    controller.destroy();
  });

  it('does not reconnect a finished terminal and keeps its snapshot status', async () => {
    vi.useFakeTimers();
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({ streams });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    api.listTerminals.mockResolvedValueOnce({
      groups: [manual({ terminal_count: 1 })],
      terminals: [terminal('term-1', { state: 'exited' })],
    });
    streams[0].emit({
      type: 'terminal_ready',
      sequence: 5,
      terminal: terminal('term-1', { state: 'exited' }),
      ansi: '\u001b[2Jdone',
    });
    streams[0].close();
    await vi.runAllTimersAsync();

    expect(streams).toHaveLength(1);
    expect(state.streams['term-1'].status).toBe(TERMINAL_STREAM_SNAPSHOT);
    expect(state.terminals[0]).toMatchObject({
      terminal_id: 'term-1',
      state: 'exited',
    });
    expect(state.selectedTerminalId).toBe('term-1');
    controller.destroy();
  });

  it('reconnects a closed stream without disturbing the other terminal', async () => {
    vi.useFakeTimers();
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({
      streams,
      terminals: [terminal('term-1'), terminal('term-2')],
    });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    expect(state.streams['term-1'].status).toBe(TERMINAL_STREAM_CONNECTING);
    expect(state.streams['term-2'].status).toBe(TERMINAL_STREAM_CONNECTING);
    streams[1].close();
    await vi.runAllTimersAsync();

    expect(streams).toHaveLength(3);
    expect(state.streams['term-1'].status).toBe(TERMINAL_STREAM_CONNECTING);
    expect(state.streams['term-2'].status).toBe(TERMINAL_STREAM_RECONNECTING);
    expect(state.streams['term-1'].errorCode).toBe('');
    expect(state.streams['term-2'].errorCode).toBe('');
    controller.destroy();
  });

  it('routes input to its exact terminal and keeps the previous buffer on focus switch', async () => {
    vi.useFakeTimers();
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({
      streams,
      terminals: [terminal('term-1'), terminal('term-2')],
    });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    controller.queueInput('hel');
    controller.selectTerminal('term-2');
    controller.queueInput('hi', { terminalId: 'term-2' });
    await vi.runAllTimersAsync();

    expect(api.sendTerminalInput).toHaveBeenCalledWith('term-1', 'hel');
    expect(api.sendTerminalInput).toHaveBeenCalledWith('term-2', 'hi');
    expect(api.sendTerminalInput).toHaveBeenCalledTimes(2);
    controller.destroy();
  });

  it('debounces resize requests and skips unchanged dimensions', async () => {
    vi.useFakeTimers();
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({ streams });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    controller.resize(100, 30, 'term-1');
    controller.resize(100, 30, 'term-1');
    controller.resize(120, 32, 'term-1');
    await vi.runAllTimersAsync();

    expect(api.resizeTerminal).toHaveBeenCalledTimes(1);
    expect(api.resizeTerminal).toHaveBeenCalledWith('term-1', 120, 32);
    controller.destroy();
  });

  it('sends an immediate resize without waiting for the debounce', async () => {
    vi.useFakeTimers();
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({ streams });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    controller.resize(200, 50, 'term-1', true);
    await vi.runAllTimersAsync();
    expect(api.resizeTerminal).toHaveBeenCalledWith('term-1', 200, 50);
    controller.destroy();
  });

  it('does not resize a finished terminal', async () => {
    vi.useFakeTimers();
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({
      streams,
      terminals: [terminal('term-1', { state: 'exited' })],
    });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    controller.resize(100, 30, 'term-1', true);
    expect(api.resizeTerminal).not.toHaveBeenCalled();
    controller.destroy();
  });

  it('batches input and removes an explicitly killed terminal', async () => {
    vi.useFakeTimers();
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({
      streams,
      terminals: [terminal('term-1')],
    });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    controller.queueInput('hello');
    controller.queueInput('\r', { immediate: true });
    await vi.runAllTimersAsync();

    expect(api.sendTerminalInput).toHaveBeenCalledWith('term-1', 'hello\r');

    api.listTerminals.mockResolvedValueOnce({
      groups: [],
      terminals: [],
    });
    await expect(controller.killSelected()).resolves.toBe(true);
    expect(api.killTerminal).toHaveBeenCalledWith('term-1');
    expect(api.forgetTerminal).not.toHaveBeenCalled();
    expect(state.terminals).toEqual([]);
    expect(state.selectedTerminalId).toBe('');
    expect(Object.keys(state.streams)).toEqual([]);
    controller.destroy();
  });

  it('closes the stream of a terminal removed by an invalidation reload', async () => {
    vi.useFakeTimers();
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({
      streams,
      terminals: [terminal('term-1'), terminal('term-2')],
    });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    expect(streams).toHaveLength(2);

    api.listTerminals.mockResolvedValueOnce({
      groups: [manual({ terminal_count: 1 })],
      terminals: [terminal('term-2')],
    });
    await controller.loadTerminals();

    expect(streams[0].connection.close).toHaveBeenCalledWith(
      1000,
      'terminals-view-close',
    );
    expect(Object.keys(state.streams)).toEqual(['term-2']);
    expect(state.selectedTerminalId).toBe('term-2');
    controller.destroy();
  });

  it('starts, selects, and connects one manual terminal alongside existing streams', async () => {
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({ streams });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    api.startTerminal.mockResolvedValueOnce({
      terminal: terminal('manual-1', {
        command: 'codex',
        owner: null,
        group_id: 'auto:manual',
      }),
      launch_history: [launchHistory('codex', { command: 'codex' })],
    });

    const started = await controller.startManualTerminal({ command: 'codex' });

    expect(api.startTerminal).toHaveBeenCalledWith({ command: 'codex' });
    expect(started).toMatchObject({ terminal_id: 'manual-1', owner: null });
    expect(state.selectedTerminalId).toBe('manual-1');
    expect(state.startError).toBe('');
    expect(state.launchHistory).toEqual([
      launchHistory('codex', { command: 'codex' }),
    ]);
    expect(streams).toHaveLength(2);
    expect(state.streams['manual-1'].status).toBe(TERMINAL_STREAM_CONNECTING);
    controller.destroy();
  });

  it('forgets a finished terminal and selects the next one', async () => {
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({ streams });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    expect(state.selectedTerminalId).toBe('term-1');

    api.listTerminals.mockResolvedValueOnce({
      groups: [manual({ terminal_count: 2 })],
      terminals: [
        terminal('term-1', { state: 'exited' }),
        terminal('term-2', { state: 'ready' }),
      ],
    });
    await controller.loadTerminals();
    state.selectedTerminalId = 'term-1';
    controller.selectTerminal('term-1');

    const forgotten = await controller.forgetSelected();

    expect(forgotten).toBe(true);
    expect(api.forgetTerminal).toHaveBeenCalledWith('term-1');
    expect(state.terminals.map((t) => t.terminal_id)).toEqual(['term-2']);
    expect(state.selectedTerminalId).toBe('term-2');
    expect(Object.keys(state.streams)).toEqual(['term-2']);
    controller.destroy();
  });

  it('closes every stream while the server is unavailable and reloads on recovery', async () => {
    vi.useFakeTimers();
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({
      streams,
      terminals: [terminal('term-1'), terminal('term-2')],
    });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    controller.setServerUnavailable(true);
    expect(streams[0].connection.close).toHaveBeenCalled();
    expect(streams[1].connection.close).toHaveBeenCalled();
    expect(Object.keys(state.streams)).toEqual([]);
    expect(state.streams).toEqual({});

    controller.setServerUnavailable(false);
    await vi.runAllTimersAsync();
    expect(streams).toHaveLength(4);
    expect(Object.keys(state.streams)).toEqual(['term-1', 'term-2']);
    controller.destroy();
  });

  it('does not queue input for a finished focused terminal', async () => {
    vi.useFakeTimers();
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({
      streams,
      terminals: [terminal('term-1', { state: 'exited' })],
    });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    controller.queueInput('x');
    await vi.runAllTimersAsync();

    expect(api.sendTerminalInput).not.toHaveBeenCalled();
    controller.destroy();
  });

  it('discards buffered input when the terminal ends', async () => {
    vi.useFakeTimers();
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({ streams });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    streams[0].emit({
      type: 'terminal_ready',
      sequence: 1,
      terminal: terminal('term-1'),
      ansi: '\\u001b[2Jshell',
    });
    controller.queueInput('hello');
    streams[0].emit({
      type: 'terminal_state',
      sequence: 2,
      terminal: terminal('term-1', { state: 'exited' }),
    });
    await vi.runAllTimersAsync();

    expect(api.sendTerminalInput).not.toHaveBeenCalled();
    expect(state.streams['term-1'].status).toBe(TERMINAL_STREAM_SNAPSHOT);
    expect(state.actionError).toBe('');
    controller.destroy();
  });

  it('does not surface a terminal-closed RPC error after the terminal ended', async () => {
    vi.useFakeTimers();
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({ streams });
    let rejectInput;
    api.sendTerminalInput.mockImplementation(
      () =>
        new Promise((_resolve, reject) => {
          rejectInput = reject;
        }),
    );
    const controller = createTerminalsController({ state, api });

    await controller.start();
    streams[0].emit({
      type: 'terminal_ready',
      sequence: 1,
      terminal: terminal('term-1'),
      ansi: '\\u001b[2Jshell',
    });
    controller.queueInput('hi', { immediate: true });
    await vi.runAllTimersAsync();
    streams[0].emit({
      type: 'terminal_state',
      sequence: 2,
      terminal: terminal('term-1', { state: 'exited' }),
    });
    rejectInput(new Error('Terminal Session is no longer running'));
    await vi.runAllTimersAsync();

    expect(api.sendTerminalInput).toHaveBeenCalledWith('term-1', 'hi');
    expect(state.actionError).toBe('');
    controller.destroy();
  });
});

function fakeApi({ streams, terminals = [terminal('term-1')], groups }) {
  const groupList = groups ?? [
    {
      ...manual(),
      terminal_count: terminals.length,
      live_count: terminals.length,
    },
  ];
  return {
    listTerminals: vi.fn().mockResolvedValue({
      groups: groupList.map((item) => ({ ...item })),
      terminals: terminals.map((item) => ({ ...item })),
    }),
    sendTerminalInput: vi.fn().mockResolvedValue({}),
    resizeTerminal: vi.fn().mockResolvedValue({}),
    startTerminal: vi.fn().mockResolvedValue({}),
    killTerminal: vi.fn().mockResolvedValue({}),
    forgetTerminal: vi.fn().mockResolvedValue({}),
    createTerminalGroup: vi.fn().mockResolvedValue({}),
    renameTerminalGroup: vi.fn().mockResolvedValue({}),
    deleteTerminalGroup: vi.fn().mockResolvedValue({}),
    setTerminalGroupOrder: vi.fn().mockResolvedValue({}),
    subscribeTerminalEvents: vi.fn((_terminalId, handlers) => {
      const connection = { close: vi.fn() };
      streams.push({
        connection,
        emit: (event) => handlers.onEvent(event),
        close: () => handlers.onClose(),
      });
      return connection;
    }),
  };
}

function manual(overrides = {}) {
  return {
    group_id: 'auto:manual',
    name: 'Manual',
    kind: 'automatic',
    terminal_count: 0,
    live_count: 0,
    order: [],
    ...overrides,
  };
}

function terminal(terminalId, changes = {}) {
  return {
    terminal_id: terminalId,
    group_id: 'auto:manual',
    state: 'working',
    command: 'python',
    title: '',
    workdir: 'C:\\repo',
    pid: 123,
    started_at: '2026-08-03T12:00:00+00:00',
    columns: 120,
    rows: 32,
    owner: {
      project_id: null,
      agent_id: 'main',
      session_id: 'session-1',
    },
    attention: null,
    ...changes,
  };
}

function launchHistory(id, changes = {}) {
  return {
    id,
    command: null,
    args: [],
    workdir: null,
    used_at: '2026-08-08T10:00:00+00:00',
    ...changes,
  };
}

function span(row, column) {
  return { row, column, rowSpan: 1, columnSpan: 1 };
}

describe('terminal group selection and reorder', () => {
  it('switches groups, reconnects only the visible terminals, and keeps selection valid', async () => {
    vi.useFakeTimers();
    const state = createTerminalsViewState();
    const streams = [];
    const groups = [
      {
        ...manual(),
        group_id: 'g1',
        name: 'Work',
        kind: 'user',
        terminal_count: 1,
      },
      {
        ...manual(),
        group_id: 'g2',
        name: 'Play',
        kind: 'user',
        terminal_count: 1,
      },
    ];
    const api = fakeApi({
      streams,
      groups,
      terminals: [
        terminal('term-1', { group_id: 'g1' }),
        terminal('term-2', { group_id: 'g2' }),
      ],
    });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    expect(state.selectedGroupId).toBe('g1');
    expect(Object.keys(state.streams)).toEqual(['term-1']);

    controller.selectGroup('g2');
    expect(state.selectedGroupId).toBe('g2');
    expect(state.selectedTerminalId).toBe('term-2');
    expect(Object.keys(state.streams)).toEqual(['term-2']);
    expect(streams[0].connection.close).toHaveBeenCalledWith(
      1000,
      'terminals-view-close',
    );

    controller.selectGroup('g1');
    expect(Object.keys(state.streams)).toEqual(['term-1']);
    controller.destroy();
  });

  it('reorders a group optimistically and persists the order', async () => {
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({
      streams,
      groups: [manual({ terminal_count: 2 })],
      terminals: [terminal('term-1'), terminal('term-2')],
    });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    controller.reorderGroup('auto:manual', ['term-2', 'term-1']);

    expect(api.setTerminalGroupOrder).toHaveBeenCalledWith('auto:manual', [
      'term-2',
      'term-1',
    ]);
    expect(state.terminals.map((item) => item.terminal_id)).toEqual([
      'term-2',
      'term-1',
    ]);
    controller.destroy();
  });

  it('creates a user group and selects it', async () => {
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({ streams });
    api.createTerminalGroup.mockResolvedValueOnce({
      group: { group_id: 'new-group', name: 'Docs', kind: 'user' },
    });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    const group = await controller.createGroup('Docs');

    expect(group?.group_id).toBe('new-group');
    expect(state.selectedGroupId).toBe('new-group');
    controller.destroy();
  });
});
