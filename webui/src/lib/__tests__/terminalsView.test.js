import { describe, expect, it, vi } from 'vitest';
import {
  clampTerminalGrid,
  layoutForCount,
  parseTerminalCommandLine,
  formatTerminalCommandLine,
} from '../terminalsView.js';
import {
  startTerminals,
  readyEvent,
  manual,
  userGroup,
  terminal,
  launchHistory,
  deferred,
} from './terminalsView.support.js';

function terminalIds(state) {
  return state.terminals.map((item) => item.terminal_id);
}

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

describe('terminal grid helpers', () => {
  it('clamps fitted grids into the server-accepted dimension window', () => {
    expect(clampTerminalGrid(10, 4)).toEqual({ columns: 40, rows: 10 });
    expect(clampTerminalGrid(500, 200)).toEqual({ columns: 240, rows: 80 });
    expect(clampTerminalGrid(100.9, 31.2)).toEqual({ columns: 100, rows: 31 });
    expect(clampTerminalGrid(120, 32)).toEqual({ columns: 120, rows: 32 });
  });

  // Spans as "row.column", with "*n" for a column span.
  it.each([
    [1, 1, 1, '0.0'],
    [2, 1, 2, '0.0 0.1'],
    [3, 2, 2, '0.0*2 1.0 1.1'],
    [5, 2, 3, '0.0 0.1 0.2 1.0 1.1'],
    [8, 2, 4, '0.0 0.1 0.2 0.3 1.0 1.1 1.2 1.3'],
    [9, 3, 4, '0.0 0.1 0.2 0.3 1.0 1.1 1.2 1.3 2.0'],
    [0, 0, 0, ''],
    [-2, 0, 0, ''],
    ['three', 0, 0, ''],
  ])(
    'lays out %s terminals in %i rows of %i columns',
    (count, rows, columns, spans) => {
      expect(layoutForCount(count)).toEqual({
        rows,
        columns,
        spans: spans
          .split(' ')
          .filter(Boolean)
          .map((cell) => {
            const [position, columnSpan = '1'] = cell.split('*');
            const [row, column] = position.split('.').map(Number);
            return { row, column, rowSpan: 1, columnSpan: Number(columnSpan) };
          }),
      });
    },
  );
});

