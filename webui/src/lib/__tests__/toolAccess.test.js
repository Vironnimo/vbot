import { describe, expect, it } from 'vitest';

import {
  changeToolAccessMode,
  groupToolCatalog,
  normalizeToolAccess,
  normalizeToolLoading,
  resetAlwaysLoadedTools,
  setAnalyzeImageAlwaysAvailable,
  setToolAccessPreference,
  setToolAlwaysLoaded,
  setToolFamilyPreference,
  setToolAccessState,
  setToolFamilyState,
  setToolsOnDemand,
  toolAccessIncludes,
  toolAccessPreferenceEnabled,
  toolAccessState,
  toolIsAlwaysLoaded,
  toolLoadingUsesDefaultSet,
} from '../toolAccess.js';

const catalog = [
  { name: 'read', family: 'files', activation: 'configurable' },
  { name: 'write', family: 'files', activation: 'configurable' },
  { name: 'session_search', family: 'sessions', activation: 'configurable' },
  {
    name: 'session_read',
    family: 'sessions',
    activation: 'follows',
    activation_source: 'session_search',
  },
  { name: 'memory', family: null, activation: 'memory_mode' },
  { name: 'status', family: null, activation: 'configurable' },
];

describe('Image analysis availability', () => {
  const image = { name: 'analyze_image', activation: 'configurable' };
  const tools = [...catalog, image];

  it.each(['all', 'selected'])(
    'round trips the explicit grant in %s mode',
    (mode) => {
      const initial = normalizeToolAccess({
        mode,
        allowed: ['analyze_image'],
        granted: ['computer'],
      });
      const enabled = setAnalyzeImageAlwaysAvailable(initial, true);
      expect(enabled.granted).toEqual(['computer', 'analyze_image']);
      expect(setAnalyzeImageAlwaysAvailable(enabled, false)).toEqual(initial);
      expect(initial.granted).toEqual(['computer']);
      expect(changeToolAccessMode(enabled, 'none', tools).granted).toEqual(
        enabled.granted,
      );
    },
  );

  it.each([
    { mode: 'none' },
    { mode: 'selected', allowed: [] },
    { mode: 'all', denied: ['analyze_image'] },
  ])('does not grant an unavailable Tool: %j', (policy) => {
    expect(setAnalyzeImageAlwaysAvailable(policy, true)).toEqual(policy);
  });

  it('never grants through ordinary Tool, family, or All selection', () => {
    const policy = { mode: 'selected', allowed: [] };
    expect(
      setToolAccessPreference(policy, image, true, tools).granted,
    ).toBeUndefined();
    expect(
      setToolFamilyPreference(policy, [image], true, tools).granted,
    ).toBeUndefined();
    expect(changeToolAccessMode(policy, 'all', tools).granted).toBeUndefined();
  });

  it('removes the special grant when the Tool is turned off', () => {
    const policy = setAnalyzeImageAlwaysAvailable({ mode: 'all' }, true);
    const off = setToolAccessPreference(policy, image, false, tools);
    expect(off).toEqual({ mode: 'all', denied: ['analyze_image'] });
  });
});

