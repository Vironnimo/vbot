// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  changedWidgetStates,
  dismissWidgetState,
  flushWidgetStates,
  markWidgetStatesSent,
  pendingWidgetStates,
  renameWidgetStatesAgent,
  reportWidgetState,
  resetWidgetStates,
  storedWidgetState,
} from '../widgetStates.svelte.js';

const SESSION = 'alpha::session-one';
const STORAGE_KEY = 'vbot.chat.widgetStates.v1';

const changedIds = (sessionKey = SESSION) =>
  changedWidgetStates(sessionKey).map((widget) => widget.widgetId);

beforeEach(() => {
  localStorage.clear();
  resetWidgetStates();
});

afterEach(() => {
  vi.restoreAllMocks();
  resetWidgetStates();
  localStorage.clear();
});

describe('widget state store', () => {
  it('counts a widget as changed while its state differs from the first report', () => {
    reportWidgetState(SESSION, 'w1', {
      title: 'Typography',
      state: { line_height: 1.5, font_size: 16 },
      initial: true,
    });
    expect(changedIds()).toEqual([]);

    reportWidgetState(SESSION, 'w1', {
      state: { line_height: 1.6, font_size: 16 },
    });
    expect(changedWidgetStates(SESSION)).toEqual([
      expect.objectContaining({
        widgetId: 'w1',
        title: 'Typography',
        state: { line_height: 1.6, font_size: 16 },
      }),
    ]);

    // Back to the first values, in another key order: unchanged again.
    reportWidgetState(SESSION, 'w1', {
      state: { font_size: 16, line_height: 1.5 },
    });
    expect(changedIds()).toEqual([]);
  });

  it('keeps the stored state when a remounted widget reports its restored state as initial', () => {
    reportWidgetState(SESSION, 'w1', { state: { size: 1 }, initial: true });
    reportWidgetState(SESSION, 'w1', { state: { size: 2 } });

    reportWidgetState(SESSION, 'w1', { state: { size: 1 }, initial: true });

    expect(storedWidgetState(SESSION, 'w1')).toEqual({ size: 2 });
    expect(changedIds()).toEqual(['w1']);
  });

  it('counts everything as changed when the first report of a widget is not its initial one', () => {
    reportWidgetState(SESSION, 'w1', { state: { size: 2 } });

    expect(changedIds()).toEqual(['w1']);
  });

  it('keeps each Session its own state for the same widget id', () => {
    reportWidgetState(SESSION, 'w1', { state: { size: 1 }, initial: true });
    reportWidgetState('alpha::fork', 'w1', {
      state: { size: 1 },
      initial: true,
    });
    reportWidgetState('alpha::fork', 'w1', { state: { size: 3 } });

    expect(changedIds()).toEqual([]);
    expect(changedIds('alpha::fork')).toEqual(['w1']);
    expect(storedWidgetState(SESSION, 'w1')).toEqual({ size: 1 });
  });

  it('hides a dismissed widget until it changes again', () => {
    reportWidgetState(SESSION, 'w1', { state: { size: 1 }, initial: true });
    reportWidgetState(SESSION, 'w1', { state: { size: 2 } });

    dismissWidgetState(SESSION, 'w1');
    expect(changedIds()).toEqual([]);
    expect(pendingWidgetStates(SESSION)).toEqual([]);

    reportWidgetState(SESSION, 'w1', { state: { size: 3 } });
    expect(changedIds()).toEqual(['w1']);
  });

  it('makes a sent state the new baseline and keeps a newer change pending', () => {
    reportWidgetState(SESSION, 'w1', { state: { size: 1 }, initial: true });
    reportWidgetState(SESSION, 'w2', { state: { on: false }, initial: true });
    reportWidgetState(SESSION, 'w1', { state: { size: 2 } });
    reportWidgetState(SESSION, 'w2', { state: { on: true } });
    const sent = pendingWidgetStates(SESSION);

    // w2 changes again while the send is in flight.
    reportWidgetState(SESSION, 'w2', { state: { on: false } });
    markWidgetStatesSent(SESSION, sent);

    expect(changedWidgetStates(SESSION)).toEqual([
      expect.objectContaining({ widgetId: 'w2', state: { on: false } }),
    ]);
  });

  it('attaches the most recently changed widgets, at most eight per send', () => {
    for (let index = 0; index < 10; index += 1) {
      reportWidgetState(SESSION, `w${index}`, { state: { value: index } });
    }

    const pending = pendingWidgetStates(SESSION);

    expect(pending).toHaveLength(8);
    expect(pending[0]).toEqual({ widget_id: 'w9', state: { value: 9 } });
    expect(pending.map((entry) => entry.widget_id)).not.toContain('w0');
    expect(pending.map((entry) => entry.widget_id)).not.toContain('w1');
  });

  it('restores entries from storage after a reload and drops the oldest past the cap', async () => {
    for (let index = 0; index < 205; index += 1) {
      reportWidgetState(SESSION, `w${index}`, { state: { value: index } });
    }
    flushWidgetStates();

    vi.resetModules();
    const reloaded = await import('../widgetStates.svelte.js');

    expect(reloaded.storedWidgetState(SESSION, 'w204')).toEqual({
      value: 204,
    });
    expect(reloaded.storedWidgetState(SESSION, 'w4')).toBeNull();
    expect(reloaded.changedWidgetStates(SESSION)).toHaveLength(200);
  });

  it('keeps working in memory when storage refuses writes', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota');
    });

    reportWidgetState(SESSION, 'w1', { state: { size: 2 } });
    flushWidgetStates();

    expect(storedWidgetState(SESSION, 'w1')).toEqual({ size: 2 });
    expect(localStorage.getItem(STORAGE_KEY) ?? '').not.toContain('w1');
  });

  it("moves a renamed Identity Agent's entries to its new id", () => {
    reportWidgetState(SESSION, 'w1', { state: { size: 2 } });
    reportWidgetState('alphabet::session-two', 'w2', { state: { size: 4 } });

    renameWidgetStatesAgent('alpha', 'omega');

    expect(changedIds()).toEqual([]);
    expect(changedIds('omega::session-one')).toEqual(['w1']);
    expect(changedIds('alphabet::session-two')).toEqual(['w2']);
  });
});
