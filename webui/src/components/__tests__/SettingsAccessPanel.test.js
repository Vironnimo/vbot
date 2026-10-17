// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../lib/i18n.js';

const api = vi.hoisted(() => ({
  getAuthStatus: vi.fn(),
  listDevices: vi.fn(),
  renameDevice: vi.fn(),
  revokeDevice: vi.fn(),
  revokeOtherDevices: vi.fn(),
  logOutDevice: vi.fn(),
  getPendingPairing: vi.fn(),
  approvePairing: vi.fn(),
  denyPairing: vi.fn(),
  signalAccessLoss: vi.fn(),
}));
vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
vi.mock('$lib/api.js', () => ({
  ...api,
  ACCESS_LOSS_AUTH_REQUIRED: 'auth_required',
}));
const { default: Panel } =
  await import('../settings/SettingsAccessPanel.svelte');

const SIGNED_IN = {
  mode: 'authenticated',
  mode_label: 'With login (internet)',
  authenticated: true,
  principal: 'device',
};
const LAPTOP = {
  id: 'dev-1',
  name: 'Firefox on Linux',
  kind: 'browser',
  created_at: '2026-10-01T08:00:00Z',
  last_used_at: '2026-10-17T09:30:00Z',
  last_client: '192.168.1.20',
  current: true,
};
const PHONE = {
  id: 'dev-2',
  name: 'Phone',
  kind: 'browser',
  created_at: '2026-10-02T08:00:00Z',
  last_used_at: null,
  last_client: null,
  current: false,
};

let component;
let onToast;