describe('terminal catalog', () => {
  it('keeps a valid selection, including retained finished terminals, and closes streams of removed ones', async () => {
    const { state, streams, api, controller } = await startTerminals({
      terminals: [terminal('term-1'), terminal('term-2')],
    });
    expect(streams).toHaveLength(2);
    controller.selectTerminal('term-2');

    api.listTerminals.mockResolvedValueOnce({
      groups: [manual({ terminal_count: 2 })],
      terminals: [
        terminal('term-2', { state: 'ready' }),
        terminal('term-3', { state: 'exited' }),
      ],
    });
    await controller.loadTerminals();

    expect(state.selectedGroupId).toBe('auto:manual');
    expect(state.selectedTerminalId).toBe('term-2');
    expect(terminalIds(state)).toEqual(['term-2', 'term-3']);
    expect(streams[0].connection.close).toHaveBeenCalledWith(
      1000,
      'terminals-view-close',
    );
    expect(Object.keys(state.streams)).toEqual(['term-2', 'term-3']);
  });

  it('starts, selects, and connects one manual terminal alongside existing streams', async () => {
    const { state, streams, api, controller } = await startTerminals();
    api.startTerminal.mockResolvedValueOnce({
      terminal: terminal('manual-1', { command: 'codex', owner: null }),
      launch_history: [launchHistory('codex', { command: 'codex' })],
    });

    const started = await controller.startManualTerminal({ command: 'codex' });

    expect(api.startTerminal).toHaveBeenCalledWith({ command: 'codex' });
    expect(started).toMatchObject({ terminal_id: 'manual-1', owner: null });
    expect(state.selectedTerminalId).toBe('manual-1');
    expect(terminalIds(state)).toEqual(['term-1', 'manual-1']);
    expect(api.setTerminalGroupOrder).toHaveBeenCalledWith('auto:manual', [
      'term-1',
      'manual-1',
    ]);
    expect(state.startError).toBe('');
    expect(state.launchHistory).toEqual([
      launchHistory('codex', { command: 'codex' }),
    ]);
    expect(streams).toHaveLength(2);
    expect(state.streams['manual-1'].status).toBe('connecting');
  });

  it('removes a running terminal at once, stops it in the background, and keeps it hidden from reloads meanwhile', async () => {
    const { state, streams, api, controller } = await startTerminals();
    expect(streams).toHaveLength(1);
    const [kill, finishKill] = deferred();
    api.killTerminal.mockReturnValueOnce(kill);

    const closing = controller.closeTerminal('term-1');

    // Optimistic removal: the tile and its now empty automatic group go
    // before the slow server-side stop lands.
    expect(state.terminals).toEqual([]);
    expect(state.groups).toEqual([]);
    expect(state.selectedGroupId).toBe('');
    expect(state.selectedTerminalId).toBe('');
    expect(state.streams).toEqual({});
    expect(api.killTerminal).toHaveBeenCalledWith('term-1');
    expect(api.forgetTerminal).not.toHaveBeenCalled();

    // An invalidation reload still lists the session; it must not reappear.
    api.listTerminals.mockResolvedValueOnce({
      groups: [manual({ terminal_count: 1 })],
      terminals: [terminal('term-1', { state: 'exited' })],
    });
    await controller.loadTerminals({ silent: true });
    expect(state.terminals).toEqual([]);
    expect(state.groups).toEqual([]);

    finishKill({});
    await expect(closing).resolves.toBe(true);
    expect(api.forgetTerminal).toHaveBeenCalledWith('term-1');
  });

  it('forgets a finished terminal without stopping it and keeps the user group count in sync', async () => {
    const { state, api, controller } = await startTerminals({
      groups: [userGroup('g1', 'Work', 2)],
      terminals: [
        terminal('term-1', { group_id: 'g1', state: 'exited' }),
        terminal('term-2', { group_id: 'g1' }),
      ],
    });

    await expect(controller.closeTerminal('term-1')).resolves.toBe(true);

    expect(api.killTerminal).not.toHaveBeenCalled();
    expect(api.forgetTerminal).toHaveBeenCalledWith('term-1');
    expect(state.groups[0].terminal_count).toBe(1);
    expect(terminalIds(state)).toEqual(['term-2']);
    expect(state.selectedTerminalId).toBe('term-2');
  });

  it('restores a running terminal when the stop fails', async () => {
    const { state, api, controller } = await startTerminals();
    api.killTerminal.mockRejectedValueOnce(new Error('stop failed'));

    await expect(controller.closeTerminal('term-1')).resolves.toBe(false);

    expect(state.actionError).toBe('stop failed');
    // Restored from the server list; its stream reconnects.
    expect(terminalIds(state)).toEqual(['term-1']);
    expect(Object.keys(state.streams)).toEqual(['term-1']);
  });

  it('surfaces a close failure to a remounted controller', async () => {
    const first = await startTerminals();
    const [kill, , failKill] = deferred();
    first.api.killTerminal.mockReturnValueOnce(kill);
    const closing = first.controller.closeTerminal('term-1');
    first.controller.destroy(); // navigate away while the stop is in flight

    // A remounted controller loads while the stop is still pending; the
    // session stays hidden behind the closing filter.
    const second = await startTerminals();
    expect(second.state.terminals).toEqual([]);

    // The stop fails: the remounted controller surfaces the still-running
    // session instead of leaving it hidden.
    failKill(new Error('stop failed'));
    await closing;
    await vi.waitFor(() => expect(second.state.terminals).toHaveLength(1));
    expect(second.state.actionError).toBe('stop failed');
  });

  it('closes every stream while the server is unavailable and reloads on recovery', async () => {
    vi.useFakeTimers();
    const { state, streams, controller } = await startTerminals({
      terminals: [terminal('term-1'), terminal('term-2')],
    });

    controller.setServerUnavailable(true);
    expect(streams[0].connection.close).toHaveBeenCalled();
    expect(streams[1].connection.close).toHaveBeenCalled();
    expect(state.streams).toEqual({});

    controller.setServerUnavailable(false);
    await vi.runAllTimersAsync();
    expect(streams).toHaveLength(4);
    expect(Object.keys(state.streams)).toEqual(['term-1', 'term-2']);
  });

  it('switches groups, reconnects only the visible terminals, and keeps selection valid', async () => {
    const { state, streams, api, controller } = await startTerminals({
      groups: [userGroup('g1', 'Work', 1), userGroup('g2', 'Play', 1)],
      terminals: [
        terminal('term-1', { group_id: 'g1' }),
        terminal('term-2', { group_id: 'g2' }),
      ],
    });
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

    // A group removed elsewhere falls back to the first remaining group.
    api.listTerminals.mockResolvedValueOnce({
      groups: [userGroup('g2', 'Play', 1)],
      terminals: [terminal('term-2', { group_id: 'g2' })],
    });
    await controller.loadTerminals();
    expect(state.selectedGroupId).toBe('g2');
    expect(state.selectedTerminalId).toBe('term-2');

    api.listTerminals.mockResolvedValueOnce({ groups: [], terminals: [] });
    await controller.loadTerminals();
    expect(state.selectedGroupId).toBe('');
    expect(state.selectedTerminalId).toBe('');
  });

  it('reorders a group optimistically and persists the order', async () => {
    const { state, api, controller } = await startTerminals({
      terminals: [terminal('term-1'), terminal('term-2')],
    });

    controller.reorderGroup('auto:manual', ['term-2', 'term-1']);

    expect(api.setTerminalGroupOrder).toHaveBeenCalledWith('auto:manual', [
      'term-2',
      'term-1',
    ]);
    expect(terminalIds(state)).toEqual(['term-2', 'term-1']);
  });

  it('creates a user group and selects it', async () => {
    const { state, api, controller } = await startTerminals();
    api.createTerminalGroup.mockResolvedValueOnce({
      group: { group_id: 'new-group', name: 'Docs', kind: 'user' },
    });

    const group = await controller.createGroup('Docs');

    expect(group?.group_id).toBe('new-group');
    expect(state.selectedGroupId).toBe('new-group');
  });
});

