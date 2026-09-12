import { describe, expect, it, vi } from 'vitest';
import {
  TERMINAL_STREAM_CONNECTED,
  TERMINAL_STREAM_CONNECTING,
  TERMINAL_STREAM_RECONNECTING,
  TERMINAL_STREAM_SNAPSHOT,
  createTerminalsController,
  createTerminalsViewState,
  selectedTerminal,
} from '../terminalsView.js';
import { fakeApi, manual, terminal } from './terminalsView.support.js';

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
