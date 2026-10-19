// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../lib/i18n.js';
import { signalAccessLoss } from '../../lib/api/accessLoss.js';
import GatedApp from './AccessGateApp.support.svelte';

const api = vi.hoisted(() => ({
  getAuthStatus: vi.fn(),
  startPairing: vi.fn(),
  pollPairing: vi.fn(),
}));
vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
vi.mock('$lib/api.js', async () => {
  const loss = await import('../../lib/api/accessLoss.js');
  return {
    ...api,
    DEVICE_KIND_BROWSER: 'browser',
    onAccessLoss: loss.onAccessLoss,
  };
});
const { default: AccessGate } = await import('../access/AccessGate.svelte');

const SIGNED_OUT = {
  mode: 'authenticated',
  mode_label: 'With login (internet)',
  authenticated: false,
  principal: null,
};
const SIGNED_IN = { ...SIGNED_OUT, authenticated: true, principal: 'device' };
const STARTED = {
  request_id: 'request-1',
  code: 'K7QM-4XPA',
  poll_secret: 'secret-1',
  expires_in: 600,
  poll_interval: 2,
};

let component;

beforeEach(() => {
  document.body.innerHTML = '';
  init('en');
  vi.useFakeTimers();
  for (const fn of Object.values(api)) fn.mockReset();
  api.startPairing.mockResolvedValue(STARTED);
  api.pollPairing.mockResolvedValue({ status: 'pending' });
});

afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  vi.useRealTimers();
  document.body.innerHTML = '';
});

async function settle(ms = 0) {
  await vi.advanceTimersByTimeAsync(ms);
  flushSync();
}

async function render() {
  component = mount(AccessGate, {
    target: document.body,
    props: { App: GatedApp },
  });
  await settle();
}

const appShown = () => document.querySelector('[data-testid="gated-app"]');
const heading = () => document.querySelector('h1');
const liveRegion = () =>
  document.querySelector('[role="status"][aria-live="polite"]');

function clickButton(label) {
  const match = [...document.querySelectorAll('button')].find(
    (item) => item.textContent.trim() === label,
  );
  expect(match, `button ${label}`).toBeTruthy();
  match.click();
}

describe('access gate', () => {
  it('mounts the app straight away when the server lets this page in', async () => {
    api.getAuthStatus.mockResolvedValue({
      mode: 'local',
      authenticated: true,
      principal: 'open',
    });

    await render();

    expect(appShown()).toBeTruthy();
    expect(api.startPairing).not.toHaveBeenCalled();
  });

  it('pairs a signed-out browser, mounts the app once approved and returns on a lost sign-in', async () => {
    api.getAuthStatus
      .mockResolvedValueOnce(SIGNED_OUT)
      .mockResolvedValueOnce(SIGNED_IN)
      .mockResolvedValue(SIGNED_OUT);
    api.pollPairing
      .mockResolvedValueOnce({ status: 'pending' })
      .mockResolvedValueOnce({ status: 'approved', device: { id: 'dev-1' } });

    await render();

    expect(appShown()).toBeNull();
    expect(heading().textContent.trim()).toBe(t('access.pairing.title'));
    expect(document.activeElement).toBe(heading());
    expect(api.startPairing).toHaveBeenCalledExactlyOnceWith(
      { kind: 'browser', name: expect.any(String) },
      { signal: expect.any(AbortSignal) },
    );
    // The code reaches screen readers spelled out, in the live region.
    expect(liveRegion().textContent).toContain('K7QM-4XPA');
    expect(liveRegion().textContent).toContain('K 7 Q M, 4 X P A');
    expect(document.body.textContent).toContain(
      'vbot devices approve K7QM-4XPA',
    );
    // The code names the server it belongs to.
    expect(document.body.textContent).toContain(
      `signed in to vBot at ${window.location.origin}`,
    );

    await settle(2_000);
    expect(appShown()).toBeNull();
    await settle(2_000);
    expect(appShown()).toBeTruthy();

    signalAccessLoss('auth_required');
    await settle();
    expect(appShown()).toBeNull();
    expect(heading().textContent.trim()).toBe(t('access.pairing.title'));
  });

  it('asks before pairing again when an approved browser did not keep its sign-in', async () => {
    api.getAuthStatus.mockResolvedValue(SIGNED_OUT);
    api.pollPairing.mockResolvedValue({ status: 'approved', device: null });
    await render();

    await settle(2_000);

    expect(document.querySelector('[role="alert"]').textContent).toContain(
      t('access.pairing.notKept'),
    );
    expect(api.startPairing).toHaveBeenCalledOnce();
    clickButton(t('access.pairing.startAgain'));
    await settle();
    expect(api.startPairing).toHaveBeenCalledTimes(2);
  });

  it.each(['startPairing', 'pollPairing'])(
    'waits before restarting pairing after %s reports unavailable devices',
    async (endpoint) => {
      api.getAuthStatus.mockResolvedValue(SIGNED_OUT);
      api[endpoint].mockRejectedValueOnce(
        Object.assign(new Error('unavailable'), {
          code: 'devices_unavailable',
          retryAfter: 45,
        }),
      );
      await render();
      if (endpoint === 'pollPairing') await settle(2_000);
      const retry = [...document.querySelectorAll('button')].find(
        (item) => item.textContent.trim() === t('access.pairing.startAgain'),
      );
      expect(retry.disabled).toBe(true);
      retry.click();
      await settle(44_999);
      expect(api.startPairing).toHaveBeenCalledOnce();
      expect(retry.disabled).toBe(true);
      await settle(1);
      expect(retry.disabled).toBe(false);
      retry.click();
      await settle();
      expect(api.startPairing).toHaveBeenCalledTimes(2);
    },
  );

  it.each([
    ['access_denied', 'access.denied.title'],
    // The server cannot check devices: the sign-in stays, nothing pairs again.
    ['devices_unavailable', 'access.unavailable.title'],
  ])(
    'shows the %s refusal and lets the user try again',
    async (code, title) => {
      api.getAuthStatus
        .mockRejectedValueOnce(Object.assign(new Error('refused'), { code }))
        .mockResolvedValue(SIGNED_IN);
      await render();

      expect(heading().textContent.trim()).toBe(t(title));
      expect(document.activeElement).toBe(heading());
      expect(api.startPairing).not.toHaveBeenCalled();

      clickButton(t('common.retry'));
      await settle();
      if (code === 'devices_unavailable') {
        expect(appShown()).toBeNull();
        expect(api.getAuthStatus).toHaveBeenCalledOnce();
        await settle(30_000);
      }
      expect(appShown()).toBeTruthy();
    },
  );
});
