// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../lib/i18n.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';

const api = vi.hoisted(() => ({
  listCredentials: vi.fn(),
  setCredential: vi.fn(),
  unsetCredential: vi.fn(),
}));
vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
vi.mock('$lib/api.js', () => api);
const { default: Panel } =
  await import('../settings/SettingsCredentialsPanel.svelte');

const STORED = {
  name: 'SKILL_TOKEN',
  configured: true,
  source: 'data_dir',
  damaged: false,
};
const ENVIRONMENT = {
  name: 'TELEGRAM_BOT_TOKEN',
  configured: true,
  source: 'process_environment',
  damaged: false,
};
const EMPTY_ENVIRONMENT = {
  name: 'SLACK_APP_TOKEN',
  configured: false,
  source: 'process_environment',
  damaged: false,
};
const DAMAGED = {
  name: 'MCP_CLIENT_SECRET',
  configured: false,
  source: null,
  damaged: true,
};
const listed = (credentials, state = 'available', reason = null) => ({
  state,
  reason,
  credentials,
});

let component;
let onToast;
let props;

beforeEach(() => {
  document.body.innerHTML = '';
  init('en');
  vi.useFakeTimers();
  for (const fn of Object.values(api)) fn.mockReset();
  api.listCredentials.mockResolvedValue(
    listed([STORED, ENVIRONMENT, EMPTY_ENVIRONMENT, DAMAGED]),
  );
  onToast = vi.fn();
});

afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  vi.useRealTimers();
  document.body.innerHTML = '';
});

async function settle() {
  await vi.advanceTimersByTimeAsync(0);
  flushSync();
}

async function render() {
  props = reactiveProps({ onToast, credentialsRefreshToken: 0 });
  component = mount(Panel, { target: document.body, props });
  await settle();
}

// Inside an open dialog, its buttons win over the page's.
function button(label) {
  const scope = document.querySelector('[role="dialog"]') ?? document;
  const match = [...scope.querySelectorAll('button')].find(
    (item) =>
      item.getAttribute('aria-label') === label ||
      item.textContent.trim() === label,
  );
  expect(match, `button ${label}`).toBeTruthy();
  return match;
}

async function click(label) {
  button(label).click();
  await settle();
}

async function type(id, value) {
  const input = document.getElementById(id);
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  await settle();
}

async function save() {
  document
    .getElementById('settings-credential-form')
    .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  await settle();
}

const row = (name) => document.querySelector(`[data-credential="${name}"]`);
const rowButtons = (name) =>
  [...row(name).querySelectorAll('button')].map((item) =>
    item.getAttribute('aria-label'),
  );
const fieldError = (id) =>
  document.getElementById(`${id}-error`)?.textContent.trim() ?? '';

