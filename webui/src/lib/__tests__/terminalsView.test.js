import { describe, expect, it, vi } from 'vitest';
import {
  TERMINAL_STREAM_CONNECTING,
  clampTerminalGrid,
  createPtyFrameSanitizer,
  createTerminalsController,
  createTerminalsViewState,
  layoutForCount,
  parseTerminalCommandLine,
  formatTerminalCommandLine,
  reconcileTerminalList,
  reconcileTerminalLaunchHistory,
  selectedTerminal,
} from '../terminalsView.js';
import {
  fakeApi,
  manual,
  terminal,
  launchHistory,
  span,
} from './terminalsView.support.js';

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
    expect(state.terminals.map((item) => item.terminal_id)).toEqual([
      'term-1',
      'manual-1',
    ]);
    expect(api.setTerminalGroupOrder).toHaveBeenCalledWith('auto:manual', [
      'term-1',
      'manual-1',
    ]);
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
});

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

describe('terminal command line editor', () => {
  it.each([
    ['  ', undefined, []],
    ['codex --profile "work space"', 'codex', ['--profile', 'work space']],
    ["python -c 'print(1 + 2)'", 'python', ['-c', 'print(1 + 2)']],
    ['tool --name="work space"', 'tool', ['--name=work space']],
    [
      String.raw`C:\tools\app.exe C:\work\repo`,
      String.raw`C:\tools\app.exe`,
      [String.raw`C:\work\repo`],
    ],
    ['tool "$HOME" "a;b" "x|y"', 'tool', ['$HOME', 'a;b', 'x|y']],
  ])('reads %s into literal API arguments', (line, command, args) => {
    expect(parseTerminalCommandLine(line)).toEqual({ command, args });
  });

  it.each([
    ['codex "unfinished', 'unclosedQuote'],
    ["codex 'unfinished", 'unclosedQuote'],
    ['"" argument', 'emptyArgument'],
    ['codex "  "', 'emptyArgument'],
  ])('rejects %s before starting a terminal', (line, error) => {
    expect(parseTerminalCommandLine(line)).toEqual({ error });
  });

  it('round-trips saved setups with quotes, whitespace, paths and literal shell characters', () => {
    const command = String.raw`C:\Program Files\tool.exe`;
    const args = [
      'work space',
      ' leading and trailing ',
      'a"b',
      "it's",
      '\\',
      'C:\\my directory\\',
      '\\\\server\\share\\',
      '$HOME',
      'x;y',
      'a|b',
      'tab\there',
      'line\nbreak',
    ];
    expect(
      parseTerminalCommandLine(formatTerminalCommandLine(command, args)),
    ).toEqual({ command, args });
    expect(formatTerminalCommandLine(null)).toBe('');
  });
});
