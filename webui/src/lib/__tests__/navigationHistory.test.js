import { describe, expect, it } from 'vitest';

import {
  createNavigationHistoryState,
  isNavigationHistoryState,
  locationHashForView,
  sameSessionOverride,
  viewIdFromLocationHash,
} from '../navigationHistory.js';

describe('createNavigationHistoryState', () => {
  it('builds a marked state without a session override', () => {
    const state = createNavigationHistoryState('settings');

    expect(state.view).toBe('settings');
    expect(state.session).toBeNull();
    expect(isNavigationHistoryState(state)).toBe(true);
  });

  it('normalizes the session override fields', () => {
    const state = createNavigationHistoryState('chat', {
      agentId: 'alpha',
      sessionId: 'session-1',
      subAgent: 'truthy-but-not-true',
    });

    expect(state.session).toEqual({
      agentId: 'alpha',
      sessionId: 'session-1',
      subAgent: false,
    });
  });

  it('keeps an explicit subAgent flag', () => {
    const state = createNavigationHistoryState('chat', {
      agentId: 'alpha',
      sessionId: 'session-1',
      subAgent: true,
    });

    expect(state.session.subAgent).toBe(true);
  });
});

describe('isNavigationHistoryState', () => {
  it('rejects null and foreign history states', () => {
    expect(isNavigationHistoryState(null)).toBe(false);
    expect(isNavigationHistoryState(undefined)).toBe(false);
    expect(isNavigationHistoryState({})).toBe(false);
    expect(isNavigationHistoryState({ view: 'chat' })).toBe(false);
    expect(
      isNavigationHistoryState({ marker: 'vbot.navigation', view: '' }),
    ).toBe(false);
  });
});

describe('sameSessionOverride', () => {
  it('treats two empty overrides as equal', () => {
    expect(sameSessionOverride(null, null)).toBe(true);
    expect(sameSessionOverride(undefined, null)).toBe(true);
  });

  it('distinguishes empty from set overrides', () => {
    const override = { agentId: 'alpha', sessionId: 's1', subAgent: false };

    expect(sameSessionOverride(null, override)).toBe(false);
    expect(sameSessionOverride(override, null)).toBe(false);
  });

  it('compares agent, session, and sub-agent flag', () => {
    const base = { agentId: 'alpha', sessionId: 's1', subAgent: true };

    expect(sameSessionOverride(base, { ...base })).toBe(true);
    expect(sameSessionOverride(base, { ...base, agentId: 'beta' })).toBe(false);
    expect(sameSessionOverride(base, { ...base, sessionId: 's2' })).toBe(false);
    expect(sameSessionOverride(base, { ...base, subAgent: false })).toBe(false);
  });

  it('coerces a missing subAgent flag to false', () => {
    expect(
      sameSessionOverride(
        { agentId: 'alpha', sessionId: 's1' },
        { agentId: 'alpha', sessionId: 's1', subAgent: false },
      ),
    ).toBe(true);
  });
});

describe('viewIdFromLocationHash', () => {
  const knownViewIds = ['chat', 'settings', 'logs'];

  it('resolves known view hashes with and without a leading slash', () => {
    expect(viewIdFromLocationHash('#settings', knownViewIds)).toBe('settings');
    expect(viewIdFromLocationHash('#/logs', knownViewIds)).toBe('logs');
  });

  it('returns empty for unknown, empty, or missing hashes', () => {
    expect(viewIdFromLocationHash('#unknown', knownViewIds)).toBe('');
    expect(viewIdFromLocationHash('', knownViewIds)).toBe('');
    expect(viewIdFromLocationHash(null, knownViewIds)).toBe('');
  });
});

describe('locationHashForView', () => {
  it('prefixes the view id with a hash', () => {
    expect(locationHashForView('chat')).toBe('#chat');
  });
});
