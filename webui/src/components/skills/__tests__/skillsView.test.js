// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../../lib/i18n.js';
import { TOOLTIP_SHOW_DELAY_MS } from '../../../lib/tooltip.js';
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

const MULTI_PROJECT_INVENTORY = {
  ...INVENTORY,
  skills: [
    ...INVENTORY.skills,
    {
      name: 'shared-project-skill',
      description: 'From the first project.',
      origin: 'project:first',
      owner_id: null,
      shared: false,
      disabled: false,
      status: 'available',
      missing: [],
      optional_missing: [],
      warnings: [],
    },
    {
      name: 'shared-project-skill',
      description: 'From the second project.',
      origin: 'project:second',
      owner_id: null,
      shared: false,
      disabled: false,
      status: 'available',
      missing: [],
      optional_missing: [],
      warnings: [],
    },
  ],
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
  let inventoryResponse;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    inventoryResponse = INVENTORY;
    rpcMock.mockReset();
    rpcMock.mockImplementation((method) => {
      if (method === 'skill.inventory') {
        return Promise.resolve(inventoryResponse);
      }
      if (method === 'agent.list') {
        return Promise.resolve({
          agents: [
            { id: 'main', name: 'Main' },
            { id: 'reviewer', name: 'Reviewer' },
          ],
        });
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

  it('wires the share action to skill.share with receivers', async () => {
    await mountView();

    // Click "Share" on the deploy entry (owner: main, not yet shared).
    const shareButton = [...document.body.querySelectorAll('button')].find(
      (button) => button.textContent.trim() === 'Share',
    );
    shareButton.click();
    flushSync();

    // The share modal opens with a toggle list of other agents.
    const modal = document.body.querySelector('[role="dialog"]');
    expect(modal).not.toBeNull();
    const toggles = [...modal.querySelectorAll('[role="switch"]')];
    expect(toggles.length).toBe(1); // only "reviewer" is available

    // Select the receiver and save.
    toggles[0].click();
    flushSync();

    const saveButton = [...modal.querySelectorAll('button')].find(
      (button) => button.textContent.trim() === 'Save',
    );
    saveButton.click();
    flushSync();

    await waitForCondition(() => callCount('skill.share') === 1);

    expect(rpcMock).toHaveBeenCalledWith('skill.share', {
      agent_id: 'main',
      name: 'deploy',
      shared: true,
      receivers: ['reviewer'],
    });
  });

  it('filters skills by search query against name and description', async () => {
    await mountView();

    // All four skills visible initially.
    let names = [...document.body.querySelectorAll('.skills-card-name')].map(
      (el) => el.textContent.trim(),
    );
    expect(names).toHaveLength(4);

    const searchInput = document.body.querySelector('.skills-search-input');
    searchInput.value = 'teach';
    searchInput.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    names = [...document.body.querySelectorAll('.skills-card-name')].map((el) =>
      el.textContent.trim(),
    );
    expect(names).toEqual(['teach']);
  });

  it('keeps descriptions in skill-name tooltips and renders edit as an icon', async () => {
    await mountView();

    expect(document.body.querySelectorAll('.skills-card-desc')).toHaveLength(0);

    const skillName = [
      ...document.body.querySelectorAll('.skills-card-name'),
    ].find((element) => element.textContent.trim() === 'teach');
    vi.useFakeTimers();
    skillName.dispatchEvent(new Event('pointerenter'));
    await vi.advanceTimersByTimeAsync(TOOLTIP_SHOW_DELAY_MS);
    expect(document.getElementById('app-tooltip').textContent).toBe(
      'Teach a topic.',
    );
    skillName.dispatchEvent(new Event('pointerleave'));
    vi.useRealTimers();

    const editButton = document.body.querySelector('button[aria-label="Edit"]');
    expect(editButton).not.toBeNull();
    expect(editButton.textContent.trim()).toBe('');
    expect(editButton.querySelector('svg')).not.toBeNull();
  });

  it('groups by agent when the by-agent view toggle is activated', async () => {
    await mountView();

    // Default: by-source groups include 'bundled', 'global', 'private'.
    let keys = [
      ...document.body.querySelectorAll('[data-testid^="skill-group-"]'),
    ].map((el) => el.getAttribute('data-testid').replace('skill-group-', ''));
    expect(keys).toContain('private');

    // Switch to by-agent: the agent's private skills group under their id.
    const agentToggle = [...document.body.querySelectorAll('button')].find(
      (button) => button.textContent.trim() === 'By agent',
    );
    agentToggle.click();
    flushSync();

    keys = [
      ...document.body.querySelectorAll('[data-testid^="skill-group-"]'),
    ].map((el) => el.getAttribute('data-testid').replace('skill-group-', ''));
    // The agent group key is `agent:main`; bundled/global source groups follow.
    expect(keys).toContain('agent:main');
    expect(keys).toContain('bundled');
  });

  it('renders same-named skills from multiple projects without duplicate keys', async () => {
    inventoryResponse = MULTI_PROJECT_INVENTORY;
    await mountView();

    const matchingCards = [
      ...document.body.querySelectorAll('.skills-card-name'),
    ].filter(
      (element) => element.textContent.trim() === 'shared-project-skill',
    );

    expect(matchingCards).toHaveLength(2);
  });

  it('opens the create modal when the new-skill button is clicked', async () => {
    await mountView();

    const newButton = [...document.body.querySelectorAll('button')].find(
      (button) => button.textContent.trim() === '+ New skill',
    );
    newButton.click();
    flushSync();

    const modal = document.body.querySelector('[role="dialog"]');
    expect(modal).not.toBeNull();
    // The scope selector is inside the modal.
    expect(modal.querySelector('#create-scope')).not.toBeNull();
    expect(modal.querySelector('#new-skill-name')).not.toBeNull();
    expect(modal.querySelector('#new-skill-content')).not.toBeNull();
  });

  it('shows agent display names instead of raw ids in private group labels', async () => {
    await mountView();

    const groupLabels = [
      ...document.body.querySelectorAll('.skills-group-title'),
    ].map((el) => el.textContent.trim());
    // The private group label should contain the agent's display name "Main",
    // not the raw id "main".
    const privateLabel = groupLabels.find((label) => label.includes('Main'));
    expect(privateLabel).toBeTruthy();
  });
});