describe('Tool Access Policy UI helpers', () => {
  it('defaults missing policy data to explicit all mode', () => {
    expect(normalizeToolAccess()).toEqual({ mode: 'all' });
  });

  it('keeps denials across modes and prevents allowed/denied overlap', () => {
    expect(
      normalizeToolAccess({
        mode: 'selected',
        allowed: ['read', 'write'],
        denied: ['write', 'memory'],
      }),
    ).toEqual({
      mode: 'selected',
      allowed: ['read'],
      denied: ['write', 'memory'],
    });
  });

  it('materializes current configurable Tools when all becomes selected', () => {
    expect(
      changeToolAccessMode(
        { mode: 'all', denied: ['write'] },
        'selected',
        catalog,
      ),
    ).toEqual({
      mode: 'selected',
      allowed: ['read', 'session_search', 'status'],
      denied: ['write'],
    });
  });

  it('limits a Project Agent selection to the Project Tool Whitelist ceiling', () => {
    expect(
      changeToolAccessMode({ mode: 'all' }, 'selected', catalog, ['read']),
    ).toEqual({
      mode: 'selected',
      allowed: ['read'],
    });
  });

  it('turns enabling one Tool from none into an exact selected policy', () => {
    expect(
      setToolAccessState({ mode: 'none' }, 'read', 'enabled', catalog),
    ).toEqual({
      mode: 'selected',
      allowed: ['read'],
    });
  });

  it('enables only the requested automatic Tool after deselecting everything', () => {
    const next = setToolAccessPreference(
      { mode: 'none', granted: ['computer'] },
      catalog[4],
      true,
      catalog,
    );
    expect(next).toEqual({
      mode: 'selected',
      allowed: [],
      denied: ['session_read'],
    });
    expect(toolAccessPreferenceEnabled(next, catalog[4])).toBe(true);
    expect(toolAccessPreferenceEnabled(next, catalog[3])).toBe(false);
    expect(toolAccessPreferenceEnabled(next, catalog[0])).toBe(false);
  });

  it('keeps no access when enabling a Tool outside the Project ceiling', () => {
    expect(
      setToolAccessPreference({ mode: 'none' }, catalog[1], true, catalog, [
        'read',
      ]),
    ).toEqual({ mode: 'none' });
  });

  it('allows automatic Tools to be blocked and restored but not explicitly enabled', () => {
    const blocked = setToolAccessState(
      { mode: 'selected', allowed: ['session_search'] },
      'session_read',
      'denied',
      catalog,
    );
    expect(blocked).toEqual({
      mode: 'selected',
      allowed: ['session_search'],
      denied: ['session_read'],
    });
    expect(toolAccessState(blocked, catalog[3])).toBe('denied');
    expect(
      setToolAccessState(blocked, 'session_read', 'enabled', catalog),
    ).toEqual({ mode: 'selected', allowed: ['session_search'] });
  });

  it('shows a following Tool as inactive until its source is included', () => {
    const policy = { mode: 'selected', allowed: ['read'] };
    expect(toolAccessState(policy, catalog[3], catalog)).toBe('inactive');
    expect(
      toolAccessState(
        { mode: 'selected', allowed: ['session_search'] },
        catalog[3],
        catalog,
      ),
    ).toBe('automatic');
  });

  it('shows memory as inactive when Memory Prompt Mode is off', () => {
    expect(
      toolAccessState({ mode: 'all' }, catalog[4], catalog, {
        memoryPromptMode: 'off',
      }),
    ).toBe('inactive');
    expect(
      toolAccessState({ mode: 'all' }, catalog[4], catalog, {
        memoryPromptMode: 'agent_user',
      }),
    ).toBe('automatic');
  });

  it('applies family block and reset actions to every current member', () => {
    const blocked = setToolFamilyState(
      { mode: 'all' },
      catalog.slice(0, 2),
      'denied',
      catalog,
    );
    expect(blocked).toEqual({ mode: 'all', denied: ['read', 'write'] });
    expect(
      setToolFamilyState(blocked, catalog.slice(0, 2), 'default', catalog),
    ).toEqual({ mode: 'all' });
  });

  it('maps one compact off switch to the policy semantics of the active mode', () => {
    expect(
      setToolAccessPreference({ mode: 'all' }, catalog[0], false, catalog),
    ).toEqual({ mode: 'all', denied: ['read'] });
    expect(
      setToolAccessPreference(
        { mode: 'selected', allowed: ['read', 'write'] },
        catalog[0],
        false,
        catalog,
      ),
    ).toEqual({ mode: 'selected', allowed: ['write'] });
  });

  it('shows automatic Tools as permitted independently of their current gate', () => {
    expect(
      toolAccessPreferenceEnabled(
        { mode: 'selected', allowed: [] },
        catalog[3],
      ),
    ).toBe(true);
    const denied = setToolAccessPreference(
      { mode: 'selected', allowed: [] },
      catalog[3],
      false,
      catalog,
    );
    expect(denied).toEqual({
      mode: 'selected',
      allowed: [],
      denied: ['session_read'],
    });
    expect(toolAccessPreferenceEnabled(denied, catalog[3])).toBe(false);
    expect(setToolAccessPreference(denied, catalog[3], true, catalog)).toEqual({
      mode: 'selected',
      allowed: [],
    });
  });

  it('groups actual families while keeping unrelated Tools in one individual section', () => {
    expect(groupToolCatalog(catalog)).toEqual([
      { id: 'files', family: true, members: catalog.slice(0, 2) },
      {
        id: 'sessions',
        family: true,
        members: [catalog[3], catalog[2]],
      },
      {
        id: null,
        family: false,
        members: [catalog[4], catalog[5]],
      },
    ]);
  });

  it('keeps a one-member declared family in Individual Tools', () => {
    const extensionTool = {
      name: 'weather_today',
      family: 'extension:weather:forecast',
      family_label: 'Weather Forecast',
      activation: 'configurable',
    };

    expect(groupToolCatalog([...catalog, extensionTool])).toEqual([
      { id: 'files', family: true, members: catalog.slice(0, 2) },
      {
        id: 'sessions',
        family: true,
        members: [catalog[3], catalog[2]],
      },
      {
        id: null,
        family: false,
        members: [catalog[4], catalog[5], extensionTool],
      },
    ]);
  });

  it('computes direct Tool inclusion without confusing it with current readiness', () => {
    expect(toolAccessIncludes({ mode: 'all' }, 'subagent')).toBe(true);
    expect(
      toolAccessIncludes({ mode: 'all', denied: ['subagent'] }, 'subagent'),
    ).toBe(false);
    expect(
      toolAccessIncludes(
        { mode: 'selected', allowed: ['subagent'] },
        'subagent',
      ),
    ).toBe(true);
    expect(toolAccessIncludes({ mode: 'none' }, 'subagent')).toBe(false);
  });
});

