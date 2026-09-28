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

const { default: SettingsNotificationsPanel } =
  await import('../settings/SettingsNotificationsPanel.svelte');

const ALL_ON = Object.freeze({
  run_completed: true,
  run_failed: true,
  automation_failed: true,
  update_result: true,
  server_stopped: true,
});

describe('SettingsNotificationsPanel', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    rpcMock.mockReset();
    rpcMock.mockResolvedValue({
      notifications: { ...ALL_ON, run_failed: false },
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

  it('shows every kind on without a stored section and saves the whole section', async () => {
    const commits = [];
    mountedComponent = mount(SettingsNotificationsPanel, {
      target: document.body,
      props: {
        settings: {},
        onCommit: (next) => commits.push(next),
      },
    });
    flushSync();

    const toggles = [...document.body.querySelectorAll('[role="switch"]')];
    expect(
      toggles.map((toggle) => [
        toggle.getAttribute('aria-label'),
        toggle.getAttribute('aria-checked'),
      ]),
    ).toEqual([
      ['Run completed', 'true'],
      ['Run failed', 'true'],
      ['Automation failed', 'true'],
      ['Update result', 'true'],
      ['Server stopped', 'true'],
    ]);

    toggles[1].click();
    flushSync();
    findSaveButton().click();
    flushSync();
    await waitForCondition(() => commits.length === 1);

    expect(rpcMock.mock.calls).toEqual([
      ['settings.update', { notifications: { ...ALL_ON, run_failed: false } }],
    ]);
    expect(commits[0].notifications.run_failed).toBe(false);
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