describe('terminal streams', () => {
  it('opens one stream per listed terminal and applies per-terminal events', async () => {
    const snapshots = [];
    const output = [];
    const { state, streams } = await startTerminals({
      terminals: [terminal('term-1'), terminal('term-2')],
      controller: {
        onSnapshot: (terminalId, ansi) => snapshots.push([terminalId, ansi]),
        onOutput: (terminalId, data) => output.push([terminalId, data]),
      },
    });
    expect(state.selectedTerminalId).toBe('term-1');
    expect(Object.keys(state.streams)).toEqual(['term-1', 'term-2']);

    streams[0].emit(readyEvent('term-1', 4, '\u001b[2Jready'));
    streams[1].emit(readyEvent('term-2', 10, '\u001b[2Jsecond'));
    streams[1].emit({ type: 'terminal_output', sequence: 11, data: 'next' });
    streams[0].emit({
      type: 'terminal_snapshot',
      sequence: 5,
      terminal: terminal('term-1', { title: 'Codex tests' }),
      ansi: '\u001b[2Jshell restored',
    });

    expect(state.streams['term-1'].status).toBe('connected');
    expect(state.streams['term-2'].status).toBe('connected');
    expect(snapshots).toEqual([
      ['term-1', '\u001b[2Jready'],
      ['term-2', '\u001b[2Jsecond'],
      ['term-1', '\u001b[2Jshell restored'],
    ]);
    expect(output).toEqual([['term-2', 'next']]);
    expect(state.terminals[0].title).toBe('Codex tests');
  });

  it.each([
    ['a sequence gap', 'gap'],
    ['a closed socket', ''],
  ])('reconnects only the terminal hit by %s', async (_cause, errorCode) => {
    vi.useFakeTimers();
    const { state, streams } = await startTerminals({
      terminals: [terminal('term-1'), terminal('term-2')],
    });
    streams[0].emit(readyEvent('term-1', 4));
    streams[1].emit(readyEvent('term-2', 10));
    streams[1].emit({ type: 'terminal_output', sequence: 11, data: 'ok' });

    if (errorCode === 'gap') {
      streams[0].emit({ type: 'terminal_output', sequence: 9, data: 'gap' });
      expect(streams[0].connection.close).toHaveBeenCalled();
    } else {
      streams[0].close();
    }
    expect(state.streams['term-1']).toMatchObject({
      status: 'reconnecting',
      errorCode,
    });

    await vi.runAllTimersAsync();
    expect(streams).toHaveLength(3);
    expect(state.streams['term-2']).toMatchObject({
      status: 'connected',
      errorCode: '',
    });
  });

  it('keeps a finished terminal selected as a snapshot without reconnecting it', async () => {
    vi.useFakeTimers();
    const { state, streams, api } = await startTerminals();
    api.listTerminals.mockResolvedValueOnce({
      groups: [manual({ terminal_count: 1 })],
      terminals: [terminal('term-1', { state: 'exited' })],
    });

    streams[0].emit(readyEvent('term-1', 5, 'done', { state: 'exited' }));
    streams[0].close();
    await vi.runAllTimersAsync();

    expect(streams).toHaveLength(1);
    expect(state.streams['term-1'].status).toBe('snapshot');
    expect(state.terminals[0].state).toBe('exited');
    expect(state.selectedTerminalId).toBe('term-1');
  });

  it('force-closes a socket stuck connecting past the budget and reconnects once', async () => {
    vi.useFakeTimers();
    const { state, streams } = await startTerminals({ socketReadyState: 0 });

    // Nothing arrived and the socket never opened: after the budget the
    // controller closes it and reconnects immediately.
    await vi.advanceTimersByTimeAsync(8_001);
    expect(streams[0].connection.close).toHaveBeenCalled();
    expect(streams).toHaveLength(2);
    expect(state.streams['term-1'].status).toBe('reconnecting');

    // A socket that opens within the budget is never force-closed.
    streams[1].connection.socket.readyState = 1;
    streams[1].emit(readyEvent('term-1', 1, 'ready'));
    await vi.advanceTimersByTimeAsync(20_000);

    expect(streams[1].connection.close).not.toHaveBeenCalled();
    expect(streams).toHaveLength(2);
    expect(state.streams['term-1'].status).toBe('connected');
  });

  it('holds a torn escape sequence until its frame completes and passes everything else byte-exact', async () => {
    const output = [];
    const { streams } = await startTerminals({
      controller: { onOutput: (_id, data) => output.push(data) },
    });
    const frames = [
      ['build \u001b[', 'build '],
      ['31mok\u001b[0m', '\u001b[31mok\u001b[0m'],
      ['a\r\n\r\n\r', 'a\r\n\r\n\r'],
      ['\nb' + '\r\n'.repeat(40), '\nb' + '\r\n'.repeat(40)],
      ['text \u001b', 'text '],
      ['[2', null],
      ['K!', '\u001b[2K!'],
    ];
    streams[0].emit(readyEvent('term-1', 1));
    frames.forEach(([data], index) =>
      streams[0].emit({ type: 'terminal_output', sequence: index + 2, data }),
    );
    expect(output).toEqual(frames.map(([, shown]) => shown).filter(Boolean));

    // A fresh snapshot drops a held tail from before it.
    output.length = 0;
    streams[0].emit({
      type: 'terminal_output',
      sequence: 9,
      data: 'x \u001b[',
    });
    streams[0].emit(readyEvent('term-1', 20));
    streams[0].emit({ type: 'terminal_output', sequence: 21, data: '31mY' });
    expect(output).toEqual(['x ', '31mY']);
  });
});

