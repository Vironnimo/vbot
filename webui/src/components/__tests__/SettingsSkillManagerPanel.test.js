// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  rpc: (...args) => rpcMock(...args),
}));

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

    expect(document.body.textContent).toContain('deploy');
    expect(document.body.textContent).toContain('Ship the app.');
    expect(rpcMock).toHaveBeenCalledWith('skill.read', { scope: 'global' });
    expect(rpcMock).toHaveBeenCalledWith('agent.list');
  });

  it('creates a skill in the global scope', async () => {
    await mountPanel();

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

  it('deletes a skill', async () => {
    await mountPanel();

    buttonByText('Delete').click();
    await flushAsync();

    const call = rpcMock.mock.calls.find(
      (entry) => entry[0] === 'skill.delete',
    );
    expect(call).toEqual(['skill.delete', { scope: 'global', name: 'deploy' }]);
  });

  it('surfaces a create error from the RPC diagnostics', async () => {
    rpcMock.mockImplementation((method) => {
      if (method === 'skill.create') {
        return Promise.reject(new Error('Skill metadata is invalid.'));
      }
      return defaultRpc(method);
    });
    await mountPanel();

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
