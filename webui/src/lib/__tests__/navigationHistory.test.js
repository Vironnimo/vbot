import { describe, expect, it } from 'vitest';

import {
  createNavigationHistoryState,
  isNavigationHistoryState,
  locationHashForView,
  sameNavigationSelection,
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

  it('defaults the selection to null when none is given', () => {
    const state = createNavigationHistoryState('chat', null);

    expect(state.selection).toBeNull();
  });

  it('normalizes the selection fields, keeping the projectAgentId tri-state', () => {
    const state = createNavigationHistoryState('chat', null, {
      agentId: 'alpha',
      projectId: 'vbot',
      projectAgentId: 'builder',
    });

    expect(state.selection).toEqual({
      agentId: 'alpha',
      projectId: 'vbot',
      projectAgentId: 'builder',
    });

    const identityAlongsideProject = createNavigationHistoryState(
      'chat',
      null,
      {
        agentId: 'alpha',
        projectId: 'vbot',
        projectAgentId: '',
      },
    );
    expect(identityAlongsideProject.selection.projectAgentId).toBe('');

    const nothingRemembered = createNavigationHistoryState('chat', null, {
      agentId: 'alpha',
    });
    expect(nothingRemembered.selection).toEqual({
      agentId: 'alpha',
      projectId: '',
      projectAgentId: null,
    });
  });
});

describe('sameNavigationSelection', () => {
  it('treats two empty selections as equal (legacy/foreign entries)', () => {
    expect(sameNavigationSelection(null, null)).toBe(true);
    expect(sameNavigationSelection(undefined, null)).toBe(true);
  });

  it('distinguishes empty from set selections', () => {
    const selection = { agentId: 'alpha', projectId: '', projectAgentId: null };

    expect(sameNavigationSelection(null, selection)).toBe(false);
    expect(sameNavigationSelection(selection, null)).toBe(false);
  });

  it('compares agent, project, and project-agent (tri-state) fields', () => {
    const base = { agentId: 'alpha', projectId: 'vbot', projectAgentId: '' };

    expect(sameNavigationSelection(base, { ...base })).toBe(true);
    expect(sameNavigationSelection(base, { ...base, agentId: 'beta' })).toBe(
      false,
    );
    expect(sameNavigationSelection(base, { ...base, projectId: '' })).toBe(
      false,
    );
    expect(
      sameNavigationSelection(base, { ...base, projectAgentId: 'builder' }),
    ).toBe(false);
    expect(
      sameNavigationSelection(base, { ...base, projectAgentId: null }),
    ).toBe(false);
  });

  it('coerces missing fields to their empty forms', () => {
    expect(
      sameNavigationSelection(
        { agentId: 'alpha' },
        { agentId: 'alpha', projectId: '', projectAgentId: null },
      ),
    ).toBe(true);
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
