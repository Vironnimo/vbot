import { describe, expect, it } from 'vitest';

import {
  createNavigationHistoryState,
  isNavigationHistoryState,
  locationHashForView,
  sameNavigationSelection,
  sameSessionOverride,
  viewIdFromLocationHash,
} from '../navigationHistory.js';

// The history.state entry format App.svelte and appController share.
describe('createNavigationHistoryState()', () => {
  it('builds a marked entry without session override or selection', () => {
    const state = createNavigationHistoryState('settings');

    expect(state).toMatchObject({
      view: 'settings',
      session: null,
      selection: null,
    });
    expect(isNavigationHistoryState(state)).toBe(true);
  });

  it.each([
    ['a non-true sub-agent flag as false', 'truthy-but-not-true', false],
    ['an explicit sub-agent flag', true, true],
  ])(
    'normalizes the session override, keeping %s',
    (_label, subAgent, flag) => {
      expect(
        createNavigationHistoryState('chat', {
          agentId: 'alpha',
          sessionId: 'session-1',
          subAgent,
        }).session,
      ).toEqual({ agentId: 'alpha', sessionId: 'session-1', subAgent: flag });
    },
  );

  // projectAgentId is tri-state: a member id, '' for an identity Agent active
  // alongside the Project, or null when nothing is remembered.
  it.each([
    [
      'a Project member',
      { agentId: 'alpha', projectId: 'vbot', projectAgentId: 'builder' },
      { agentId: 'alpha', projectId: 'vbot', projectAgentId: 'builder' },
    ],
    [
      'an identity Agent alongside the Project',
      { agentId: 'alpha', projectId: 'vbot', projectAgentId: '' },
      { agentId: 'alpha', projectId: 'vbot', projectAgentId: '' },
    ],
    [
      'nothing remembered',
      { agentId: 'alpha' },
      { agentId: 'alpha', projectId: '', projectAgentId: null },
    ],
  ])('normalizes a selection with %s', (_label, selection, stored) => {
    expect(
      createNavigationHistoryState('chat', null, selection).selection,
    ).toEqual(stored);
  });
});

describe('isNavigationHistoryState()', () => {
  it.each([
    null,
    undefined,
    {},
    { view: 'chat' },
    { marker: 'vbot.navigation', view: '' },
  ])('rejects the foreign history state %j', (value) => {
    expect(isNavigationHistoryState(value)).toBe(false);
  });
});

describe('sameNavigationSelection()', () => {
  const base = { agentId: 'alpha', projectId: 'vbot', projectAgentId: '' };

  it.each([
    ['two empty selections', null, undefined, true],
    ['an empty and a set selection', null, base, false],
    ['a set and an empty selection', base, null, false],
    ['equal fields', base, { ...base }, true],
    ['another Agent', base, { ...base, agentId: 'beta' }, false],
    ['another Project', base, { ...base, projectId: '' }, false],
    [
      'a member instead of an identity Agent',
      base,
      { ...base, projectAgentId: 'builder' },
      false,
    ],
    [
      'nothing remembered instead of an identity Agent',
      base,
      { ...base, projectAgentId: null },
      false,
    ],
    [
      'missing fields and their empty forms',
      { agentId: 'alpha' },
      { agentId: 'alpha', projectId: '', projectAgentId: null },
      true,
    ],
  ])('compares %s', (_label, left, right, same) => {
    expect(sameNavigationSelection(left, right)).toBe(same);
  });
});

describe('sameSessionOverride()', () => {
  const base = { agentId: 'alpha', sessionId: 's1', subAgent: true };

  it.each([
    ['two empty overrides', null, undefined, true],
    ['an empty and a set override', null, base, false],
    ['a set and an empty override', base, null, false],
    ['equal fields', base, { ...base }, true],
    ['another Agent', base, { ...base, agentId: 'beta' }, false],
    ['another Session', base, { ...base, sessionId: 's2' }, false],
    ['another sub-agent flag', base, { ...base, subAgent: false }, false],
    [
      'a missing and a false sub-agent flag',
      { agentId: 'alpha', sessionId: 's1' },
      { agentId: 'alpha', sessionId: 's1', subAgent: false },
      true,
    ],
  ])('compares %s', (_label, left, right, same) => {
    expect(sameSessionOverride(left, right)).toBe(same);
  });
});

describe('location hash', () => {
  it.each([
    ['#settings', 'settings'],
    ['#/logs', 'logs'],
    ['#unknown', ''],
    ['', ''],
    [null, ''],
  ])('resolves %j to the known view %j', (hash, viewId) => {
    expect(viewIdFromLocationHash(hash, ['chat', 'settings', 'logs'])).toBe(
      viewId,
    );
  });

  it('names a view by its hash', () => {
    expect(locationHashForView('chat')).toBe('#chat');
  });
});
