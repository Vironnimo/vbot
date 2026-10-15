import { afterEach, describe, expect, it, vi } from 'vitest';
import { ensureSessionState } from '../chatState.js';
import { deferred, setupController } from './chatState.support.js';

describe('Session work projection', () => {
  let controller;
  afterEach(() => {
    controller?.destroy();
    vi.useRealTimers();
  });

  it('coalesces lifecycle changes and rejects snapshots superseded during their read', async () => {
    vi.useFakeTimers();
    const first = deferred();
    const next = deferred();
    const getSessionWork = vi
      .fn()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(next.promise);
    const setup = setupController({ operationOverrides: { getSessionWork } });
    controller = setup.controller;
    const session = ensureSessionState(setup.chatState, 'alpha', 'one');
    controller.syncSessionWork(session);
    await vi.advanceTimersByTimeAsync(0);
    controller.syncSessionWork(session);
    controller.syncSessionWork(session);
    first.resolve({ run_active: false, background_work_running: false });
    await vi.advanceTimersByTimeAsync(100);
    expect(getSessionWork).toHaveBeenCalledTimes(2);
    expect(session.workProjection).toBeNull();
    next.resolve({ run_active: false, background_work_running: true });
    await vi.advanceTimersByTimeAsync(0);
    expect(session.workProjection.background_work_running).toBe(true);
  });

  it('starts the new Session read immediately and ignores departed or destroyed reads', async () => {
    vi.useFakeTimers();
    const old = deferred();
    const current = deferred();
    const getSessionWork = vi
      .fn()
      .mockReturnValueOnce(old.promise)
      .mockReturnValueOnce(current.promise);
    const setup = setupController({ operationOverrides: { getSessionWork } });
    controller = setup.controller;
    const first = ensureSessionState(setup.chatState, 'alpha', 'one');
    const second = ensureSessionState(setup.chatState, 'beta', 'two');
    controller.syncSessionWork(first);
    await vi.advanceTimersByTimeAsync(0);
    controller.syncSessionWork(second);
    await vi.advanceTimersByTimeAsync(0);
    expect(getSessionWork).toHaveBeenCalledTimes(2);
    old.resolve({ run_active: false, background_work_running: true });
    await vi.advanceTimersByTimeAsync(0);
    expect(first.workProjection).toBeNull();
    controller.destroy();
    current.reject(new Error('Disconnected'));
    await vi.advanceTimersByTimeAsync(20000);
    expect(getSessionWork).toHaveBeenCalledTimes(2);
    expect(second.workProjection).toBeNull();
  });

  it('retains known work on failure, retries, and refreshes after Stop all', async () => {
    vi.useFakeTimers();
    const getSessionWork = vi
      .fn()
      .mockResolvedValue({ run_active: false, background_work_running: true });
    const setup = setupController({
      operationOverrides: {
        getSessionWork,
        stopAll: vi.fn().mockResolvedValue({ stopped: 1 }),
      },
    });
    controller = setup.controller;
    const session = ensureSessionState(setup.chatState, 'alpha', 'one');
    controller.syncSessionWork(session);
    await vi.advanceTimersByTimeAsync(0);
    getSessionWork.mockRejectedValueOnce(new Error('Offline'));
    controller.syncSessionWork(session);
    await vi.advanceTimersByTimeAsync(100);
    expect(session.workProjection.background_work_running).toBe(true);
    await vi.advanceTimersByTimeAsync(1100);
    expect(getSessionWork).toHaveBeenCalledTimes(3);
    getSessionWork.mockResolvedValue({
      run_active: false,
      background_work_running: false,
    });
    await controller.stopAll(session);
    await vi.advanceTimersByTimeAsync(0);
    expect(getSessionWork).toHaveBeenCalledTimes(4);
    expect(session.workProjection.background_work_running).toBe(false);
  });
});
