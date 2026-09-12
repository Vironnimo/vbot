import { describe, expect, it, vi } from 'vitest';
import {
  createTerminalsController,
  createTerminalsViewState,
} from '../terminalsView.js';
import { fakeApi, terminal } from './terminalsView.support.js';

describe('terminal live controller', () => {
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
    api.resizeTerminal.mockResolvedValue({
      terminal: { columns: 110, rows: 31 },
    });
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

  it('restores the original grid when the earlier resize is still in flight', async () => {
    const state = createTerminalsViewState();
    const api = fakeApi({ streams: [] });
    let finishFirst;
    let finishSecond;
    api.resizeTerminal
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finishFirst = resolve;
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finishSecond = resolve;
          }),
      );
    const controller = createTerminalsController({ state, api });
    await controller.start();
    controller.resize(90, 24, 'term-1', true);
    controller.resize(120, 32, 'term-1', true);
    expect(api.resizeTerminal).toHaveBeenCalledTimes(1);
    finishFirst({ terminal: { columns: 90, rows: 24 } });
    await Promise.resolve();
    expect(api.resizeTerminal).toHaveBeenLastCalledWith('term-1', 120, 32);
    expect(state.streams['term-1'].gridPending).toBe(true);
    finishSecond({ terminal: { columns: 120, rows: 32 } });
    await Promise.resolve();
    expect(state.terminals[0]).toMatchObject({ columns: 120, rows: 32 });
    expect(state.streams['term-1'].gridPending).toBe(false);
    controller.destroy();
  });

  it('replaces intermediate queued sizes with the latest intent', async () => {
    const state = createTerminalsViewState();
    const api = fakeApi({ streams: [] });
    let finish;
    api.resizeTerminal.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const controller = createTerminalsController({ state, api });
    await controller.start();
    controller.resize(90, 24, 'term-1', true);
    controller.resize(100, 30, 'term-1', true);
    controller.resize(110, 31, 'term-1', true);
    finish({ terminal: { columns: 90, rows: 24 } });
    await Promise.resolve();
    await Promise.resolve();
    expect(api.resizeTerminal.mock.calls).toEqual([
      ['term-1', 90, 24],
      ['term-1', 110, 31],
    ]);
    expect(state.terminals[0]).toMatchObject({ columns: 110, rows: 31 });
    controller.destroy();
  });

  it('drops an intermediate size when the latest intent matches the in-flight request', async () => {
    const state = createTerminalsViewState();
    const api = fakeApi({ streams: [] });
    let finish;
    api.resizeTerminal.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const controller = createTerminalsController({ state, api });
    await controller.start();
    controller.resize(90, 24, 'term-1', true);
    controller.resize(100, 30, 'term-1', true);
    controller.resize(90, 24, 'term-1', true);
    finish({ terminal: { columns: 90, rows: 24 } });
    await Promise.resolve();
    expect(api.resizeTerminal).toHaveBeenCalledTimes(1);
    expect(state.streams['term-1'].gridPending).toBe(false);
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
});
