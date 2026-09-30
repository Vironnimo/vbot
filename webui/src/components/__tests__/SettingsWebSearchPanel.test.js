// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../lib/i18n.js';
import { rpcBackedApiMock } from './apiMock.support.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';

const rpcMock = vi.fn();
vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
vi.mock('$lib/api.js', () => rpcBackedApiMock(rpcMock));
const { default: Panel } =
  await import('../settings/SettingsWebSearchPanel.svelte');

const BRAVE = () => t('settings.webSearch.providers.brave');
const TAVILY = () => t('settings.webSearch.providers.tavily');
const DUCKDUCKGO = () => t('settings.webSearch.providers.duckduckgo');

function webSearchSettings(services) {
  return {
    web_search: {
      provider: 'brave',
      available_providers: ['brave', 'duckduckgo', 'tavily'],
      default_count: 12,
      searxng: { base_url: 'http://localhost:8888' },
      services,
    },
    general: { data_directory: '/vbot-data' },
  };
}

const settings = webSearchSettings([
  { id: 'brave', api_key_env: 'BRAVE_API_KEY', configured: false },
  { id: 'tavily', api_key_env: 'TAVILY_API_KEY', configured: true },
]);

describe('Web Search settings', () => {
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

  function keyRow() {
    return document.querySelector('[data-api-key]');
  }

  function openProviders() {
    document.getElementById('settings-web-search-provider').click();
    flushSync();
    return [...document.querySelectorAll('[role="option"]')];
  }

  function optionFor(options, label) {
    const option = options.find(
      (item) =>
        item
          .querySelector('.dropdown-primitive__option-label')
          ?.textContent.trim() === label,
    );
    expect(option).toBeTruthy();
    return option;
  }

  function choose(label) {
    optionFor(openProviders(), label).click();
    flushSync();
  }

  it('shows the API key row of the selected provider and marks keyed options', () => {
    component = mount(Panel, { target: document.body, props: { settings } });
    flushSync();

    // The selected provider's key row names the variable it needs.
    expect(keyRow().dataset.apiKey).toBe('missing');
    expect(keyRow().textContent).toContain('BRAVE_API_KEY');
    // Where the .env file lives stays behind the key's "?".
    keyRow().querySelector('.info-hint').click();
    flushSync();
    expect(document.querySelector('.info-popover').textContent).toContain(
      '/vbot-data',
    );

    const options = openProviders();
    const meta = (label) =>
      optionFor(options, label)
        .querySelector('.dropdown-primitive__option-meta')
        ?.textContent.trim();
    expect(meta(BRAVE())).toBe(t('settings.serviceKey.optionMissing'));
    expect(meta(TAVILY())).toBe(t('settings.serviceKey.optionSet'));
    expect(meta(DUCKDUCKGO())).toBeUndefined();
    optionFor(options, TAVILY()).click();
    flushSync();

    expect(keyRow().dataset.apiKey).toBe('set');
    expect(keyRow().textContent).toContain('TAVILY_API_KEY');

    // Keyless providers need no key row.
    choose(DUCKDUCKGO());
    expect(keyRow()).toBeNull();
  });

  it('treats key status as a server fact outside the draft', async () => {
    rpcMock.mockImplementation(async (_method, params) => ({
      ...settings,
      web_search: { ...settings.web_search, ...params.web_search },
    }));
    const props = reactiveProps({ settings });
    component = mount(Panel, { target: document.body, props });
    flushSync();

    // Newer settings with the key now present (such as after the key row
    // saved it) update the row but leave nothing to save.
    props.settings = webSearchSettings([
      { id: 'brave', api_key_env: 'BRAVE_API_KEY', configured: true },
      { id: 'tavily', api_key_env: 'TAVILY_API_KEY', configured: true },
    ]);
    flushSync();
    expect(keyRow().dataset.apiKey).toBe('set');
    await vi.advanceTimersByTimeAsync(850);
    expect(rpcMock).not.toHaveBeenCalled();

    // A saved change sends only the editable fields.
    choose(TAVILY());
    await vi.advanceTimersByTimeAsync(850);
    expect(rpcMock).toHaveBeenCalledWith('settings.update', {
      web_search: {
        provider: 'tavily',
        default_count: 12,
        searxng: { base_url: 'http://localhost:8888' },
      },
      base: {
        web_search: {
          provider: 'brave',
          default_count: 12,
          searxng: { base_url: 'http://localhost:8888' },
        },
      },
    });
  });
});
