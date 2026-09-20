// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { rpcBackedApiMock } from './apiMock.js';

const rpcMock = vi.fn();
vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
vi.mock('$lib/api.js', () => rpcBackedApiMock(rpcMock));
const { default: Panel } =
  await import('../settings/SettingsWebFetchPanel.svelte');

const settings = {
  web_fetch: {
    provider: 'direct',
    mode: 'fallback',
    available_providers: ['direct', 'tavily', 'parallel'],
    services: [
      {
        id: 'tavily',
        api_key_env: 'TAVILY_API_KEY',
        configured: false,
        pricing_url: 'https://docs.tavily.com/documentation/api-credits',
      },
    ],
  },
  general: { data_directory: '/vbot-data' },
};

describe('Web Fetch settings', () => {
  let component;
  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    rpcMock.mockReset();
    vi.useFakeTimers();
  });
  afterEach(async () => {
    if (component) await unmount(component);
    component = null;
    vi.useRealTimers();
    document.body.innerHTML = '';
  });

  async function choose(id, label) {
    document.getElementById(id).click();
    flushSync();
    await Promise.resolve();
    const option = [...document.querySelectorAll('[role="option"]')].find(
      (item) => item.textContent.trim() === label,
    );
    expect(option).toBeTruthy();
    option.click();
    flushSync();
  }

  it('starts with direct fetching and exposes cost, credential and pricing information on opt-in', async () => {
    component = mount(Panel, { target: document.body, props: { settings } });
    flushSync();
    expect(document.body.textContent).toContain('Direct (no service)');
    expect(document.getElementById('settings-web-fetch-mode')).toBeNull();
    await choose('settings-web-fetch-provider', 'Tavily');
    expect(document.body.textContent).toContain('may charge per page');
    expect(document.body.textContent).toContain('TAVILY_API_KEY');
    expect(document.body.textContent).toContain('/vbot-data');
    expect(document.querySelector('a').href).toBe(
      settings.web_fetch.services[0].pricing_url,
    );
    expect(rpcMock).not.toHaveBeenCalled();
  });

  it('autosaves only the selection and preserves a newer draft while the save is pending', async () => {
    let resolveSave;
    rpcMock.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSave = resolve;
        }),
    );
    component = mount(Panel, { target: document.body, props: { settings } });
    flushSync();
    await choose('settings-web-fetch-provider', 'Tavily');
    await vi.advanceTimersByTimeAsync(850);
    expect(rpcMock).toHaveBeenCalledWith('settings.update', {
      web_fetch: { provider: 'tavily', mode: 'fallback' },
    });
    await choose('settings-web-fetch-provider', 'Parallel');
    resolveSave({
      ...settings,
      web_fetch: { ...settings.web_fetch, provider: 'tavily' },
    });
    await Promise.resolve();
    await Promise.resolve();
    flushSync();
    expect(
      document.getElementById('settings-web-fetch-provider').textContent,
    ).toContain('Parallel');
  });

  it('retains the selection and exposes save failure for retry', async () => {
    const errors = [];
    rpcMock.mockRejectedValue(new Error('Offline'));
    component = mount(Panel, {
      target: document.body,
      props: { settings, onError: (message) => errors.push(message) },
    });
    flushSync();
    await choose('settings-web-fetch-provider', 'Tavily');
    await choose('settings-web-fetch-mode', 'Prefer this service');
    await vi.advanceTimersByTimeAsync(850);
    flushSync();
    expect(errors.some((message) => message.includes('Offline'))).toBe(true);
    expect(
      document.getElementById('settings-web-fetch-mode').textContent,
    ).toContain('Prefer this service');
  });
});
