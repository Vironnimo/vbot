import { describe, expect, it } from 'vitest';

import {
  applyPermissionPreset,
  effectivePermissionBase,
  isPermissionDeniedResult,
  parsePastedPermissionRules,
  permissionRulesComplete,
  withPastedPermissionRules,
  withPermissionBase,
} from '../permissionRules.js';

describe('Permission Rules editing', () => {
  it('applies a preset over the bases and keeps the own rules', () => {
    const own = { commands: 'listed', allow: ['Shell(git *)'], deny: ['x'] };

    expect(
      applyPermissionPreset(own, { read: 'work_area', write: 'scratch' }),
    ).toEqual({
      allow: ['Shell(git *)'],
      deny: ['x'],
      read: 'work_area',
      write: 'scratch',
    });
    // Full access has no definition: no own rules.
    expect(applyPermissionPreset(own, null)).toBeNull();
  });

  it.each([
    ['own', { read: 'work_area' }, { read: 'anywhere' }, 'work_area', 'own'],
    ['inherited', null, { read: 'work_area' }, 'work_area', 'inherited'],
    ['default', {}, null, 'anywhere', 'default'],
  ])('takes a base from the %s level', (_label, own, parent, value, source) => {
    expect(effectivePermissionBase(own, parent, 'read')).toEqual({
      value,
      source,
    });
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