describe('terminal input', () => {
  it('batches input per exact terminal and keeps the previous buffer on focus switch', async () => {
    vi.useFakeTimers();
    const { api, controller } = await startTerminals({
      terminals: [terminal('term-1'), terminal('term-2')],
    });

    controller.queueInput('hel');
    controller.queueInput('lo');
    controller.selectTerminal('term-2');
    controller.queueInput('hi', { terminalId: 'term-2' });
    controller.queueInput('\r', { terminalId: 'term-2', immediate: true });
    await vi.runAllTimersAsync();

    expect(api.sendTerminalInput.mock.calls).toEqual([
      ['term-2', 'hi\r'],
      ['term-1', 'hello'],
    ]);
  });

  it('drops pending input when the terminal ends and hides the terminal-closed error of an in-flight request', async () => {
    vi.useFakeTimers();
    const { state, streams, api, controller } = await startTerminals();
    const [input, , failInput] = deferred();
    api.sendTerminalInput.mockReturnValueOnce(input);
    streams[0].emit(readyEvent('term-1', 1));

    controller.queueInput('hi', { immediate: true });
    await vi.advanceTimersByTimeAsync(0);
    controller.queueInput('buffered');
    streams[0].emit({
      type: 'terminal_state',
      sequence: 2,
      terminal: terminal('term-1', { state: 'exited' }),
    });
    failInput(new Error('Terminal Session is no longer running'));
    controller.queueInput('after end', { immediate: true });
    await vi.runAllTimersAsync();

    expect(api.sendTerminalInput.mock.calls).toEqual([['term-1', 'hi']]);
    expect(state.actionError).toBe('');
    expect(state.streams['term-1'].status).toBe('snapshot');
  });
});

