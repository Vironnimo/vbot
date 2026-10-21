// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';

const api = vi.hoisted(() => ({
  createAgentSecret: vi.fn(),
  deleteAgentSecret: vi.fn(),
  listAgentSecrets: vi.fn(),
  listSecretSourceEntries: vi.fn(),
  lockSecretSource: vi.fn(),
  revealAgentSecret: vi.fn(),
  unlockSecretSource: vi.fn(),
  updateAgentSecret: vi.fn(),
}));
vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
vi.mock('$lib/api.js', () => api);
const { default: Panel } =
  await import('../settings/SettingsAgentSecretsPanel.svelte');

const GITHUB = {
  id: 'sec-1',
  name: 'GITHUB',
  reference: '{{secret:GITHUB}}',
  kind: 'login',
  lifetime: 'permanent',
  description: 'Work account',
  agents: ['main'],
  sinks: ['terminal'],
  fields: [
    { name: 'username', concealed: false, value: 'octo' },
    { name: 'password', concealed: true },
  ],
  recent_uses: [],
  source: null,
};
const KEEPASS = {
  name: 'keepassxc',
  label: 'KeePassXC',
  state: 'locked',
  reason: null,
  capabilities: ['unlock', 'lock', 'browse'],
  unlock_fields: [
    { name: 'password', label: 'Password', concealed: true, required: true },
  ],
};
const overview = (secrets, sources = []) => ({
  secrets,
  sources,
  requests: [],
});

let component;
let onToast;
let props;

beforeEach(() => {
  document.body.innerHTML = '';
  init('en');
  vi.useFakeTimers();
  for (const fn of Object.values(api)) fn.mockReset();
  api.listAgentSecrets.mockResolvedValue(overview([GITHUB]));
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
  props = reactiveProps({
    onToast,
    agentSecretsRefreshToken: 0,
    agents: [{ id: 'main', name: 'Main' }],
  });
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

// The dialog control labelled `label` (a required marker aside).
async function type(label, value) {
  const labelElement = [
    ...document.querySelectorAll('[role="dialog"] label'),
  ].find((item) => item.textContent.replace('*', '').trim() === label);
  expect(labelElement, `field ${label}`).toBeTruthy();
  const input = document.getElementById(labelElement.htmlFor);
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  await settle();
}

async function submitDialog() {
  document
    .querySelector('[role="dialog"] form')
    .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  await settle();
}

const shownPassword = () =>
  document.querySelector(
    '[data-agent-secret="GITHUB"] [data-secret-field="password"] [data-revealed]',
  );

describe('Settings Agent Secrets panel', () => {
  it('shows a concealed value only between Reveal and Hide, asking the server each time', async () => {
    api.revealAgentSecret.mockResolvedValue({ value: 'hunter2-value' });
    await render();

    const row = document.querySelector('[data-agent-secret="GITHUB"]');
    expect(row.dataset.agentSecretLifetime).toBe('permanent');
    expect(row.querySelector('.s-row-desc').textContent).toBe(
      'Login · Permanent · Work account',
    );
    await click('Details of GITHUB');
    expect(
      row.querySelector('[data-secret-field="username"]').textContent,
    ).toContain('octo');
    expect(shownPassword().dataset.revealed).toBe('false');
    expect(row.textContent).not.toContain('hunter2-value');

    await click('Reveal password of GITHUB');
    expect(api.revealAgentSecret).toHaveBeenCalledWith('sec-1', 'password');
    expect(shownPassword().textContent.trim()).toBe('hunter2-value');

    await click('Hide password of GITHUB');
    expect(shownPassword().dataset.revealed).toBe('false');
    expect(row.textContent).not.toContain('hunter2-value');

    await click('Reveal password of GITHUB');
    expect(api.revealAgentSecret).toHaveBeenCalledTimes(2);
    props.agentSecretsRefreshToken = 1;
    await settle();
    expect(api.listAgentSecrets).toHaveBeenCalledTimes(2);
    expect(shownPassword().dataset.revealed).toBe('false');
    expect(document.body.textContent).not.toContain('hunter2-value');
  });

  it('adds a Secret with its value, Agents and sinks, then deletes it after confirmation', async () => {
    api.listAgentSecrets.mockResolvedValue(overview([]));
    api.createAgentSecret.mockResolvedValue({ secret: { name: 'API_KEY' } });
    await render();

    await click('Add secret');
    await type('Name', 'api key');
    await type('Value', 'tok-123');
    await click('Main');
    await click('Terminal input');
    await submitDialog();

    expect(api.createAgentSecret).toHaveBeenCalledWith({
      name: 'API_KEY',
      kind: 'token',
      values: { value: 'tok-123' },
      lifetime: 'permanent',
      agents: ['main'],
      sinks: ['environment', 'terminal', 'files'],
    });
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(onToast).toHaveBeenCalledWith({
      title: 'API_KEY added.',
      variant: 'success',
    });

    api.listAgentSecrets.mockResolvedValue(overview([GITHUB]));
    props.agentSecretsRefreshToken = 1;
    await settle();
    await click('Delete GITHUB');
    expect(api.deleteAgentSecret).not.toHaveBeenCalled();
    await click('Delete');
    expect(api.deleteAgentSecret).toHaveBeenCalledWith('sec-1');
  });

  it('unlocks a password manager and binds one of its entries as a Secret', async () => {
    api.listAgentSecrets.mockResolvedValue(overview([], [KEEPASS]));
    api.unlockSecretSource.mockResolvedValue({});
    api.listSecretSourceEntries.mockResolvedValue({
      source: 'keepassxc',
      containers: [],
      entries: [
        {
          reference: 'uuid-7',
          title: 'Mail login',
          kind: 'login',
          container: 'Work',
          fields: [
            { name: 'UserName', concealed: false, value: 'me@example.com' },
            { name: 'Password', concealed: true },
            { name: 'totp', concealed: true, optional: true },
            { name: 'notes', concealed: true, optional: true },
          ],
        },
      ],
    });
    api.createAgentSecret.mockResolvedValue({ secret: { name: 'MAIL_LOGIN' } });
    await render();

    const source = () =>
      document.querySelector('[data-secret-source="keepassxc"]');
    expect(source().dataset.secretSourceState).toBe('locked');
    await click('Unlock KeePassXC');
    await type('Password', 'master-pass');
    api.listAgentSecrets.mockResolvedValue(
      overview([], [{ ...KEEPASS, state: 'available' }]),
    );
    await submitDialog();
    expect(api.unlockSecretSource).toHaveBeenCalledWith('keepassxc', {
      password: 'master-pass',
    });
    expect(source().dataset.secretSourceState).toBe('available');

    await click('Browse KeePassXC');
    await click('Use Mail login as a Secret');
    await submitDialog();
    expect(api.createAgentSecret).toHaveBeenCalledWith({
      name: 'MAIL_LOGIN',
      kind: 'login',
      values: {},
      lifetime: 'permanent',
      agents: [],
      sinks: ['environment', 'terminal', 'typing'],
      source: 'keepassxc',
      source_reference: 'uuid-7',
      source_fields: { username: 'UserName', password: 'Password' },
      concealed: { username: false, password: true },
    });
  });
});
