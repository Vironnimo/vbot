// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import WhatsAppSetup from '../settings/WhatsAppSetup.svelte';
import { getWhatsAppStatus, setupWhatsApp, pairWhatsApp } from '$lib/api.js';

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
});