describe('explicit Tool opt-in', () => {
  const computer = {
    name: 'computer',
    activation: 'configurable',
    requires_opt_in: true,
  };
  const allTools = [...catalog, computer];
  it('never grants by choosing All or materializing Choose', () => {
    const all = changeToolAccessMode(
      { mode: 'selected', allowed: [] },
      'all',
      allTools,
    );
    expect(toolAccessPreferenceEnabled(all, computer)).toBe(false);
    expect(
      changeToolAccessMode(all, 'selected', allTools).allowed,
    ).not.toContain('computer');
    expect(toolAccessState(all, computer)).toBe('off');
  });
  it('grants and revokes explicitly without leaving All mode', () => {
    const granted = setToolAccessPreference(
      { mode: 'all' },
      computer,
      true,
      allTools,
    );
    expect(granted).toEqual({ mode: 'all', granted: ['computer'] });
    expect(toolAccessPreferenceEnabled(granted, computer)).toBe(true);
    const revoked = setToolAccessPreference(granted, computer, false, allTools);
    expect(revoked).toEqual({ mode: 'all', denied: ['computer'] });
    expect(toolAccessPreferenceEnabled(revoked, computer)).toBe(false);
  });
  it('preserves explicit grants across modes but None still disables them', () => {
    const granted = { mode: 'all', granted: ['computer'] };
    const none = changeToolAccessMode(granted, 'none', allTools);
    expect(none.granted).toEqual(['computer']);
    expect(toolAccessPreferenceEnabled(none, computer)).toBe(false);
    expect(
      toolAccessPreferenceEnabled(
        changeToolAccessMode(none, 'all', allTools),
        computer,
      ),
    ).toBe(true);
  });
  it('requires selection and grant within the Project ceiling', () => {
    expect(
      toolAccessPreferenceEnabled(
        { mode: 'selected', allowed: ['computer'] },
        computer,
      ),
    ).toBe(false);
    const policy = setToolAccessPreference(
      { mode: 'selected', allowed: [] },
      computer,
      true,
      allTools,
      ['computer'],
    );
    expect(policy).toEqual({
      mode: 'selected',
      allowed: ['computer'],
      granted: ['computer'],
    });
    expect(
      setToolAccessPreference({ mode: 'all' }, computer, true, allTools, [
        'read',
      ]),
    ).toEqual({ mode: 'all' });
  });
  it('keeps companions inactive until their parent is granted', () => {
    const follower = {
      name: 'computer_read',
      activation: 'follows',
      activation_source: 'computer',
    };
    expect(
      toolAccessState({ mode: 'all' }, follower, [computer, follower]),
    ).toBe('inactive');
    expect(
      toolAccessState({ mode: 'all', granted: ['computer'] }, follower, [
        computer,
        follower,
      ]),
    ).toBe('automatic');
  });
});

