// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { archiveRetention } from '../../lib/archiveRetention.svelte.js';
import { init, t } from '../../lib/i18n.js';
import { rpcBackedApiMock } from './apiMock.support.js';

const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => rpcBackedApiMock(rpcMock));

const { default: SettingsArchivePanel } =
  await import('../settings/SettingsArchivePanel.svelte');

describe('SettingsArchivePanel', () => {
  let mounted;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    archiveRetention.unknown = false;
    rpcMock.mockReset();
    rpcMock.mockImplementation(async (method, params) => ({
      archive: params.archive,
    }));
    mounted = null;
  });

  afterEach(async () => {
    if (mounted) await unmount(mounted);
    mounted = null;
    document.body.innerHTML = '';
  });

  function mountPanel(props) {
    mounted = mount(SettingsArchivePanel, {
      target: document.body,
      props,
    });
    flushSync();
  }

  function toggle() {
    return document.querySelector(
      `[role="switch"][aria-label="${t('settings.archive.automatic')}"]`,
    );
  }

  function daysField() {
    return document.getElementById('settings-archive-retention-days');
  }

  function typeDays(value) {
    daysField().value = value;
    daysField().dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
  }

  function save() {
    document.querySelector('.save-status button').click();
    flushSync();
  }

  it('saves a period of days, ignores one out of range, and saves never as null', async () => {
    const commits = [];
    mountPanel({
      settings: { archive: { retention_days: 30 } },
      onCommit: (next) => commits.push(next),
    });

    expect(toggle().getAttribute('aria-checked')).toBe('true');
    expect(daysField().value).toBe('30');
    // A value out of range marks the field and saves nothing.
    typeDays('0');
    expect(daysField().getAttribute('aria-invalid')).toBe('true');
    expect(document.querySelector('.save-status button')).toBeNull();

    typeDays('7');
    save();
    await waitForCondition(() => commits.length === 1);
    expect(rpcMock).toHaveBeenLastCalledWith('settings.update', {
      archive: { retention_days: 7 },
      base: { archive: { retention_days: 30 } },
    });

    toggle().click();
    flushSync();
    expect(daysField().closest('.s-row').hidden).toBe(true);
    save();
    await waitForCondition(() => commits.length === 2);
    expect(rpcMock).toHaveBeenLastCalledWith('settings.update', {
      archive: { retention_days: null },
      base: { archive: { retention_days: 7 } },
    });

    // Turning it back on returns to the last period.
    toggle().click();
    flushSync();
    expect(daysField().value).toBe('7');
  });

  it('says that automatic deletion is paused while vBot cannot read the period', () => {
    mountPanel({ settings: { archive: { retention_days: 30 } } });
    expect(document.querySelector('.banner--warn')).toBeNull();

    archiveRetention.unknown = true;
    flushSync();
    expect(document.querySelector('.banner--warn').textContent).toBe(
      t('archive.retention.unknown'),
    );
  });

  it('reports a period the server refuses', async () => {
    const onError = vi.fn();
    rpcMock.mockRejectedValue(
      Object.assign(new Error('archive.retention_days must be 1-3650'), {
        code: 'invalid_request',
      }),
    );
    mountPanel({ settings: { archive: { retention_days: null } }, onError });

    expect(toggle().getAttribute('aria-checked')).toBe('false');
    toggle().click();
    flushSync();
    expect(daysField().value).toBe('30');
    save();
    await waitForCondition(() =>
      onError.mock.calls.some(([message]) =>
        message.includes('archive.retention_days must be 1-3650'),
      ),
    );
    expect(rpcMock).toHaveBeenCalledWith('settings.update', {
      archive: { retention_days: 30 },
      base: { archive: { retention_days: null } },
    });
  });
});

async function waitForCondition(check, attempts = 20) {
  for (let index = 0; index < attempts; index += 1) {
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
    if (check()) return;
  }
  throw new Error('Timed out waiting for condition.');
}
