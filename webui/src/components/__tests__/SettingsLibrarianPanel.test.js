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

const { default: SettingsLibrarianPanel } =
  await import('../settings/SettingsLibrarianPanel.svelte');

const SETTINGS = Object.freeze({
  librarian: {
    enabled: false,
    interval_days: 7,
    archive_after_days: 90,
    consolidate: true,
  },
});

describe('SettingsLibrarianPanel', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    rpcMock.mockReset();
    rpcMock.mockImplementation(async (_method, params) => ({
      librarian: params.librarian,
    }));
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
  });

  it('shows the interval only while the schedule is on, keeps the hand-run options, and saves the edited section', async () => {
    const commits = [];
    mountedComponent = mount(SettingsLibrarianPanel, {
      target: document.body,
      props: {
        settings: SETTINGS,
        onCommit: (next) => commits.push(next),
      },
    });
    flushSync();

    const [scheduleToggle, consolidateToggle] =
      document.body.querySelectorAll('[role="switch"]');
    const intervalInput = () =>
      document.getElementById('settings-librarian-interval');
    const archiveInput = () =>
      document.getElementById('settings-librarian-archive-after');
    expect(scheduleToggle.getAttribute('aria-checked')).toBe('false');
    expect(consolidateToggle.getAttribute('aria-checked')).toBe('true');
    expect(intervalInput().closest('.s-row').hidden).toBe(true);
    // A pass started by hand uses these even while the schedule is off.
    expect(archiveInput().closest('.s-row').hidden).toBe(false);
    expect(archiveInput().value).toBe('90');

    scheduleToggle.click();
    flushSync();
    expect(intervalInput().closest('.s-row').hidden).toBe(false);
    intervalInput().value = '14';
    intervalInput().dispatchEvent(new Event('input', { bubbles: true }));
    archiveInput().value = '0';
    archiveInput().dispatchEvent(new Event('input', { bubbles: true }));
    consolidateToggle.click();
    flushSync();
    findSaveButton().click();
    flushSync();
    await waitForCondition(() => commits.length === 1);

    // The rejected 0 keeps the last valid archive age.
    expect(rpcMock.mock.calls).toEqual([
      [
        'settings.update',
        {
          librarian: {
            enabled: true,
            consolidate: false,
            interval_days: 14,
            archive_after_days: 90,
          },
          base: { librarian: SETTINGS.librarian },
        },
      ],
    ]);
    expect(commits[0].librarian.interval_days).toBe(14);
  });
});

function findSaveButton() {
  return document.body.querySelector('.save-status button');
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
