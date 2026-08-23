// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../../lib/i18n.js';
import { rpcBackedApiMock } from '../../__tests__/apiMock.js';

const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => rpcBackedApiMock(rpcMock));

const { default: SkillsView } = await import('../SkillsView.svelte');

const INVENTORY = {
  skills: [
    {
      name: 'teach',
      description: 'Teach a topic.',
      origin: 'bundled',
      owner_id: null,
      shared: false,
      disabled: false,
      status: 'available',
      missing: [],
      optional_missing: [],
      warnings: [],
    },
    {
      name: 'deploy',
      description: 'Ship the app.',
      origin: 'agent',
      owner_id: 'main',
      shared: false,
      disabled: false,
      status: 'available',
      missing: [],
      optional_missing: [],
      warnings: [],
    },
    {
      name: 'notes',
      description: 'Take notes.',
      origin: 'agent',
      owner_id: 'main',
      shared: true,
      disabled: false,
      status: 'available',
      missing: [],
      optional_missing: [],
      warnings: [],
    },
    {
      name: 'broken',
      description: '',
      origin: 'global',
      owner_id: null,
      shared: false,
      disabled: true,
      status: 'disabled',
      missing: [],
      optional_missing: [],
      warnings: ['name mismatch'],
    },
  ],
  stale_shared: [],
};

function callCount(method) {
  return rpcMock.mock.calls.filter((call) => call[0] === method).length;
}

async function waitForCondition(check, attempts = 20) {
  for (let index = 0; index < attempts; index += 1) {
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
    if (check()) {
      return;
    }
  }
}

describe('SkillsView', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    rpcMock.mockReset();
    rpcMock.mockImplementation((method) => {
      if (method === 'skill.inventory') {
        return Promise.resolve(INVENTORY);
      }
      if (method === 'agent.list') {
        return Promise.resolve({ agents: [{ id: 'main', name: 'Main' }] });
      }
      return Promise.resolve({});
    });
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
  });

  async function mountView() {
    mountedComponent = mount(SkillsView, {
      target: document.body,
      props: { settings: {}, skillsRefreshToken: 0 },
    });
    flushSync();
    await waitForCondition(() => callCount('skill.inventory') >= 1);
  }

  it('renders every origin group in catalog order with truthful status chips', async () => {
    await mountView();

    const keys = [
      ...document.body.querySelectorAll('[data-testid^="skill-group-"]'),
    ].map((element) =>
      element.getAttribute('data-testid').replace('skill-group-', ''),
    );
    // Bundled and Global exist; the private group carries both private rows.
    expect(keys[0]).toBe('bundled');
    expect(keys).toContain('global');
    expect(keys).toContain('private');

    const chips = [...document.body.querySelectorAll('.chip')].map((chip) =>
      chip.textContent.trim(),
    );
    expect(chips).toContain('Available');
    expect(chips).toContain('Disabled');
  });

  it('shows share actions only on owned rows and edit/delete only on writable scopes', async () => {
    await mountView();

    const buttons = [...document.body.querySelectorAll('button')];
    const shareButtons = buttons.filter(
      (button) => button.textContent.trim() === 'Share',
    );
    const disableButtons = buttons.filter(
      (button) => button.textContent.trim() === 'Disable',
    );

    // Owned rows carry share affordances: deploy is unshared ("Share"),
    // notes is already shared ("Unshare").
    const unshareButtons = buttons.filter(
      (button) => button.textContent.trim() === 'Unshare',
    );
    expect(shareButtons.length).toBe(1);
    expect(unshareButtons.length).toBe(1);
    // Disable exists on every enabled row; the already-disabled row flips to
    // "Enable" (4 entries total across both labels).
    const enableButtons = buttons.filter(
      (button) => button.textContent.trim() === 'Enable',
    );
    expect(disableButtons.length).toBe(3);
    expect(enableButtons.length).toBe(1);
    // Bundled teach never offers Delete (only broken/global does).
    const deleteButtons = buttons.filter(
      (button) => button.textContent.trim() === 'Delete',
    );
    expect(deleteButtons.length).toBeLessThan(disableButtons.length);
  });

  it('wires the disable action to skill.set_disabled and reloads inventory', async () => {
    await mountView();
    const before = callCount('skill.inventory');

    const disableButton = [...document.body.querySelectorAll('button')].find(
      (button) => button.textContent.trim() === 'Disable',
    );
    disableButton.click();
    flushSync();

    await waitForCondition(
      () =>
        callCount('skill.set_disabled') === 1 &&
        callCount('skill.inventory') > before,
    );

    expect(rpcMock).toHaveBeenCalledWith('skill.set_disabled', {
      name: 'teach',
      disabled: true,
    });
  });

  it('wires the share action to skill.share with the owning agent', async () => {
    await mountView();

    const shareButton = [...document.body.querySelectorAll('button')].find(
      (button) => button.textContent.trim() === 'Share',
    );
    shareButton.click();
    flushSync();

    await waitForCondition(() => callCount('skill.share') === 1);

    expect(rpcMock).toHaveBeenCalledWith('skill.share', {
      agent_id: 'main',
      name: 'deploy',
      shared: true,
    });
  });
});
