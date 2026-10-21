// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const validateMock = vi.fn();
vi.mock('$lib/api.js', () => ({
  validatePermissionRules: (...args) => validateMock(...args),
}));

const { default: PermissionRulesEditor } =
  await import('../PermissionRulesEditor.svelte');

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
  work_dir: { read: 'work_area', write: 'work_area' },
  work_dir_read_only: { read: 'work_area', write: 'scratch' },
};

function response(fields) {
  return {
    valid: true,
    permission_rules: null,
    notes: [],
    errors: [],
    preset: 'full_access',
    presets: PRESETS,
    inherited_preset: 'full_access',
    inherited: null,
    effective: null,
    preview: null,
    ...fields,
  };
}

async function settle() {
  await vi.runAllTimersAsync();
  flushSync();
}

describe('PermissionRulesEditor', () => {
  let component = null;
  let changes = [];

  async function mountEditor(props) {
    component = mount(PermissionRulesEditor, {
      target: document.body,
      props: {
        agentId: 'coder',
        onChange: (next) => changes.push(next),
        ...props,
      },
    });
    flushSync();
    await settle();
  }

  function choose(trigger, label) {
    trigger.click();
    flushSync();
    const option = [
      ...document.querySelectorAll('.dropdown-primitive__option'),
    ].find((item) => item.textContent.trim() === label);
    expect(option).toBeTruthy();
    option.click();
    flushSync();
  }

  const trigger = (label) =>
    document.querySelector(`button[aria-label="${label}"]`);
  const triggerLabel = (label) =>
    trigger(label)
      .querySelector('.dropdown-primitive__trigger-label')
      .textContent.trim();

  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = '';
    init('en');
    changes = [];
    validateMock.mockReset();
  });

  afterEach(async () => {
    if (component) await unmount(component);
    component = null;
    vi.useRealTimers();
  });

  it('checks the rules for the Agent and applies a preset over its own rules', async () => {
    const own = { allow: ['Read(~/notes/**)'] };
    validateMock.mockResolvedValue(
      response({ permission_rules: own, preset: 'custom', effective: own }),
    );
    await mountEditor({ value: own });

    expect(validateMock).toHaveBeenCalledWith(own, {
      agentId: 'coder',
      newAgent: false,
    });
    expect(triggerLabel(t('permissions.preset'))).toBe(
      t('permissions.preset.custom'),
    );

    choose(trigger(t('permissions.preset')), t('permissions.preset.work_dir'));

    expect(changes.at(-1)).toEqual({ ...own, ...PRESETS.work_dir });
  });

  it('names what an Agent without own rules inherits', async () => {
    validateMock.mockResolvedValue(
      response({
        preset: 'work_dir_read_only',
        inherited_preset: 'work_dir_read_only',
        inherited: { read: 'work_area', write: 'scratch' },
      }),
    );
    await mountEditor({ value: null });

    expect(triggerLabel(t('permissions.preset'))).toBe(
      t('permissions.preset.useDefault', {
        preset: t('permissions.preset.work_dir_read_only'),
      }),
    );
    expect(triggerLabel(t('permissions.part.write'))).toBe(
      t('permissions.base.inherited', {
        value: t('permissions.base.write.scratch'),
      }),
    );
  });

  it('offers Full access over a restrictive default and says it overrides it', async () => {
    const inherited = { read: 'work_area', write: 'scratch' };
    const answer = (rules) =>
      response({
        permission_rules: rules,
        preset: rules ? 'full_access' : 'work_dir_read_only',
        inherited_preset: 'work_dir_read_only',
        inherited,
        effective: rules ? null : inherited,
      });
    validateMock.mockImplementation(async (rules) => answer(rules));
    await mountEditor({ value: null });

    choose(
      trigger(t('permissions.preset')),
      t('permissions.preset.full_access'),
    );
    // The explicit full set overrides every inherited part.
    expect(changes.at(-1)).toEqual(FULL_ACCESS);
    expect(document.body.textContent).not.toContain(
      t('permissions.preset.ignoresDefault'),
    );

    await unmount(component);
    await mountEditor({ value: FULL_ACCESS });

    expect(triggerLabel(t('permissions.preset'))).toBe(
      t('permissions.preset.full_access'),
    );
    expect(document.body.textContent).toContain(
      t('permissions.preset.ignoresDefault'),
    );
    expect(document.body.textContent).toContain(
      t('permissions.preview.fullAccess'),
    );
  });

  it('makes Full access of the shared default no rules at all', async () => {
    validateMock.mockResolvedValue(response({}));
    await mountEditor({ value: null, level: 'default', agentId: '' });

    expect(triggerLabel(t('permissions.preset'))).toBe(
      t('permissions.preset.full_access'),
    );
    choose(trigger(t('permissions.preset')), t('permissions.preset.work_dir'));
    expect(changes.at(-1)).toEqual(PRESETS.work_dir);
    choose(
      trigger(t('permissions.preset')),
      t('permissions.preset.full_access'),
    );
    // The default inherits nothing, so Full access stores no default.
    expect(changes.at(-1)).toBeNull();
  });

  // An Agent can change only files it can read.
  it('narrows write when read stays in the work area and says why', async () => {
    const own = { write: 'anywhere' };
    validateMock.mockResolvedValue(
      response({ permission_rules: own, preset: 'custom', effective: own }),
    );
    await mountEditor({ value: own });

    choose(
      trigger(t('permissions.part.read')),
      t('permissions.base.read.work_area'),
    );

    expect(changes.at(-1)).toEqual({ read: 'work_area', write: 'work_area' });
    expect(document.body.textContent).toContain(
      t('permissions.base.writeNarrowed'),
    );
  });

  it('shows the server problem of a rule next to it', async () => {
    const own = { deny: ['Foo(x)'] };
    const message = 'permission_rules.deny[0] "Foo(x)": unknown kind Foo';
    validateMock.mockResolvedValue(
      response({
        valid: false,
        preset: null,
        errors: [{ part: 'deny', index: 0, rule: 'Foo(x)', message }],
      }),
    );
    await mountEditor({ value: own });

    const pattern = document.querySelector(
      `input[aria-label="${t('permissions.rulePattern', { index: 1 })}"]`,
    );
    expect(pattern.getAttribute('aria-invalid')).toBe('true');
    expect(document.body.textContent).toContain(message);
  });
});