describe('terminal resize', () => {
  it.each([
    ['a debounced burst', false],
    ['an immediate request', true],
  ])('drops %s ending at the known dimensions', async (_kind, immediate) => {
    vi.useFakeTimers();
    const { api, controller } = await startTerminals();

    // The terminal runs at 120x32: a burst ending there never reaches the
    // server, not even with an intermediate size in between.
    if (!immediate) {
      controller.resize(100, 30, 'term-1');
    }
    controller.resize(120, 32, 'term-1', immediate);
    await vi.runAllTimersAsync();

    expect(api.resizeTerminal).not.toHaveBeenCalled();
  });

  it('collapses a burst into one debounced request with the final size and adopts the confirmed grid', async () => {
    vi.useFakeTimers();
    const { state, api, controller } = await startTerminals();
    api.resizeTerminal.mockResolvedValue({
      terminal: { columns: 112, rows: 33 },
    });

    controller.resize(100, 30, 'term-1');
    controller.resize(110, 31, 'term-1');
    controller.resize(110, 31, 'term-1');
    await vi.advanceTimersByTimeAsync(50);
    expect(api.resizeTerminal).not.toHaveBeenCalled();
    await vi.runAllTimersAsync();

    expect(api.resizeTerminal.mock.calls).toEqual([['term-1', 110, 31]]);
    expect(state.terminals[0]).toMatchObject({ columns: 112, rows: 33 });
    expect(state.streams['term-1'].gridPending).toBe(false);

    // A fit at the confirmed size is a no-op.
    controller.resize(112, 33, 'term-1');
    await vi.runAllTimersAsync();
    expect(api.resizeTerminal).toHaveBeenCalledTimes(1);
  });

  it('sends an immediate resize at once, clamped into the server bounds', async () => {
    const { state, api, controller } = await startTerminals();

    // Maximize sends its measurement without the debounce; a tiny tile fits
    // below the 40x10 minimum and requests the legal grid.
    controller.resize(22, 6, 'term-1', true);
    expect(api.resizeTerminal).toHaveBeenCalledWith('term-1', 40, 10);
    await vi.waitFor(() =>
      expect(state.terminals[0]).toMatchObject({ columns: 40, rows: 10 }),
    );
  });

  it('keeps the grid divergence visible while a resize correction failed', async () => {
    vi.useFakeTimers();
    const { state, api, controller } = await startTerminals();
    api.resizeTerminal.mockRejectedValue(new Error('resize rejected'));

    controller.resize(100, 30, 'term-1');
    await vi.runAllTimersAsync();

    expect(api.resizeTerminal).toHaveBeenCalledTimes(1);
    expect(state.streams['term-1'].gridPending).toBe(true);
    expect(state.actionError).toBe('resize rejected');
  });

  it.each([
    ['a return to the prior grid', [[120, 32]], [[120, 32]]],
    [
      'the newest of several sizes',
      [
        [100, 30],
        [110, 31],
      ],
      [[110, 31]],
    ],
    [
      'nothing when the newest matches it',
      [
        [100, 30],
        [90, 24],
      ],
      [],
    ],
  ])(
    'queues %s behind an in-flight resize',
    async (_case, later, followUps) => {
      const { state, api, controller } = await startTerminals();
      const requests = [];
      api.resizeTerminal.mockImplementation((_id, columns, rows) => {
        const [promise, resolve] = deferred();
        requests.push(() => resolve({ terminal: { columns, rows } }));
        return promise;
      });

      controller.resize(90, 24, 'term-1', true);
      for (const [columns, rows] of later) {
        controller.resize(columns, rows, 'term-1', true);
      }
      expect(api.resizeTerminal).toHaveBeenCalledTimes(1);

      requests[0]();
      await vi.waitFor(() =>
        expect(api.resizeTerminal).toHaveBeenCalledTimes(1 + followUps.length),
      );
      if (followUps.length) {
        expect(state.streams['term-1'].gridPending).toBe(true);
        requests[1]();
      }
      const [columns, rows] = followUps.at(-1) ?? [90, 24];
      await vi.waitFor(() =>
        expect(state.terminals[0]).toMatchObject({ columns, rows }),
      );
      expect(api.resizeTerminal.mock.calls.slice(1)).toEqual(
        followUps.map((size) => ['term-1', ...size]),
      );
      expect(state.streams['term-1'].gridPending).toBe(false);
    },
  );

  it('does not resize a finished terminal', async () => {
    const { api, controller } = await startTerminals({
      terminals: [terminal('term-1', { state: 'exited' })],
    });

    controller.resize(100, 30, 'term-1', true);

    expect(api.resizeTerminal).not.toHaveBeenCalled();
  });
});
