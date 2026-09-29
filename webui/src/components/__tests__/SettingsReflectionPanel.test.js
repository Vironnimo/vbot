// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { rpcBackedApiMock } from './apiMock.support.js';

const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => rpcBackedApiMock(rpcMock));

const { default: SettingsReflectionPanel } =
  await import('../settings/SettingsReflectionPanel.svelte');

const SETTINGS = Object.freeze({
  reflection: {
    enabled: false,
    memory_turn_interval: 10,
    skill_model_step_interval: 10,
  },
});

describe('SettingsReflectionPanel', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    rpcMock.mockReset();
    rpcMock.mockResolvedValue({
      reflection: {
        enabled: true,
        memory_turn_interval: 10,
        skill_model_step_interval: 10,
      },
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

  it('shows the intervals only while reflection is on, keeps their drafts, and saves the edited section', async () => {
    const commits = [];
    mountedComponent = mount(SettingsReflectionPanel, {
      target: document.body,
      props: {
        settings: SETTINGS,
        onCommit: (next) => commits.push(next),
      },
    });
    flushSync();

    const toggle = document.body.querySelector('[role="switch"]');
    const memoryInput = () =>
      document.getElementById('settings-reflection-memory-interval');
    const memoryRowHidden = () => memoryInput().closest('.s-row').hidden;
    expect(toggle.getAttribute('aria-checked')).toBe('false');
    expect(memoryRowHidden()).toBe(true);

    toggle.click();
    flushSync();
    expect(memoryInput().value).toBe('10');
    expect(
      document.getElementById('settings-reflection-skill-interval').value,
    ).toBe('10');

    memoryInput().value = '5';
    memoryInput().dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    // Hiding the rows keeps the edited interval.
    toggle.click();
    flushSync();
    expect(memoryRowHidden()).toBe(true);
    toggle.click();
    flushSync();
    expect(memoryRowHidden()).toBe(false);
    expect(memoryInput().value).toBe('5');
    findSaveButton().click();
    flushSync();
    await waitForCondition(() => commits.length === 1);

    expect(rpcMock.mock.calls).toEqual([
      [
        'settings.update',
        {
          reflection: {
            enabled: true,
            memory_turn_interval: 5,
            skill_model_step_interval: 10,
          },
        },
      ],
    ]);
    expect(commits[0].reflection.enabled).toBe(true);
  });
});

function findSaveButton() {
  return [...document.body.querySelectorAll('button')].find((button) =>
    button.className.includes('s-save-button'),
  );
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
  throw new Error('Timed out waiting for condition.');
}
