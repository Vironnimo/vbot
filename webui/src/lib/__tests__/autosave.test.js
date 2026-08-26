import { describe, expect, it, vi } from 'vitest';

import {
  createAutosaveCoordinator,
  createAutosaveParticipant,
  createDebouncedAutosave,
  DEFAULT_AUTOSAVE_DEBOUNCE_MS,
} from '../autosave.js';

function deferred() {
  let resolve;
  const promise = new Promise((next) => {
    resolve = next;
  });
  return { promise, resolve };
}

describe('autosave coordination', () => {
  it('flushes only registered participants with pending changes', async () => {
    let pending = true;
    const pendingParticipant = {
      hasPending: () => pending,
      flush: vi.fn(async () => {
        pending = false;
        return true;
      }),
    };
    const cleanParticipant = {
      hasPending: () => false,
      flush: vi.fn().mockResolvedValue(true),
    };
    const coordinator = createAutosaveCoordinator();
    const unregister = coordinator.register(pendingParticipant);
    coordinator.register(cleanParticipant);

    expect(coordinator.hasPending()).toBe(true);
    await expect(coordinator.flushPending()).resolves.toBe(true);
    expect(pendingParticipant.flush).toHaveBeenCalledOnce();
    expect(cleanParticipant.flush).not.toHaveBeenCalled();

    unregister();
    expect(coordinator.hasPending()).toBe(false);
  });

  it('flushes a participant that becomes dirty during another save', async () => {
    let firstPending = true;
    let secondPending = false;
    const coordinator = createAutosaveCoordinator();
    coordinator.register({
      hasPending: () => firstPending,
      flush: vi.fn(async () => {
        firstPending = false;
        secondPending = true;
        return true;
      }),
    });
    const secondFlush = vi.fn(async () => {
      secondPending = false;
      return true;
    });
    coordinator.register({
      hasPending: () => secondPending,
      flush: secondFlush,
    });

    await expect(coordinator.flushPending()).resolves.toBe(true);
    expect(secondFlush).toHaveBeenCalledOnce();
  });

  it('waits for an in-flight save and persists edits made during that save', async () => {
    const firstSave = deferred();
    const draft = { value: 'first' };
    const savedValues = [];
    const save = vi
      .fn()
      .mockImplementationOnce(() => firstSave.promise)
      .mockImplementationOnce(() => true);
    const participant = createAutosaveParticipant({
      cancelPending: vi.fn(),
      getSnapshot: () => ({ ...draft }),
      hasChanges: () => true,
      save: (reason) => {
        savedValues.push(draft.value);
        return save(reason);
      },
    });

    const initialSave = participant.runSave();
    draft.value = 'latest';
    const flush = participant.flush();
    firstSave.resolve(true);

    await expect(initialSave).resolves.toBe(true);
    await expect(flush).resolves.toBe(true);
    expect(savedValues).toEqual(['first', 'latest']);
    expect(save).toHaveBeenNthCalledWith(1, 'auto');
    expect(save).toHaveBeenNthCalledWith(2, 'transition');
  });

  it('reports a failed transition save without accepting the snapshot', async () => {
    const save = vi.fn().mockResolvedValue(false);
    const participant = createAutosaveParticipant({
      cancelPending: vi.fn(),
      getSnapshot: () => ({ value: 'draft' }),
      hasChanges: () => true,
      save,
    });

    await expect(participant.flush()).resolves.toBe(false);
    await expect(participant.runSave()).resolves.toBe(false);
    expect(save).toHaveBeenCalledOnce();
    expect(participant.hasPending()).toBe(true);
  });
});

describe('debounced autosave', () => {
  it('schedules one save after the debounce window', async () => {
    vi.useFakeTimers();
    const save = vi.fn().mockResolvedValue(true);
    const debounced = createDebouncedAutosave({
      getSnapshot: () => ({ value: 'draft' }),
      hasChanges: () => true,
      save,
      debounceMs: 50,
    });

    debounced.scheduleRun();
    expect(save).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(50);
    expect(save).toHaveBeenCalledTimes(1);

    vi.useRealTimers();
  });

  it('resets the timer on every scheduleRun call', async () => {
    vi.useFakeTimers();
    const save = vi.fn().mockResolvedValue(true);
    const debounced = createDebouncedAutosave({
      getSnapshot: () => ({ value: 'draft' }),
      hasChanges: () => true,
      save,
      debounceMs: 50,
    });

    debounced.scheduleRun();
    await vi.advanceTimersByTimeAsync(30);
    debounced.scheduleRun();
    await vi.advanceTimersByTimeAsync(30);
    expect(save).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(20);
    expect(save).toHaveBeenCalledTimes(1);

    vi.useRealTimers();
  });

  it('cancels a pending run via cancelPendingTimer', async () => {
    vi.useFakeTimers();
    const save = vi.fn().mockResolvedValue(true);
    const debounced = createDebouncedAutosave({
      getSnapshot: () => ({ value: 'draft' }),
      hasChanges: () => true,
      save,
      debounceMs: 50,
    });

    debounced.scheduleRun();
    debounced.cancelPendingTimer();
    await vi.advanceTimersByTimeAsync(60);
    expect(save).not.toHaveBeenCalled();

    vi.useRealTimers();
  });

  it('wires cancelPending into the participant so a flush clears the timer', async () => {
    vi.useFakeTimers();
    const save = vi.fn().mockResolvedValue(true);
    const debounced = createDebouncedAutosave({
      getSnapshot: () => ({ value: 'draft' }),
      hasChanges: () => true,
      save,
      debounceMs: 50,
    });

    debounced.scheduleRun();
    await debounced.participant.flush();
    expect(save).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(60);
    expect(save).toHaveBeenCalledTimes(1);

    vi.useRealTimers();
  });

  it('exposes the default debounce constant', () => {
    expect(DEFAULT_AUTOSAVE_DEBOUNCE_MS).toBe(800);
  });
});
