// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  clearDraft,
  flushComposerMemory,
  getDraft,
  getHistory,
  pushHistory,
  resetComposerMemory,
  setDraft,
} from '../composerMemory.js';

describe('composerMemory drafts', () => {
  beforeEach(() => {
    localStorage.clear();
    resetComposerMemory();
  });

  afterEach(() => {
    resetComposerMemory();
    localStorage.clear();
  });

  it('returns an empty draft for an unknown or blank key', () => {
    expect(getDraft('agent::session')).toBe('');
    expect(getDraft('')).toBe('');
  });

  it('stores and reads back a per-session draft', () => {
    setDraft('agent::one', 'half a thought');
    setDraft('agent::two', 'a different thought');

    expect(getDraft('agent::one')).toBe('half a thought');
    expect(getDraft('agent::two')).toBe('a different thought');
  });

  it('clears a draft when set to empty text', () => {
    setDraft('agent::one', 'something');
    setDraft('agent::one', '');

    expect(getDraft('agent::one')).toBe('');
  });

  it('clears a draft explicitly', () => {
    setDraft('agent::one', 'something');
    clearDraft('agent::one');

    expect(getDraft('agent::one')).toBe('');
  });

  it('evicts the oldest sessions past the cap', () => {
    for (let index = 0; index < 90; index += 1) {
      setDraft(`agent::s${index}`, `draft ${index}`);
    }

    // The 80-session cap drops the oldest while keeping the most recent.
    expect(getDraft('agent::s0')).toBe('');
    expect(getDraft('agent::s9')).toBe('');
    expect(getDraft('agent::s10')).toBe('draft 10');
    expect(getDraft('agent::s89')).toBe('draft 89');
  });

  it('persists drafts to localStorage on flush', () => {
    setDraft('agent::one', 'keep me');
    flushComposerMemory();

    const stored = JSON.parse(localStorage.getItem('vbot.composer.drafts.v1'));
    expect(stored['agent::one']).toBe('keep me');
  });
});

describe('composerMemory history', () => {
  beforeEach(() => {
    localStorage.clear();
    resetComposerMemory();
  });

  afterEach(() => {
    resetComposerMemory();
    localStorage.clear();
  });

  it('returns an empty history for an unknown or blank key', () => {
    expect(getHistory('agent')).toEqual([]);
    expect(getHistory('')).toEqual([]);
  });

  it('records sent messages newest-first', () => {
    pushHistory('agent', 'first');
    pushHistory('agent', 'second');

    expect(getHistory('agent')).toEqual(['second', 'first']);
  });

  it('ignores a consecutive duplicate send', () => {
    pushHistory('agent', 'same');
    pushHistory('agent', 'same');

    expect(getHistory('agent')).toEqual(['same']);
  });

  it('floats a reused message back to the top without duplicating it', () => {
    pushHistory('agent', 'a');
    pushHistory('agent', 'b');
    pushHistory('agent', 'a');

    expect(getHistory('agent')).toEqual(['a', 'b']);
  });

  it('trims entries and skips blank sends', () => {
    pushHistory('agent', '  spaced  ');
    pushHistory('agent', '   ');

    expect(getHistory('agent')).toEqual(['spaced']);
  });

  it('keeps history scoped per agent', () => {
    pushHistory('agent-a', 'for a');
    pushHistory('agent-b', 'for b');

    expect(getHistory('agent-a')).toEqual(['for a']);
    expect(getHistory('agent-b')).toEqual(['for b']);
  });

  it('caps history length per agent', () => {
    for (let index = 0; index < 120; index += 1) {
      pushHistory('agent', `message ${index}`);
    }

    const history = getHistory('agent');
    expect(history).toHaveLength(100);
    expect(history[0]).toBe('message 119');
    expect(history[99]).toBe('message 20');
  });
});