describe('On-demand Tools', () => {
  const loadingCatalog = [
    { name: 'read', loaded_by_default: true },
    { name: 'edit', loaded_by_default: true },
    { name: 'write', loaded_by_default: true },
    { name: 'bash', loaded_by_default: true },
    { name: 'web_fetch', loaded_by_default: false },
    { name: 'calendar' },
  ];
  const tool = (name) => ({ name });
  const alwaysLoaded = (value) =>
    loadingCatalog
      .filter((entry) => toolIsAlwaysLoaded(value, entry, loadingCatalog))
      .map((entry) => entry.name);

  it.each([
    [null, null],
    ['on', null],
    [{ on_demand: false }, null],
    [{ on_demand: true }, { on_demand: true }],
    [
      { on_demand: false, always_loaded: [' read ', 'read', '', '*', 'mcp_x'] },
      { on_demand: false, always_loaded: ['read', 'mcp_x'] },
    ],
  ])('normalizes %j to %j', (value, expected) => {
    expect(normalizeToolLoading(value)).toEqual(expected);
  });

  it('reads the catalog default set until the first change writes the list', () => {
    const value = { on_demand: true };
    expect(toolLoadingUsesDefaultSet(value)).toBe(true);
    expect(alwaysLoaded(value)).toEqual(['read', 'edit', 'write', 'bash']);

    const pinned = setToolAlwaysLoaded(
      value,
      tool('web_fetch'),
      true,
      loadingCatalog,
    );
    expect(pinned).toEqual({
      on_demand: true,
      always_loaded: ['read', 'edit', 'write', 'bash', 'web_fetch'],
    });
    expect(
      setToolAlwaysLoaded(value, tool('bash'), false, loadingCatalog),
    ).toEqual({ on_demand: true, always_loaded: ['read', 'edit', 'write'] });
    // An explicit empty list is a choice of its own, not the default set.
    expect(alwaysLoaded({ on_demand: true, always_loaded: [] })).toEqual([]);
    expect(resetAlwaysLoadedTools(pinned)).toEqual({ on_demand: true });
  });

  it('marks and unmarks the file edit Tools the catalog lists as one unit', () => {
    const value = { on_demand: true, always_loaded: ['bash', 'mcp_x'] };
    // apply_patch is not in this catalog, so only the listed ones are added.
    expect(
      setToolAlwaysLoaded(value, tool('edit'), true, loadingCatalog),
    ).toEqual({
      on_demand: true,
      always_loaded: ['bash', 'mcp_x', 'edit', 'write'],
    });
    // One stored member makes all of them always loaded; unmarking removes
    // all three and keeps names the catalog does not know.
    const partial = {
      on_demand: true,
      always_loaded: ['apply_patch', 'mcp_x'],
    };
    expect(alwaysLoaded(partial)).toEqual(['edit', 'write']);
    expect(
      setToolAlwaysLoaded(partial, tool('write'), false, loadingCatalog),
    ).toEqual({ on_demand: true, always_loaded: ['mcp_x'] });
  });

  it('keeps an explicit list while the switch is off', () => {
    const custom = { on_demand: true, always_loaded: ['read'] };
    const off = setToolsOnDemand(custom, false);
    expect(off).toEqual({ on_demand: false, always_loaded: ['read'] });
    expect(setToolsOnDemand(off, true)).toEqual(custom);
    expect(setToolsOnDemand({ on_demand: true }, false)).toBeNull();
    expect(setToolsOnDemand(null, true)).toEqual({ on_demand: true });
  });
});
