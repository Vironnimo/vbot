// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { rpcBackedApiMock } from './apiMock.js';

const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => rpcBackedApiMock(rpcMock));

const { default: SettingsSkillManagerPanel } =
  await import('../settings/SettingsSkillManagerPanel.svelte');

function defaultRpc(method) {
  if (method === 'agent.list') {
    return Promise.resolve({ agents: [{ id: 'builder', name: 'Builder' }] });
  }
  if (method === 'skill.read') {
    return Promise.resolve({
      skills: [
        {
          name: 'deploy',
          description: 'Ship the app.',
          content:
            '---\nname: deploy\ndescription: Ship the app.\n---\n\n# Deploy',
        },
      ],
    });
  }
  return Promise.resolve({});
}

function buttonByText(text) {
  return [...document.body.querySelectorAll('button')].find((button) =>
    button.textContent.trim().includes(text),
  );
}

// Clicks a button in the open ConfirmDialog by its label (Delete / Cancel).
function confirmDialog(label) {
  const footer = document.body.querySelector('.modal-footer');
  expect(footer, 'confirm dialog not open').toBeTruthy();
  const button = [...footer.querySelectorAll('button')].find(
    (item) => item.textContent.trim() === label,
  );
  expect(button, `confirm button not found: ${label}`).toBeTruthy();
  button.click();
}

async function flushAsync() {
  await Promise.resolve();
  await Promise.resolve();
  flushSync();
}

describe('SettingsSkillManagerPanel', () => {
  let mountedComponent;
  let onToast;
  let onError;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    rpcMock.mockReset();
    rpcMock.mockImplementation(defaultRpc);
    onToast = vi.fn();
    onError = vi.fn();
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
  });

  async function mountPanel() {
    mountedComponent = mount(SettingsSkillManagerPanel, {
      target: document.body,
      props: { onToast, onError },
    });
    flushSync();
    await flushAsync();
  }

  it('loads and renders the global scope skills', async () => {
    await mountPanel();

    expect(
      document.body.querySelector('.s-skill-manager-name').textContent,
    ).toBe('deploy');
    expect(
      document.body.querySelector('.s-skill-manager-desc').textContent,
    ).toBe('Ship the app.');
    expect(document.body.querySelector('.s-skill-manager-create')).toBeNull();
    expect(rpcMock).toHaveBeenCalledWith('skill.read', { scope: 'global' });
    expect(rpcMock).toHaveBeenCalledWith('agent.list');
  });

  it('opens and cancels the labeled create form on demand', async () => {
    await mountPanel();

    buttonByText('New skill').click();
    await flushAsync();

    expect(document.body.querySelector('.s-skill-manager-create')).toBeTruthy();
    expect(
      document.querySelector('label[for="new-skill-name"]').textContent,
    ).toContain('Skill name');
    expect(
      document.querySelector('label[for="new-skill-content"]').textContent,
    ).toContain('SKILL.md content');

    buttonByText('Cancel').click();
    await flushAsync();

    expect(document.body.querySelector('.s-skill-manager-create')).toBeNull();
  });

  it('creates a skill in the global scope', async () => {
    await mountPanel();

    buttonByText('New skill').click();
    await flushAsync();

    const input = document.body.querySelector('input');
    input.value = 'newskill';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    const textarea = document.body.querySelector('textarea');
    textarea.value = '---\nname: newskill\ndescription: New.\n---\n\n# New';
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    await flushAsync();

    buttonByText('Create skill').click();
    await flushAsync();

    const call = rpcMock.mock.calls.find(
      (entry) => entry[0] === 'skill.create',
    );
    expect(call).toBeTruthy();
    expect(call[1].scope).toBe('global');
    expect(call[1].name).toBe('newskill');
    expect(call[1].content).toContain('name: newskill');
    expect(onToast).toHaveBeenCalled();
    expect(document.body.querySelector('.s-skill-manager-create')).toBeNull();
  });

  it('edits an existing skill', async () => {
    await mountPanel();

    buttonByText('Edit').click();
    await flushAsync();
    buttonByText('Save').click();
    await flushAsync();

    const call = rpcMock.mock.calls.find(
      (entry) => entry[0] === 'skill.update',
    );
    expect(call).toBeTruthy();
    expect(call[1]).toMatchObject({ scope: 'global', name: 'deploy' });
  });

  it('deletes a skill after confirming the dialog', async () => {
    await mountPanel();

    buttonByText('Delete').click();
    await flushAsync();

    // The row action opens the shared ConfirmDialog; skill.delete only fires
    // once it is confirmed.
    expect(
      rpcMock.mock.calls.some((entry) => entry[0] === 'skill.delete'),
    ).toBe(false);
    confirmDialog('Delete');
    await flushAsync();

    const call = rpcMock.mock.calls.find(
      (entry) => entry[0] === 'skill.delete',
    );
    expect(call).toEqual(['skill.delete', { scope: 'global', name: 'deploy' }]);
  });

  it('does not delete a skill when the dialog is cancelled', async () => {
    await mountPanel();

    buttonByText('Delete').click();
    await flushAsync();

    confirmDialog('Cancel');
    await flushAsync();

    expect(
      rpcMock.mock.calls.some((entry) => entry[0] === 'skill.delete'),
    ).toBe(false);
    expect(document.body.querySelector('.modal-footer')).toBeNull();
  });

  it('surfaces a create error from the RPC diagnostics', async () => {
    rpcMock.mockImplementation((method) => {
      if (method === 'skill.create') {
        return Promise.reject(new Error('Skill metadata is invalid.'));
      }
      return defaultRpc(method);
    });
    await mountPanel();

    buttonByText('New skill').click();
    await flushAsync();

    const input = document.body.querySelector('input');
    input.value = 'bad';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    const textarea = document.body.querySelector('textarea');
    textarea.value = 'x';
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    await flushAsync();

    buttonByText('Create skill').click();
    await flushAsync();

    expect(onError).toHaveBeenCalled();
    expect(onError.mock.calls.at(-1)[0]).toContain('invalid');
  });
});
