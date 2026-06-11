// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

const rpcMock = vi.fn();
let toastMock;

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', async () => {
  const actual = await vi.importActual('$lib/api.js');

  return {
    ...actual,
    rpc: (...args) => rpcMock(...args),
  };
});

const { default: SettingsView } = await import('../SettingsView.svelte');

describe('SettingsView provider API keys', () => {
  let mountedComponent;
  let currentSettings;
  let unsetKeyResult;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
    toastMock = vi.fn();
    currentSettings = settingsPayload({ anthropicConfigured: false });
    unsetKeyResult = {
      provider_id: 'openrouter',
      connection_id: 'openrouter:api-key',
      credential_key: 'OPENROUTER_API_KEY',
      removed: true,
      configured: false,
    };
    rpcMock.mockReset();
    rpcMock.mockImplementation(createRpcMock());
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }

    document.body.innerHTML = '';
    rpcMock.mockReset();
  });

  it('shows only connected providers with per-connection actions', async () => {
    mountedComponent = mountSettingsView();
    await openProvidersPanel();

    expect(document.body.textContent).toContain('OpenRouter');
    expect(document.body.textContent).not.toContain('Anthropic');

    const row = providerRow('OpenRouter');
    expect(row.textContent).toContain('Connected');
    expect(row.textContent).toContain('Replace key…');
    expect(row.textContent).toContain('Remove');
    expect(row.textContent).not.toContain('Missing credentials');
  });

  it('saves a new provider api key through the add modal', async () => {
    mountedComponent = mountSettingsView();
    await openProvidersPanel();

    buttonByText('Add provider').click();
    flushSync();

    buttonContaining('Anthropic').click();
    flushSync();

    currentSettings = settingsPayload({ anthropicConfigured: true });

    setInputValue('.provider-connect-modal input', 'sk-ant-test');
    buttonByText('Save key').click();
    await waitForCondition(() =>
      rpcMock.mock.calls.some((call) => call[0] === 'provider.set_key'),
    );

    expect(rpcMock).toHaveBeenCalledWith('provider.set_key', {
      provider_id: 'anthropic',
      connection_id: 'anthropic:api-key',
      value: 'sk-ant-test',
    });
    await waitForCondition(() =>
      toastMock.mock.calls.some(
        (call) => call[0]?.title === 'Anthropic connected successfully',
      ),
    );
    await waitForCondition(() =>
      document.body.textContent.includes('Anthropic'),
    );
    expect(modalRoot()).toBeUndefined();
  });

  it('replaces an existing api key through the scoped modal', async () => {
    mountedComponent = mountSettingsView();
    await openProvidersPanel();

    buttonByText('Replace key…').click();
    flushSync();

    expect(modalRoot()).toBeTruthy();
    expect(modalRoot().textContent).toContain('Connect OpenRouter');

    setInputValue('.provider-connect-modal input', 'sk-or-replacement');
    buttonByText('Save key').click();
    await waitForCondition(() =>
      rpcMock.mock.calls.some((call) => call[0] === 'provider.set_key'),
    );

    expect(rpcMock).toHaveBeenCalledWith('provider.set_key', {
      provider_id: 'openrouter',
      connection_id: 'openrouter:api-key',
      value: 'sk-or-replacement',
    });
  });

  it('removes an api key and reloads settings', async () => {
    mountedComponent = mountSettingsView();
    await openProvidersPanel();

    currentSettings = settingsPayload({
      anthropicConfigured: false,
      openrouterConfigured: false,
    });
    buttonByText('Remove').click();
    await waitForCondition(() =>
      rpcMock.mock.calls.some((call) => call[0] === 'provider.unset_key'),
    );

    expect(rpcMock).toHaveBeenCalledWith('provider.unset_key', {
      provider_id: 'openrouter',
      connection_id: 'openrouter:api-key',
    });
    await waitForCondition(() =>
      toastMock.mock.calls.some(
        (call) => call[0]?.title === 'API key removed.',
      ),
    );
    await waitForCondition(
      () => !document.body.textContent.includes('OpenRouter'),
    );
  });

  it('warns when the process environment still provides the removed key', async () => {
    unsetKeyResult = {
      ...unsetKeyResult,
      removed: false,
      configured: true,
    };
    mountedComponent = mountSettingsView();
    await openProvidersPanel();

    buttonByText('Remove').click();
    await waitForCondition(() => toastMock.mock.calls.length > 0);

    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({
        variant: 'warn',
      }),
    );
  });

  function createRpcMock() {
    return async (method, params) => {
      if (method === 'settings.get') {
        return currentSettings;
      }

      if (method === 'provider.set_key') {
        return {
          provider_id: params.provider_id,
          connection_id: params.connection_id,
          credential_key: 'KEY',
          configured: true,
        };
      }

      if (method === 'provider.unset_key') {
        return unsetKeyResult;
      }

      throw new Error(`Unexpected RPC method: ${method}`);
    };
  }
});

function mountSettingsView(props = {}) {
  return mount(SettingsView, {
    target: document.body,
    props: { onToast: toastMock, ...props },
  });
}

async function openProvidersPanel() {
  await waitForCondition(() => buttonByText('Providers'));
  buttonByText('Providers').click();
  flushSync();
  await waitForCondition(() => buttonByText('Add provider'));
}

function modalRoot() {
  return Array.from(
    document.body.querySelectorAll('.provider-connect-modal'),
  )[0];
}

function providerRow(providerName) {
  const rows = Array.from(document.body.querySelectorAll('.s-provider-card'));
  const row = rows.find((item) => item.textContent.includes(providerName));
  expect(row).toBeTruthy();
  return row;
}

function buttonByText(label) {
  return Array.from(document.body.querySelectorAll('button')).find(
    (button) => button.textContent.trim() === label,
  );
}

function buttonContaining(label) {
  const button = Array.from(document.body.querySelectorAll('button')).find(
    (candidate) => candidate.textContent.includes(label),
  );
  expect(button).toBeTruthy();
  return button;
}

function setInputValue(selector, value) {
  const input = document.body.querySelector(selector);
  expect(input).toBeTruthy();
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

function settingsPayload({
  anthropicConfigured = false,
  openrouterConfigured = true,
} = {}) {
  return {
    general: {
      server: {
        listen_host: '127.0.0.1',
        listen_port: 8420,
      },
      data_directory: 'C:/data',
    },
    providers: {
      items: [
        {
          id: 'anthropic',
          name: 'Anthropic',
          base_url: 'https://api.anthropic.com/v1',
          models_endpoint: null,
          credentials_configured: anthropicConfigured,
          status: anthropicConfigured ? 'configured' : 'missing_credentials',
          model_count: 1,
          connections: [
            {
              id: 'anthropic:api-key',
              type: 'api_key',
              label: 'API Key',
              configured: anthropicConfigured,
              credential_key: 'ANTHROPIC_API_KEY',
            },
          ],
        },
        {
          id: 'openrouter',
          name: 'OpenRouter',
          base_url: 'https://openrouter.example.test',
          models_endpoint: null,
          credentials_configured: openrouterConfigured,
          status: openrouterConfigured ? 'configured' : 'missing_credentials',
          model_count: 1,
          connections: [
            {
              id: 'openrouter:api-key',
              type: 'api_key',
              label: 'API Key',
              configured: openrouterConfigured,
              credential_key: 'OPENROUTER_API_KEY',
            },
          ],
        },
      ],
    },
    skills: {
      default_directory: 'C:/data/skills',
      directories: [],
    },
    subagents: {},
    appearance: {
      language: 'en',
      available_languages: ['en'],
    },
  };
}

async function waitForCondition(check, attempts = 30) {
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