beforeEach(() => {
  document.body.innerHTML = '';
  init('en');
  vi.useFakeTimers();
  for (const fn of Object.values(api)) fn.mockReset();
  api.getAuthStatus.mockResolvedValue(SIGNED_IN);
  api.listDevices.mockResolvedValue({ devices: [LAPTOP, PHONE] });
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
  component = mount(Panel, { target: document.body, props: { onToast } });
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

async function type(id, value) {
  const input = document.getElementById(id);
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  await settle();
}

async function submit(formId) {
  document
    .getElementById(formId)
    .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  await settle();
}

const rows = () =>
  [...document.querySelectorAll('[role="listitem"]')].map((row) =>
    row.textContent.replace(/\s+/g, ' ').trim(),
  );

describe('Settings access panel', () => {
  it('shows the mode and the devices, and signs this device out', async () => {
    api.logOutDevice.mockResolvedValue({ revoked: true });
    await render();

    expect(document.body.textContent).toContain('With login (internet)');
    expect(document.body.textContent).toContain('vbot application access show');
    const [laptop, phone] = rows();
    expect(laptop).toContain(t('settings.access.devices.thisDevice'));
    expect(laptop).toContain('from 192.168.1.20');
    expect(phone).not.toContain(t('settings.access.devices.thisDevice'));

    await click(t('settings.access.signOut.button'));
    await click(t('settings.access.signOut.confirm'));

    expect(api.logOutDevice).toHaveBeenCalledOnce();
    expect(api.signalAccessLoss).toHaveBeenCalledExactlyOnceWith(
      'auth_required',
    );
  });

  it('renames and removes a device and reloads the list', async () => {
    api.renameDevice.mockResolvedValue({});
    api.revokeDevice.mockResolvedValue({});
    await render();

    await click(t('settings.access.rename.aria', { name: 'Phone' }));
    await type('rename-device-name', '  Work phone ');
    await submit('rename-device-form');
    expect(api.renameDevice).toHaveBeenCalledExactlyOnceWith(
      'dev-2',
      'Work phone',
    );
    expect(onToast).toHaveBeenCalledWith(
      expect.objectContaining({ title: t('settings.access.rename.done') }),
    );

    await click(t('settings.access.revoke.aria', { name: 'Phone' }));
    await click(t('settings.access.revoke.confirm'));
    expect(api.revokeDevice).toHaveBeenCalledExactlyOnceWith('dev-2');
    expect(api.listDevices).toHaveBeenCalledTimes(3);
  });

  it('keeps a failed removal in its dialog', async () => {
    api.revokeDevice.mockRejectedValue(
      Object.assign(new Error('gone'), { code: 'device_not_found' }),
    );
    await render();

    await click(t('settings.access.revoke.aria', { name: 'Phone' }));
    await click(t('settings.access.revoke.confirm'));

    expect(document.querySelector('[role="alert"]').textContent).toContain(
      t('settings.access.error.deviceNotFound'),
    );
    expect(onToast).not.toHaveBeenCalled();
  });

  it('signs out every other device', async () => {
    api.revokeOtherDevices.mockResolvedValue({ revoked: ['dev-2'] });
    await render();

    await click(t('settings.access.revokeOthers.button'));
    expect(document.body.textContent).toContain(
      t('settings.access.revokeOthers.body'),
    );
    await click(t('settings.access.revokeOthers.confirm'));

    expect(api.revokeOtherDevices).toHaveBeenCalledOnce();
    expect(onToast).toHaveBeenCalledWith(
      expect.objectContaining({
        title: t('settings.access.revokeOthers.done', { count: 1 }),
      }),
    );
  });

  it('approves a new device by its pairing code after showing its details', async () => {
    api.getPendingPairing.mockResolvedValue({
      code: 'K7QM-4XPA',
      name: 'Safari on iOS',
      kind: 'browser',
      client: '203.0.113.7',
      requested_at: '2026-10-17T09:59:00Z',
      expires_in: 540,
    });
    api.approvePairing.mockResolvedValue({ name: 'Safari on iOS' });
    await render();

    await click(t('settings.access.add.button'));
    await type('add-device-code', 'k7qm-4xpa');
    await submit('add-device-form');
    expect(api.getPendingPairing).toHaveBeenCalledExactlyOnceWith('K7QM-4XPA');
    expect(document.body.textContent).toContain('Safari on iOS');
    expect(document.body.textContent).toContain('203.0.113.7');
    expect(api.approvePairing).not.toHaveBeenCalled();

    await click(t('settings.access.add.approve'));
    expect(api.approvePairing).toHaveBeenCalledExactlyOnceWith('K7QM-4XPA');
    expect(onToast).toHaveBeenCalledWith(
      expect.objectContaining({
        title: t('settings.access.add.approved', { name: 'Safari on iOS' }),
      }),
    );
    expect(document.getElementById('add-device-code')).toBeNull();
    // Reloaded now and again once the new device picked up its approval.
    expect(api.listDevices).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(4_000);
    expect(api.listDevices).toHaveBeenCalledTimes(3);
  });

  it('tells an unknown pairing code apart', async () => {
    api.getPendingPairing.mockRejectedValue(
      Object.assign(new Error('unknown'), { code: 'pairing_not_found' }),
    );
    await render();

    await click(t('settings.access.add.button'));
    await type('add-device-code', 'AAAA-BBBB');
    await submit('add-device-form');

    expect(document.body.textContent).toContain(
      t('settings.access.add.notFound'),
    );
    expect(api.approvePairing).not.toHaveBeenCalled();
  });

  it('explains that nobody signs in on a home-network server', async () => {
    api.getAuthStatus.mockResolvedValue({
      mode: 'lan',
      mode_label: 'Home network without login',
      authenticated: true,
      principal: 'open',
    });
    api.listDevices.mockResolvedValue({ devices: [] });
    await render();

    expect(document.body.textContent).toContain('Home network without login');
    expect(document.body.textContent).toContain(
      t('settings.access.devices.descriptionOpen'),
    );
    expect(document.body.textContent).toContain(
      t('settings.access.devices.emptyOpen'),
    );
    expect(document.body.textContent).not.toContain(
      t('settings.access.add.button'),
    );
  });
});
