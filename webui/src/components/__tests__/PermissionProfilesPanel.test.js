// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../lib/i18n.js';

vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
const list = vi.fn();
const save = vi.fn();
const remove = vi.fn();
const validate = vi.fn();
vi.mock('$lib/api.js', () => ({
  listPermissionProfiles: (...args) => list(...args),
  savePermissionProfile: (...args) => save(...args),
  deletePermissionProfile: (...args) => remove(...args),
  validatePermissionRules: (...args) => validate(...args),
}));
const { default: PermissionProfilesPanel } =
  await import('../settings/PermissionProfilesPanel.svelte');
const { refreshPermissionProfiles } =
  await import('../../lib/permissionProfiles.js');

const restricted = {
  read: 'anywhere',
  write: 'work_area',
  commands: 'any',
  web: 'listed',
  allow: [],
  deny: [],
};

function button(label) {
  const found = [...document.querySelectorAll('button')].find(
    (node) => node.textContent.trim() === label,
  );
  expect(found).toBeTruthy();
  return found;
}
function enter(field, value) {
  field.value = value;
  field.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}
async function settle() {
  flushSync();
  await vi.runAllTimersAsync();
  flushSync();
}

describe('PermissionProfilesPanel', () => {
  let component;
  let entries;
  const catalog = () => ({ profiles: [...entries.values()] });

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    vi.useFakeTimers();
    entries = new Map([
      ['restricted', { name: 'restricted', builtin: true, rules: restricted }],
      [
        'full_access',
        {
          name: 'full_access',
          builtin: true,
          rules: { ...restricted, write: 'anywhere', web: 'any' },
        },
      ],
    ]);
    list.mockReset().mockImplementation(async () => catalog());
    save.mockReset().mockImplementation(async (name, rules) => {
      entries.set(name, { name, builtin: false, rules });
      return catalog();
    });
    remove.mockReset().mockImplementation(async (name) => {
      entries.delete(name);
      return catalog();
    });
    validate
      .mockReset()
      .mockResolvedValue({ valid: true, bases: {}, errors: [] });
  });
  afterEach(async () => {
    if (component) await unmount(component);
    component = null;
    vi.useRealTimers();
  });

  it('creates a restricted profile, saves complete edits against their base and confirms removal', async () => {
    component = mount(PermissionProfilesPanel, { target: document.body });
    await settle();
    const builtinRows = [...document.querySelectorAll('.s-group .s-row')];
    expect(
      builtinRows.map((row) => row.querySelector('.s-row-label').textContent),
    ).toEqual([
      t('permissions.profile.restricted'),
      t('permissions.preset.full_access'),
    ]);
    expect(builtinRows.every((row) => !row.querySelector('button'))).toBe(true);
    button(t('permissions.profiles.add')).click();
    flushSync();
    enter(document.querySelector('form input'), 'Project work');
    button(t('common.create')).click();
    await settle();
    expect(save).toHaveBeenCalledWith('Project work', restricted, null);

    // An empty rule is an unfinished draft, including during autosave.
    document
      .querySelector('[data-permission-list="deny"]')
      .nextElementSibling.querySelector('button')
      .click();
    await settle();
    expect(save).toHaveBeenCalledTimes(1);
    enter(
      document.querySelector('[data-permission-list="deny"] input'),
      '~/private',
    );
    await settle();
    const edited = { ...restricted, deny: ['Read(~/private)'] };
    expect(save).toHaveBeenCalledWith('Project work', edited, restricted);

    button(t('permissions.profiles.remove')).click();
    flushSync();
    expect(remove).not.toHaveBeenCalled();
    remove.mockRejectedValueOnce(new Error('Still used by the test Agent'));
    button(t('common.delete')).click();
    await settle();
    expect(document.querySelector('[role="dialog"]').textContent).toContain(
      'Still used by the test Agent',
    );
    button(t('common.delete')).click();
    await settle();
    expect(remove).toHaveBeenCalledWith('Project work', edited);
    expect(document.querySelector('[data-permission-rules]')).toBeNull();
  });

  it('adopts changes to an open profile and retires its editor when another client deletes it', async () => {
    entries.set('Shared', {
      name: 'Shared',
      builtin: false,
      rules: restricted,
    });
    component = mount(PermissionProfilesPanel, { target: document.body });
    await settle();
    button(t('common.edit')).click();
    await settle();
    entries.set('Shared', {
      name: 'Shared',
      builtin: false,
      rules: { ...restricted, deny: ['Read(secret)'] },
    });
    await refreshPermissionProfiles();
    await settle();
    expect(
      document.querySelector('[data-permission-list="deny"] input').value,
    ).toBe('secret');
    entries.delete('Shared');
    await refreshPermissionProfiles();
    await settle();
    expect(document.querySelector('[data-permission-rules]')).toBeNull();
    expect(save).not.toHaveBeenCalled();
  });
});
