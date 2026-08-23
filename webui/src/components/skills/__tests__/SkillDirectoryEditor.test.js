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

const { default: SkillDirectoryEditor } =
  await import('../SkillDirectoryEditor.svelte');

describe('SkillDirectoryEditor', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    vi.useFakeTimers();
    rpcMock.mockReset();
    rpcMock.mockImplementation((method) => {
      if (method === 'settings.update') {
        return Promise.resolve({ skills: { directories: [] } });
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
    vi.useRealTimers();
  });

  function mountEditor(onCommit) {
    mountedComponent = mount(SkillDirectoryEditor, {
      target: document.body,
      props: {
        settings: { skills: { directories: ['C:/existing'] } },
        onCommit,
        onToast: () => {},
        onError: () => {},
      },
    });
    flushSync();
  }

  const input = () =>
    document.body.querySelector('input[placeholder="C:/path/to/skills"]');

  async function addDirectory(path) {
    input().value = path;
    input().dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    const addButtons = [...document.body.querySelectorAll('button')].filter(
      (button) => button.textContent.trim() === 'Add directory',
    );
    expect(addButtons.length).toBe(1);
    addButtons[0].click();
    flushSync();
  }

  it('persists an added directory through settings.update and reports the commit', async () => {
    const onCommit = vi.fn();
    mountEditor(onCommit);

    await addDirectory('C:/extra');
    // The debounced autosave fires after 800ms.
    await vi.advanceTimersByTimeAsync(900);

    const updateCalls = rpcMock.mock.calls.filter(
      (call) => call[0] === 'settings.update',
    );
    expect(updateCalls.length).toBe(1);
    expect(JSON.stringify(updateCalls[0][1])).toContain('C:/extra');
    expect(onCommit).toHaveBeenCalled();
  });

  it('saves immediately through the explicit Save button', async () => {
    const onCommit = vi.fn();
    mountEditor(onCommit);

    await addDirectory('C:/manual');
    const saveButton = [...document.body.querySelectorAll('button')].find(
      (button) => button.textContent.trim() === 'Save',
    );
    saveButton.click();
    flushSync();
    await vi.advanceTimersByTimeAsync(0);

    expect(
      rpcMock.mock.calls.some((call) => call[0] === 'settings.update'),
    ).toBe(true);
  });

  it('removes a configured directory and saves without it', async () => {
    rpcMock.mockImplementation((method, params) => {
      if (method === 'settings.update') {
        return Promise.resolve({
          skills: { directories: params?.skills?.directories ?? [] },
        });
      }
      return Promise.resolve({});
    });
    mountEditor(vi.fn());

    const removeButton = [...document.body.querySelectorAll('button')].find(
      (button) => button.textContent.trim() === 'Remove',
    );
    expect(removeButton).not.toBeUndefined();
    removeButton.click();
    flushSync();
    await vi.advanceTimersByTimeAsync(900);

    const updateCall = rpcMock.mock.calls.find(
      (call) => call[0] === 'settings.update',
    );
    expect(JSON.stringify(updateCall?.[1])).not.toContain('C:/existing');
  });

  it('does not save while the list matches the server state', async () => {
    mountEditor(vi.fn());

    await vi.advanceTimersByTimeAsync(2000);

    expect(
      rpcMock.mock.calls.some((call) => call[0] === 'settings.update'),
    ).toBe(false);
  });
});