describe('Settings credentials panel', () => {
  it('lists each saved name with its source and state, never offering to change an environment value', async () => {
    await render();

    expect(
      [...document.querySelectorAll('[role="listitem"]')].map((item) => [
        item.dataset.credential,
        item.dataset.credentialSource,
        item.dataset.credentialConfigured,
        item.dataset.credentialDamaged,
      ]),
    ).toEqual([
      ['SKILL_TOKEN', 'data_dir', 'true', 'false'],
      ['TELEGRAM_BOT_TOKEN', 'process_environment', 'true', 'false'],
      ['SLACK_APP_TOKEN', 'process_environment', 'false', 'false'],
      ['MCP_CLIENT_SECRET', 'none', 'false', 'true'],
    ]);
    expect(document.body.textContent).toContain(
      t('settings.credentials.count', { count: 4 }),
    );
    expect(row('SKILL_TOKEN').textContent).toContain(
      t('settings.credentials.stored'),
    );
    expect(row('TELEGRAM_BOT_TOKEN').textContent).toContain(
      t('settings.credentials.environment'),
    );
    expect(row('SLACK_APP_TOKEN').textContent).toContain(
      t('settings.credentials.chipMissing'),
    );
    expect(row('MCP_CLIENT_SECRET').textContent).toContain(
      t('settings.credentials.chipDamaged'),
    );
    expect(rowButtons('SKILL_TOKEN')).toEqual([
      t('settings.credentials.replaceAria', { name: 'SKILL_TOKEN' }),
      t('settings.credentials.removeAria', { name: 'SKILL_TOKEN' }),
    ]);
    expect(rowButtons('MCP_CLIENT_SECRET')).toHaveLength(2);
    expect(rowButtons('TELEGRAM_BOT_TOKEN')).toEqual([]);
    expect(rowButtons('SLACK_APP_TOKEN')).toEqual([]);
  });

  it('adds a credential once its name and value are valid and keeps no value after saving', async () => {
    api.setCredential.mockResolvedValue({
      name: 'NEW_TOKEN',
      configured: true,
      source: 'data_dir',
      changed: true,
    });
    await render();

    await click(t('settings.credentials.add'));
    expect(document.getElementById('settings-credential-value').type).toBe(
      'password',
    );
    await save();
    expect(fieldError('settings-credential-name')).toBe(
      t('settings.credentials.nameRequired'),
    );
    expect(fieldError('settings-credential-value')).toBe(
      t('settings.credentials.valueRequired'),
    );
    for (const [name, problem] of [
      ['1_TOKEN', 'settings.credentials.nameInvalid'],
      ['MY-TOKEN', 'settings.credentials.nameInvalid'],
      ['A'.repeat(201), 'settings.credentials.nameTooLong'],
      ['SKILL_TOKEN', 'settings.credentials.nameTaken'],
    ]) {
      await type('settings-credential-name', name);
      expect(fieldError('settings-credential-name')).toBe(
        t(problem, { max: 200 }),
      );
    }
    await type('settings-credential-value', '  secret-value-1 ');
    await save();
    expect(api.setCredential).not.toHaveBeenCalled();

    await type('settings-credential-name', ' NEW_TOKEN ');
    await save();

    expect(api.setCredential).toHaveBeenCalledExactlyOnceWith(
      'NEW_TOKEN',
      'secret-value-1',
    );
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(onToast).toHaveBeenCalledWith({
      title: t('settings.credentials.saved', { name: 'NEW_TOKEN' }),
      variant: 'success',
    });
    expect(api.listCredentials).toHaveBeenCalledTimes(2);
    await click(t('settings.credentials.add'));
    expect(document.getElementById('settings-credential-value').value).toBe('');
    expect(document.getElementById('settings-credential-name').value).toBe('');
    expect(document.body.innerHTML).not.toContain('secret-value-1');
  });

  it('replaces a value and warns when the server environment overrides it', async () => {
    api.setCredential.mockResolvedValue({
      name: 'SKILL_TOKEN',
      configured: true,
      source: 'process_environment',
      changed: true,
    });
    await render();

    await click(t('settings.credentials.replaceAria', { name: 'SKILL_TOKEN' }));
    expect(document.getElementById('settings-credential-name')).toBeNull();
    await type('settings-credential-value', 'rotated');
    await save();

    expect(api.setCredential).toHaveBeenCalledExactlyOnceWith(
      'SKILL_TOKEN',
      'rotated',
    );
    expect(onToast).toHaveBeenCalledWith({
      title: t('settings.credentials.savedShadowed', { name: 'SKILL_TOKEN' }),
      variant: 'warn',
    });
  });

  it('removes a credential after confirmation and keeps a failure in the dialog', async () => {
    api.unsetCredential
      .mockRejectedValueOnce(new Error('store busy'))
      .mockResolvedValueOnce({
        name: 'SKILL_TOKEN',
        configured: false,
        source: null,
        removed: true,
      });
    await render();

    await click(t('settings.credentials.removeAria', { name: 'SKILL_TOKEN' }));
    await click(t('common.remove'));
    expect(document.querySelector('[role="dialog"]').textContent).toContain(
      t('settings.credentials.removeError'),
    );
    expect(onToast).not.toHaveBeenCalled();

    await click(t('common.remove'));
    expect(api.unsetCredential).toHaveBeenCalledTimes(2);
    expect(api.unsetCredential).toHaveBeenLastCalledWith('SKILL_TOKEN');
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(onToast).toHaveBeenCalledWith({
      title: t('settings.credentials.removed', { name: 'SKILL_TOKEN' }),
      variant: 'success',
    });
    expect(api.listCredentials).toHaveBeenCalledTimes(2);
  });

  it('shows why the Secret Store is unavailable and offers no change', async () => {
    api.listCredentials.mockResolvedValue(
      listed(
        [{ ...STORED, configured: false, source: null }],
        'unavailable',
        'the key does not open this store',
      ),
    );
    await render();

    expect(
      document.querySelector('[data-credential-store="unavailable"]')
        .textContent,
    ).toContain(
      t('settings.credentials.unavailableReason', {
        reason: 'the key does not open this store',
      }),
    );
    expect(button(t('settings.credentials.add')).disabled).toBe(true);
    for (const item of row('SKILL_TOKEN').querySelectorAll('button')) {
      expect(item.disabled).toBe(true);
    }
  });

  it('reloads when saved credentials change and ignores an older reply', async () => {
    await render();
    let answerOlder;
    api.listCredentials
      .mockReturnValueOnce(
        new Promise((resolve) => {
          answerOlder = resolve;
        }),
      )
      .mockResolvedValueOnce(listed([ENVIRONMENT]));

    props.credentialsRefreshToken = 1;
    flushSync();
    props.credentialsRefreshToken = 2;
    await settle();
    answerOlder(listed([STORED]));
    await settle();

    expect(api.listCredentials).toHaveBeenCalledTimes(3);
    expect(
      [...document.querySelectorAll('[role="listitem"]')].map(
        (item) => item.dataset.credential,
      ),
    ).toEqual(['TELEGRAM_BOT_TOKEN']);
  });
});
