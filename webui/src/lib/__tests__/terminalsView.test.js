import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  TERMINAL_STREAM_CONNECTED,
  TERMINAL_STREAM_CONNECTING,
  TERMINAL_STREAM_RECONNECTING,
  TERMINAL_STREAM_SNAPSHOT,
  clampTerminalGrid,
  createPtyFrameSanitizer,
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

describe('pty frame sanitizer', () => {
  it('reassembles an ANSI escape split across frames', () => {
    const sanitizer = createPtyFrameSanitizer();

    expect(sanitizer.next('build ok \u001b[')).toBe('build ok ');
    expect(sanitizer.next('31mred\u001b[0m')).toBe('\u001b[31mred\u001b[0m');
    expect(sanitizer.flush()).toBe('');
  });

  it('drops a buffered partial escape at end of stream', () => {
    const sanitizer = createPtyFrameSanitizer();

    expect(sanitizer.next('text \u001b[2')).toBe('text ');
    expect(sanitizer.flush()).toBe('');
  });

  it('passes a blank-line burst through byte-exact so viewer and server stay in sync', () => {
    const sanitizer = createPtyFrameSanitizer();
    const burst = '\r\n'.repeat(80);

    expect(sanitizer.next('head' + burst.slice(0, 60))).toBe(
      'head' + burst.slice(0, 60),
    );
    expect(sanitizer.next(burst.slice(60) + 'tail')).toBe(
      burst.slice(60) + 'tail',
    );
  });

  it('passes a blank-line burst split across frames through unchanged', () => {
    const sanitizer = createPtyFrameSanitizer();

    expect(sanitizer.next('a\r\n\r\n\r')).toBe('a\r\n\r\n\r');
    expect(sanitizer.next('\nb')).toBe('\nb');
    expect(sanitizer.flush()).toBe('');
  });
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

describe('terminal grid bounds', () => {
  it('clamps fitted grids into the server-accepted dimension window', () => {
    expect(clampTerminalGrid(10, 4)).toEqual({ columns: 40, rows: 10 });
    expect(clampTerminalGrid(500, 200)).toEqual({ columns: 240, rows: 80 });
    expect(clampTerminalGrid(100.9, 31.2)).toEqual({
      columns: 100,
      rows: 31,
    });
    expect(clampTerminalGrid(120, 32)).toEqual({ columns: 120, rows: 32 });
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

  it('debounces resize requests and drops bursts ending at the current dimensions', async () => {
    vi.useFakeTimers();
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({ streams });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    // The terminal already runs at 120x32: a burst that ends there must not
    // reach the server, not even with an intermediate size in between.
    controller.resize(100, 30, 'term-1');
    controller.resize(100, 30, 'term-1');
    controller.resize(120, 32, 'term-1');
    await vi.runAllTimersAsync();

    expect(api.resizeTerminal).not.toHaveBeenCalled();
    controller.destroy();
  });

  it('debounces a resize burst into one request with the final dimensions', async () => {
    vi.useFakeTimers();
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({ streams });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    // A burst collapses into one debounced request with the final size;
    // intermediate sizes never reach the server.
    controller.resize(100, 30, 'term-1');
    controller.resize(110, 31, 'term-1');
    controller.resize(110, 31, 'term-1');
    await vi.runAllTimersAsync();

    expect(api.resizeTerminal).toHaveBeenCalledTimes(1);
    expect(api.resizeTerminal).toHaveBeenCalledWith('term-1', 110, 31);
    controller.destroy();
  });

  it('sends a settled size once after the debounce without repeated fits', async () => {
    vi.useFakeTimers();
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({ streams });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    // A single settled measurement reaches the PTY after the debounce; the
    // remount-transient protection is the debounce itself plus the
    // geometry-change follow-up fit, not a repeated-measurement counter.
    controller.resize(100, 30, 'term-1');
    await vi.runAllTimersAsync();
    expect(api.resizeTerminal).toHaveBeenCalledTimes(1);
    expect(api.resizeTerminal).toHaveBeenCalledWith('term-1', 100, 30);

    // Repeating the same measurement stays silent.
    controller.resize(100, 30, 'term-1');
    await vi.runAllTimersAsync();
    expect(api.resizeTerminal).toHaveBeenCalledTimes(1);
    controller.destroy();
  });

  it('skips a resize that repeats the terminal’s authoritative dimensions', async () => {
    vi.useFakeTimers();
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({ streams });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    // A tab revisit builds a fresh stream; repeating the known dimensions
    // must not reach the server or make the program repaint.
    controller.resize(120, 32, 'term-1', true);
    await vi.runAllTimersAsync();

    expect(api.resizeTerminal).not.toHaveBeenCalled();
    controller.destroy();
  });

  it('sends an immediate resize without waiting for the debounce', async () => {
    vi.useFakeTimers();
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({ streams });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    // Maximize is a deterministic user action: its measurement goes out
    // immediately, without the debounce.
    controller.resize(200, 50, 'term-1', true);
    await vi.runAllTimersAsync();
    expect(api.resizeTerminal).toHaveBeenCalledWith('term-1', 200, 50);
    controller.destroy();
  });

  it('clamps a fitted grid below the server minimum into the accepted bounds', async () => {
    vi.useFakeTimers();
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({ streams });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    // A tiny tile fits below the server's 40x10 minimum; the request must
    // carry the clamped legal grid, not one the server would reject.
    controller.resize(22, 6, 'term-1', true);
    await vi.runAllTimersAsync();

    expect(api.resizeTerminal).toHaveBeenCalledWith('term-1', 40, 10);
    expect(state.terminals[0]).toMatchObject({ columns: 40, rows: 10 });
    controller.destroy();
  });

  it('adopts the server-confirmed dimensions after a successful resize', async () => {
    vi.useFakeTimers();
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({ streams });
    api.resizeTerminal.mockResolvedValue({ columns: 110, rows: 31 });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    controller.resize(100, 30, 'term-1');
    controller.resize(100, 30, 'term-1');
    await vi.runAllTimersAsync();

    expect(api.resizeTerminal).toHaveBeenCalledTimes(1);
    expect(state.terminals[0]).toMatchObject({ columns: 110, rows: 31 });
    expect(state.streams['term-1'].gridPending).toBe(false);
    // The next fit at the confirmed size is now a no-op.
    controller.resize(110, 31, 'term-1');
    await vi.runAllTimersAsync();
    expect(api.resizeTerminal).toHaveBeenCalledTimes(1);
    controller.destroy();
  });

  it('keeps the grid divergence visible while a resize correction failed', async () => {
    vi.useFakeTimers();
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({ streams });
    api.resizeTerminal.mockRejectedValue(new Error('resize rejected'));
    const controller = createTerminalsController({ state, api });

    await controller.start();
    controller.resize(100, 30, 'term-1');
    controller.resize(100, 30, 'term-1');
    await vi.runAllTimersAsync();

    expect(api.resizeTerminal).toHaveBeenCalledTimes(1);
    expect(state.streams['term-1'].gridPending).toBe(true);
    expect(state.actionError).toBe('resize rejected');
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

  it('removes a running terminal immediately and stops it in the background', async () => {
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({
      streams,
      terminals: [terminal('term-1')],
    });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    expect(state.terminals).toHaveLength(1);
    expect(streams).toHaveLength(1);

    // The server-side stop is slow; the tile must be gone before it lands.
    let resolveKill;
    api.killTerminal.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveKill = resolve;
      }),
    );
    const closing = controller.closeTerminal('term-1');

    // Optimistic removal already happened synchronously.
    expect(state.terminals).toEqual([]);
    expect(state.selectedTerminalId).toBe('');
    expect(Object.keys(state.streams)).toEqual([]);
    expect(api.killTerminal).toHaveBeenCalledWith('term-1');
    expect(api.forgetTerminal).not.toHaveBeenCalled();

    resolveKill({});
    await closing;
    expect(api.forgetTerminal).toHaveBeenCalledWith('term-1');
    controller.destroy();
  });

  it('keeps a closing terminal hidden from reloads during the background stop', async () => {
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({
      streams,
      terminals: [terminal('term-1')],
    });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    let resolveKill;
    api.killTerminal.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveKill = resolve;
      }),
    );
    const closing = controller.closeTerminal('term-1');

    // A catalog invalidation reload lands while the stop is still in flight;
    // the server still lists the session, but it must not reappear.
    api.listTerminals.mockResolvedValueOnce({
      groups: [manual({ terminal_count: 1 })],
      terminals: [terminal('term-1', { state: 'exited' })],
    });
    await controller.loadTerminals({ silent: true });
    expect(state.terminals).toEqual([]);
    expect(state.groups).toEqual([]);

    resolveKill({});
    await closing;
    controller.destroy();
  });

  it('surfaces a close failure to a remounted controller', async () => {
    const state1 = createTerminalsViewState();
    const streams1 = [];
    const api1 = fakeApi({
      streams: streams1,
      terminals: [terminal('term-1')],
    });
    const controller1 = createTerminalsController({ state: state1, api: api1 });
    await controller1.start();

    let rejectKill;
    api1.killTerminal.mockReturnValueOnce(
      new Promise((_, reject) => {
        rejectKill = reject;
      }),
    );
    const closing = controller1.closeTerminal('term-1');
    controller1.destroy(); // navigate away while the stop is in flight

    // A remounted controller loads while the stop is still pending; the
    // session stays hidden behind the closing filter.
    const state2 = createTerminalsViewState();
    const streams2 = [];
    const api2 = fakeApi({
      streams: streams2,
      terminals: [terminal('term-1')],
    });
    const controller2 = createTerminalsController({ state: state2, api: api2 });
    await controller2.start();
    expect(state2.terminals).toEqual([]);

    // The stop fails: the remounted controller must surface the still-running
    // session instead of leaving it hidden.
    rejectKill(new Error('stop failed'));
    await closing;
    await vi.waitFor(() => expect(state2.terminals).toHaveLength(1));
    expect(state2.actionError).toBe('stop failed');
    controller2.destroy();
  });

  it('forgets a finished terminal without killing it', async () => {
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({
      streams,
      terminals: [terminal('term-1', { state: 'exited' })],
    });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    const closed = await controller.closeTerminal('term-1');

    expect(closed).toBe(true);
    expect(api.killTerminal).not.toHaveBeenCalled();
    expect(api.forgetTerminal).toHaveBeenCalledWith('term-1');
    expect(state.terminals).toEqual([]);
    controller.destroy();
  });

  it('restores a running terminal when the stop fails', async () => {
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({
      streams,
      terminals: [terminal('term-1')],
    });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    api.killTerminal.mockRejectedValueOnce(new Error('stop failed'));

    const closed = await controller.closeTerminal('term-1');

    expect(closed).toBe(false);
    expect(state.actionError).toBe('stop failed');
    // The terminal is restored from the server list and its stream reconnects.
    expect(state.terminals).toHaveLength(1);
    expect(state.terminals[0].terminal_id).toBe('term-1');
    expect(Object.keys(state.streams)).toEqual(['term-1']);
    controller.destroy();
  });

  it('removes an empty automatic group when its last terminal is closed', async () => {
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({
      streams,
      terminals: [terminal('term-1')],
    });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    expect(state.groups).toHaveLength(1);
    expect(state.groups[0].terminal_count).toBe(1);

    await controller.closeTerminal('term-1');

    expect(state.groups).toEqual([]);
    expect(state.selectedGroupId).toBe('');
    controller.destroy();
  });

  it('decrements the group count when a terminal in a user group is closed', async () => {
    const state = createTerminalsViewState();
    const streams = [];
    const groups = [
      {
        ...manual(),
        group_id: 'g1',
        name: 'Work',
        kind: 'user',
        terminal_count: 2,
        live_count: 2,
      },
    ];
    const api = fakeApi({
      streams,
      groups,
      terminals: [
        terminal('term-1', { group_id: 'g1' }),
        terminal('term-2', { group_id: 'g1' }),
      ],
    });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    await controller.closeTerminal('term-1');

    expect(state.groups[0].terminal_count).toBe(1);
    expect(state.terminals.map((t) => t.terminal_id)).toEqual(['term-2']);
    expect(state.selectedTerminalId).toBe('term-2');
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

  it('force-closes a wedged connecting socket after the budget and reconnects', async () => {
    vi.useFakeTimers();
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({ streams, socketReadyState: 0 });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    expect(streams[0].connection.socket.readyState).toBe(0);

    // Nothing arrived and the socket never opened: after the wedge budget the
    // controller must close it and schedule an immediate reconnect.
    await vi.advanceTimersByTimeAsync(8_001);

    expect(streams[0].connection.close).toHaveBeenCalled();
    expect(streams).toHaveLength(2);
    expect(state.streams['term-1'].status).toBe(TERMINAL_STREAM_RECONNECTING);

    // The fresh socket opens cleanly; its wedge timer must never fire again.
    streams[1].connection.socket.readyState = 1;
    streams[1].emit({
      type: 'terminal_ready',
      sequence: 1,
      terminal: terminal('term-1'),
      ansi: '\u001b[2Jready',
    });
    await vi.advanceTimersByTimeAsync(20_000);

    expect(streams).toHaveLength(2);
    expect(state.streams['term-1'].status).toBe(TERMINAL_STREAM_CONNECTED);
    controller.destroy();
  });

  it('does not force-close a socket that opens within the budget', async () => {
    vi.useFakeTimers();
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({ streams, socketReadyState: 0 });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    // The socket opens (readyState flips) before the wedge budget elapses.
    streams[0].connection.socket.readyState = 1;
    streams[0].emit({
      type: 'terminal_ready',
      sequence: 1,
      terminal: terminal('term-1'),
      ansi: '\\u001b[2Jready',
    });
    await vi.runAllTimersAsync();

    expect(streams[0].connection.close).not.toHaveBeenCalled();
    expect(streams).toHaveLength(1);
    expect(state.streams['term-1'].status).toBe(TERMINAL_STREAM_CONNECTED);
    controller.destroy();
  });

  it('sanitizes output chunks split across frames before rendering', async () => {
    vi.useFakeTimers();
    const state = createTerminalsViewState();
    const streams = [];
    const output = [];
    const api = fakeApi({ streams });
    const controller = createTerminalsController({
      state,
      api,
      onOutput: (terminalId, data) => output.push([terminalId, data]),
    });

    await controller.start();
    streams[0].emit({
      type: 'terminal_ready',
      sequence: 1,
      terminal: terminal('term-1'),
      ansi: '\\u001b[2Jshell',
    });
    // A CSI escape torn across two frames must be reassembled before xterm.
    streams[0].emit({
      type: 'terminal_output',
      sequence: 2,
      data: 'build \u001b[',
    });
    streams[0].emit({
      type: 'terminal_output',
      sequence: 3,
      data: '31mok\u001b[0m',
    });

    expect(output).toEqual([
      ['term-1', 'build '],
      ['term-1', '\u001b[31mok\u001b[0m'],
    ]);
    controller.destroy();
  });
});

function fakeApi({
  streams,
  terminals = [terminal('term-1')],
  groups,
  socketReadyState = 1,
}) {
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
      const connection = {
        close: vi.fn(),
        socket: { readyState: socketReadyState },
      };
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
