import { describe, expect, it } from 'vitest';

import {
  PERMISSION_PRESET_INHERIT,
  applyPermissionPreset,
  isPermissionDeniedResult,
  parsePastedPermissionRules,
  permissionPresetChoice,
  permissionRulesComplete,
  withPastedPermissionRules,
  withPermissionBase,
} from '../permissionRules.js';

// The server's presets (`permissions.validate` `presets`), each the change it
// makes: null unsets a part.
const FULL_ACCESS = {
  read: 'anywhere',
  write: 'anywhere',
  commands: 'any',
  web: 'any',
  allow: [],
  deny: [],
};
const PRESETS = {
  full_access: FULL_ACCESS,
  work_dir: {
    read: 'work_area',
    write: 'work_area',
    commands: null,
    web: null,
  },
  work_dir_read_only: {
    read: 'work_area',
    write: 'scratch',
    commands: null,
    web: null,
  },
};
// The server's `bases.write.narrowed_by_read`.
const BASES = {
  write: { narrowed_by_read: { work_area: { anywhere: 'work_area' } } },
};

describe('Permission Rules editing', () => {
  it('applies a preset over the bases and keeps the own rules', () => {
    const own = { commands: 'listed', allow: ['Shell(git *)'], deny: ['x'] };

    expect(applyPermissionPreset(own, PRESETS.work_dir_read_only)).toEqual({
      allow: ['Shell(git *)'],
      deny: ['x'],
      read: 'work_area',
      write: 'scratch',
    });
    // Full access defines every part: it replaces the own rules.
    expect(applyPermissionPreset(own, FULL_ACCESS)).toEqual(FULL_ACCESS);
  });

  it.each([
    ['agent', null, 'full_access', PERMISSION_PRESET_INHERIT],
    ['agent', FULL_ACCESS, 'full_access', 'full_access'],
    // Own rules that restrict nothing yet are no choice of Full access.
    ['agent', { read: 'anywhere' }, 'full_access', 'custom'],
    ['override', { read: 'work_area' }, 'work_dir', 'work_dir'],
    ['agent', { commands: 'listed' }, 'custom', 'custom'],
    // The shared default inherits nothing: no rules is Full access.
    ['default', null, 'full_access', 'full_access'],
    ['default', {}, 'full_access', 'full_access'],
    [
      'default',
      { read: 'work_area', write: 'scratch' },
      'work_dir_read_only',
      'work_dir_read_only',
    ],
  ])(
    'shows the preset choice of a %s level with %j',
    (level, rules, preset, choice) => {
      expect(
        permissionPresetChoice(rules, { level, preset, presets: PRESETS }),
      ).toBe(choice);
    },
  );

  // An Agent can change only files it can read; the server says how.
  it('keeps write within read', () => {
    expect(
      withPermissionBase({ write: 'anywhere' }, 'read', 'work_area', BASES),
    ).toEqual({ write: 'work_area', read: 'work_area' });
    expect(
      withPermissionBase({ read: 'work_area' }, 'write', 'scratch', BASES),
    ).toEqual({ read: 'work_area', write: 'scratch' });
  });

  it('starts own rules when a base is set and removes a base to inherit', () => {
    expect(withPermissionBase(null, 'web', 'listed')).toEqual({
      web: 'listed',
    });
    expect(withPermissionBase({ web: 'listed' }, 'web', null)).toEqual({});
  });

  it.each([
    [
      'lines with list markers',
      '- Read(docs/**)\n\n  "Bash(npm test)",\n',
      'deny',
      { allow: [], deny: ['Read(docs/**)', 'Bash(npm test)'] },
    ],
    [
      'Claude Code settings',
      JSON.stringify({
        permissions: { allow: ['Bash(git status)'], deny: ['Read(.env)'] },
      }),
      'allow',
      { allow: ['Bash(git status)'], deny: ['Read(.env)'] },
    ],
  ])('reads pasted %s', (_label, text, effect, pasted) => {
    expect(parsePastedPermissionRules(text, effect)).toEqual(pasted);
  });

  it('adds pasted rules to the inherited list without repeating any', () => {
    expect(
      withPastedPermissionRules(
        { read: 'work_area' },
        { deny: ['Read(.env)'] },
        { allow: [], deny: ['Read(.env)', 'Shell(rm *)'] },
      ),
    ).toEqual({ read: 'work_area', deny: ['Read(.env)', 'Shell(rm *)'] });
  });

  it('waits for a pattern in every own rule', () => {
    expect(permissionRulesComplete(null)).toBe(true);
    expect(permissionRulesComplete({ allow: ['Read(docs)'] })).toBe(true);
    expect(permissionRulesComplete({ deny: ['Read( )'] })).toBe(false);
  });

  it.each([
    [{ ok: false, error: { code: 'permission_denied' } }, true],
    ['{"ok":false,"error":{"code":"permission_denied"}}', true],
    [{ ok: false, error: { code: 'not_found' } }, false],
    ['not json', false],
  ])('recognizes a refusal by the Permission Rules: %j', (result, refused) => {
    expect(isPermissionDeniedResult(result)).toBe(refused);
  });
});
