// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  rpc: (...args) => rpcMock(...args),
}));

const { default: SettingsView } = await import('../SettingsView.svelte');

describe('SettingsView', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    rpcMock.mockReset();
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }

    document.body.innerHTML = '';
  });

  it('shows one global refresh button when any provider appears refresh-eligible', async () => {
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        settings: settingsPayload({ includeSecondEligibleProvider: true }),
      }),
    );

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openProvidersPanel();

    expect(buttonsByText('Update Model DB')).toHaveLength(1);
    expect(providerRow('OpenRouter').textContent).not.toContain(
      'Update Model DB',
    );
    expect(providerRow('Groq').textContent).not.toContain('Update Model DB');
  });

  it('hides the global refresh button when no provider appears refresh-eligible', async () => {
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        settings: settingsPayload({ eligibleProvider: false }),
      }),
    );

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openProvidersPanel();

    expect(buttonsByText('Update Model DB')).toHaveLength(0);
  });

  it('refreshes the global model database, shows loading and success, and reloads model list', async () => {
    let resolveRefresh;
    const refreshPromise = new Promise((resolve) => {
      resolveRefresh = resolve;
    });
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        settings: settingsPayload({ includeSecondEligibleProvider: true }),
        refreshResult: refreshPromise,
      }),
    );

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openProvidersPanel();

    buttonByText('Update Model DB').click();
    flushSync();

    expect(buttonByText('Updating…')).toBeTruthy();
    expect(rpcMock).toHaveBeenCalledWith('model.refresh_db');
    expect(
      rpcMock.mock.calls.some(
        (call) => call[0] === 'model.refresh_db' && call[1]?.provider_id,
      ),
    ).toBe(false);

    resolveRefresh({
      providers: [
        {
          provider_id: 'openrouter',
          model_count: 2,
          fetched_at: '2026-05-08T19:08:00+00:00',
        },
        {
          provider_id: 'groq',
          model_count: 3,
          fetched_at: '2026-05-08T19:08:00+00:00',
        },
      ],
      refreshed_count: 2,
      model_count: 5,
    });
    await waitForCondition(() =>
      document.body.textContent.includes('5 models'),
    );

    expect(document.body.textContent).toContain(
      'Model DB updated: 2 providers, 5 models available.',
    );
    expect(providerRow('OpenRouter').textContent).toContain(
      '2 models available.',
    );
    expect(providerRow('Groq').textContent).toContain('3 models available.');
    expect(rpcMock.mock.calls.some((call) => call[0] === 'model.list')).toBe(
      true,
    );
  });

  it('updates provider counts from the compatible single-provider refresh shape', async () => {
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        refreshResult: {
          provider_id: 'openrouter',
          model_count: 2,
          fetched_at: '2026-05-08T19:08:00+00:00',
        },
      }),
    );

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openProvidersPanel();

    buttonByText('Update Model DB').click();
    await waitForCondition(() =>
      document.body.textContent.includes('2 models'),
    );

    expect(document.body.textContent).toContain(
      'Model DB updated: 1 providers, 2 models available.',
    );
    expect(rpcMock.mock.calls.some((call) => call[0] === 'model.list')).toBe(
      true,
    );
  });

  it('shows refresh errors and skips model list reload on failure', async () => {
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        refreshError: new Error('fetch failed'),
      }),
    );

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openProvidersPanel();

    buttonByText('Update Model DB').click();
    await waitForCondition(() =>
      document.body.textContent.includes('fetch failed'),
    );

    expect(document.body.textContent).toContain(
      'Model DB could not be updated. fetch failed',
    );
    expect(rpcMock.mock.calls.some((call) => call[0] === 'model.list')).toBe(
      false,
    );
  });
});

async function openProvidersPanel() {
  await waitForCondition(() => buttonByText('Providers'));
  buttonByText('Providers').click();
  flushSync();
  await waitForCondition(() =>
    document.body.textContent.includes('OpenRouter'),
  );
}

function providerRow(providerName) {
  const rows = Array.from(document.body.querySelectorAll('.s-row'));
  const row = rows.find((item) => item.textContent.includes(providerName));
  expect(row).toBeTruthy();
  return row;
}

function buttonByText(label) {
  return Array.from(document.body.querySelectorAll('button')).find(
    (button) => button.textContent.trim() === label,
  );
}

function buttonsByText(label) {
  return Array.from(document.body.querySelectorAll('button')).filter(
    (button) => button.textContent.trim() === label,
  );
}

function createSettingsRpcMock(options = {}) {
  return async (method) => {
    if (method === 'settings.get') {
      return options.settings ?? settingsPayload();
    }

    if (method === 'model.refresh_db') {
      if (options.refreshError) {
        throw options.refreshError;
      }

      return options.refreshResult ?? refreshResult();
    }

    if (method === 'model.list') {
      return { models: [{ id: 'openrouter/fresh-model' }] };
    }

    throw new Error(`Unexpected RPC method: ${method}`);
  };
}

function settingsPayload(options = {}) {
  const openrouter = provider('openrouter', 'OpenRouter', '/models');

  if (options.eligibleProvider === false) {
    openrouter.credentials_configured = false;
    openrouter.status = 'missing_credentials';
  }

  const providers = [openrouter, provider('openai', 'OpenAI', null)];

  if (options.includeSecondEligibleProvider) {
    providers.push(provider('groq', 'Groq', '/models'));
  }

  return {
    general: {
      server: {
        listen_host: '127.0.0.1',
        listen_port: 8420,
        port_source: 'default',
      },
      data_directory: 'C:/data',
    },
    providers: {
      items: providers,
      custom_endpoints: { supported: false, items: [] },
    },
    skills: {
      default_directory: 'C:/data/skills',
      directories: [],
    },
    appearance: {
      language: 'en',
      available_languages: ['en'],
    },
  };
}

function provider(id, name, modelsEndpoint) {
  return {
    id,
    name,
    base_url: `https://${id}.example.test`,
    models_endpoint: modelsEndpoint,
    connections: [],
    credentials_configured: true,
    status: 'configured',
    model_count: 1,
    kind: 'remote',
    editable: false,
  };
}

function refreshResult() {
  return {
    providers: [
      {
        provider_id: 'openrouter',
        model_count: 2,
        fetched_at: '2026-05-08T19:08:00+00:00',
      },
    ],
    refreshed_count: 1,
    model_count: 2,
  };
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
