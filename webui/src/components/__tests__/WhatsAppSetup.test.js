// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import WhatsAppSetup from '../settings/WhatsAppSetup.svelte';
import { getWhatsAppStatus, setupWhatsApp, pairWhatsApp } from '$lib/api.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';

vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
vi.mock('$lib/api.js', () => ({
  getWhatsAppStatus: vi.fn(),
  setupWhatsApp: vi.fn(),
  pairWhatsApp: vi.fn(),
}));

describe('WhatsApp setup', () => {
  let component;
  beforeEach(() => {
    vi.useFakeTimers();
    vi.resetAllMocks();
  });
  afterEach(async () => {
    if (component) await unmount(component);
    component = null;
    document.body.innerHTML = '';
    vi.useRealTimers();
  });
  async function settle() {
    flushSync();
    await Promise.resolve();
    await Promise.resolve();
    flushSync();
  }
  function click(text) {
    const button = [...document.querySelectorAll('button')].find(
      (item) => item.textContent.trim() === text,
    );
    expect(button).toBeTruthy();
    button.click();
  }

  it('installs on request, shows the pairing QR and removes it when connected', async () => {
    getWhatsAppStatus.mockResolvedValue({
      installed: false,
      state: 'disconnected',
    });
    component = mount(WhatsAppSetup, {
      target: document.body,
      props: { channelId: 'wa' },
    });
    await settle();
    setupWhatsApp.mockResolvedValue({ installed: false, setup: 'installing' });
    click('Install WhatsApp support');
    await settle();
    expect(setupWhatsApp).toHaveBeenCalledWith('wa');
    expect(document.body.textContent).toContain('Installing WhatsApp support');
    getWhatsAppStatus.mockResolvedValue({
      installed: true,
      state: 'disconnected',
    });
    await vi.advanceTimersByTimeAsync(3000);
    await settle();
    pairWhatsApp.mockResolvedValue({
      installed: true,
      state: 'pairing',
      qr_image: 'data:image/png;base64,cXI=',
    });
    click('Connect WhatsApp');
    await settle();
    expect(pairWhatsApp).toHaveBeenCalledWith('wa', false);
    expect(document.querySelector('img').getAttribute('src')).toContain(
      'data:image/png',
    );
    getWhatsAppStatus.mockResolvedValue({
      installed: true,
      state: 'connected',
    });
    await vi.advanceTimersByTimeAsync(3000);
    await settle();
    expect(document.querySelector('img')).toBeNull();
    expect(document.body.textContent).toContain('WhatsApp connected');
  });

  it('leaves a collapsed row once connected but still shows failures', async () => {
    getWhatsAppStatus.mockResolvedValue({
      installed: true,
      state: 'connected',
    });
    component = mount(WhatsAppSetup, {
      target: document.body,
      props: { channelId: 'wa', hideWhenConnected: true },
    });
    await settle();
    expect(document.querySelector('.whatsapp-setup')).toBeNull();
    getWhatsAppStatus.mockResolvedValue({
      installed: true,
      state: 'connected',
      error: 'Session expired',
    });
    await vi.advanceTimersByTimeAsync(3000);
    await settle();
    expect(document.body.textContent).toContain('Session expired');
  });

  it('shows failures and stops polling after unmount', async () => {
    getWhatsAppStatus.mockResolvedValue({
      installed: true,
      state: 'logged_out',
    });
    component = mount(WhatsAppSetup, {
      target: document.body,
      props: { channelId: 'wa' },
    });
    await settle();
    pairWhatsApp.mockRejectedValue(new Error('Connection unavailable'));
    click('Link again with a new QR code');
    await settle();
    expect(pairWhatsApp).toHaveBeenCalledWith('wa', true);
    expect(document.body.textContent).toContain('Connection unavailable');
    await unmount(component);
    component = null;
    const count = getWhatsAppStatus.mock.calls.length;
    await vi.advanceTimersByTimeAsync(10000);
    expect(getWhatsAppStatus).toHaveBeenCalledTimes(count);
  });

  it('pauses hidden setup reads and preserves an installation until the visible view refreshes', async () => {
    getWhatsAppStatus.mockResolvedValue({
      installed: false,
      state: 'disconnected',
    });
    const props = reactiveProps({ channelId: 'wa', active: false });
    component = mount(WhatsAppSetup, { target: document.body, props });
    await settle();
    await vi.advanceTimersByTimeAsync(6000);
    expect(getWhatsAppStatus).not.toHaveBeenCalled();
    props.active = true;
    await settle();
    expect(getWhatsAppStatus).toHaveBeenCalledOnce();
    let finishInstall;
    setupWhatsApp.mockReturnValueOnce(
      new Promise((resolve) => {
        finishInstall = resolve;
      }),
    );
    click('Install WhatsApp support');
    props.active = false;
    await settle();
    finishInstall({ installed: false, setup: 'installing' });
    await settle();
    await vi.advanceTimersByTimeAsync(6000);
    expect(getWhatsAppStatus).toHaveBeenCalledOnce();
    expect(setupWhatsApp).toHaveBeenCalledOnce();

    let finishOldRead;
    getWhatsAppStatus.mockReturnValueOnce(
      new Promise((resolve) => {
        finishOldRead = resolve;
      }),
    );
    props.active = true;
    await settle();
    expect(getWhatsAppStatus).toHaveBeenCalledTimes(2);
    const hidden = vi.spyOn(document, 'hidden', 'get');
    try {
      hidden.mockReturnValue(true);
      document.dispatchEvent(new Event('visibilitychange'));
      await settle();
      await vi.advanceTimersByTimeAsync(6000);
      expect(getWhatsAppStatus).toHaveBeenCalledTimes(2);
      getWhatsAppStatus.mockResolvedValue({
        installed: true,
        state: 'connected',
      });
      hidden.mockReturnValue(false);
      document.dispatchEvent(new Event('visibilitychange'));
      await settle();
      expect(getWhatsAppStatus).toHaveBeenCalledTimes(3);
      finishOldRead({
        installed: true,
        state: 'pairing',
        qr_image: 'data:image/png;base64,cXI=',
      });
      await settle();
      expect(document.querySelector('img')).toBeNull();
      expect(document.body.textContent).toContain('WhatsApp connected');
    } finally {
      hidden.mockRestore();
    }
  });
});
