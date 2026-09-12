// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { autosaveInput, createDebouncedAutosave } from '../autosave.js';

afterEach(() => {
  document.body.replaceChildren();
  vi.useRealTimers();
});

function editor(type = 'text') {
  vi.useFakeTimers();
  const input = document.createElement('input');
  input.type = type;
  document.body.append(input);
  input.focus();
  const action = autosaveInput(input);
  const save = vi.fn().mockResolvedValue(true);
  const autosave = createDebouncedAutosave({
    getSnapshot: () => input.value,
    hasChanges: () => true,
    save,
  });
  return { input, action, save, ...autosave };
}

describe('autosave input boundaries', () => {
  it('keeps an unfinished number focused until blur, then saves once', async () => {
    const form = editor('number');
    form.input.value = '12';
    form.scheduleRun();
    await vi.advanceTimersByTimeAsync(2000);
    expect(form.save).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(form.input);
    form.input.blur();
    await vi.advanceTimersByTimeAsync(0);
    expect(form.save).toHaveBeenCalledOnce();
    form.action.destroy();
  });

  it('waits for composition to finish and a full idle interval', async () => {
    const form = editor();
    form.input.dispatchEvent(new Event('compositionstart'));
    form.scheduleRun();
    await vi.advanceTimersByTimeAsync(2000);
    expect(form.save).not.toHaveBeenCalled();
    form.input.dispatchEvent(new Event('compositionend'));
    await vi.advanceTimersByTimeAsync(799);
    expect(form.save).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(form.save).toHaveBeenCalledOnce();
    expect(document.activeElement).toBe(form.input);
    form.action.destroy();
  });

  it('cancels blur listeners on disposal or an explicit save', async () => {
    const form = editor('number');
    form.scheduleRun();
    await vi.advanceTimersByTimeAsync(800);
    await form.participant.runSave('manual');
    form.input.blur();
    await vi.advanceTimersByTimeAsync(1000);
    expect(form.save).toHaveBeenCalledOnce();
    form.input.focus();
    form.scheduleRun();
    await vi.advanceTimersByTimeAsync(800);
    form.cancelPendingTimer();
    form.input.blur();
    expect(form.save).toHaveBeenCalledOnce();
    form.action.destroy();
  });
});
