// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

const rpcMock = vi.fn();

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

describe('SettingsView OAuth providers', () => {
  let mountedComponent;
  let currentSettings;

  beforeEach(() => {
    document.body.innerHTML = '';
    Object.assign(navigator, {
      clipboard: {
        writeText: vi.fn().mockResolvedValue(undefined),
      },
    });
    init('en');
    mountedComponent = null;
    currentSettings = settingsPayload({ oauthConfigured: false });
    rpcMock.mockReset();
    rpcMock.mockImplementation(createSettingsRpcMock(() => currentSettings));
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }

    document.body.innerHTML = '';
    rpcMock.mockReset();
  });

  it('renders Connect for disconnected oauth connections and preserves api-key status rendering', async () => {
    mountedComponent = mountSettingsView();
    await openProvidersPanel();

    expect(providerRow('GitHub Copilot').textContent).toContain('Connect');
    expect(providerRow('OpenRouter').textContent).toContain('Configured');
  });

  it('starts provider.connect and shows the device flow dialog with the user code', async () => {
    mountedComponent = mountSettingsView();
    await openProvidersPanel();

    buttonByText('Connect').click();
    await waitForCondition(() =>
      document.body.textContent.includes('ABCD-1234'),
    );

    expect(rpcMock).toHaveBeenCalledWith('provider.connect', {
      provider_id: 'github-copilot',
      connection_id: 'github-copilot:oauth',
    });
    expect(document.body.textContent).toContain('Connect GitHub Copilot');
    expect(document.body.textContent).toContain(
      'https://github.com/login/device',
    );
  });

  it('copies the displayed device flow user code from an explicit copy control', async () => {
    mountedComponent = mountSettingsView();
    await openProvidersPanel();

    buttonByText('Connect').click();
    await waitForCondition(() => buttonByText('Copy'));
    buttonByText('Copy').click();
    await waitForCondition(() => document.body.textContent.includes('Copied'));

    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('ABCD-1234');
    expect(document.body.textContent).toContain('Device code copied.');
  });

  it('closes the dialog and shows a success toast after auth completion', async () => {
    mountedComponent = mountSettingsView();
    await openProvidersPanel();

    buttonByText('Connect').click();
    await waitForCondition(() =>
      document.body.textContent.includes('ABCD-1234'),
    );
    currentSettings = settingsPayload({ oauthConfigured: true });

    mountedComponent.handleProviderAuthCompleted({
      type: 'provider_auth_completed',
      payload: {
        provider_id: 'github-copilot',
        connection_id: 'github-copilot:oauth',
        success: true,
      },
    });
    await waitForCondition(() =>
      document.body.textContent.includes(
        'GitHub Copilot connected successfully',
      ),
    );

    expect(document.body.textContent).not.toContain('ABCD-1234');
    expect(providerRow('GitHub Copilot').textContent).toContain('Disconnect');
  });

  it('closes the dialog and shows an error toast after auth failure', async () => {
    mountedComponent = mountSettingsView();
    await openProvidersPanel();

    buttonByText('Connect').click();
    await waitForCondition(() =>
      document.body.textContent.includes('ABCD-1234'),
    );

    mountedComponent.handleProviderAuthCompleted({
      type: 'provider_auth_completed',
      payload: {
        provider_id: 'github-copilot',
        connection_id: 'github-copilot:oauth',
        success: false,
      },
    });
    await waitForCondition(() =>
      document.body.textContent.includes('Authorization failed or timed out'),
    );

    expect(document.body.textContent).not.toContain('ABCD-1234');
    expect(providerRow('GitHub Copilot').textContent).toContain('Connect');
  });

  it('cancels an active device flow through provider.disconnect', async () => {
    mountedComponent = mountSettingsView();
    await openProvidersPanel();

    buttonByText('Connect').click();
    await waitForCondition(() =>
      document.body.textContent.includes('ABCD-1234'),
    );
    buttonByText('Cancel').click();
    await waitForCondition(
      () => !document.body.textContent.includes('ABCD-1234'),
    );

    expect(rpcMock).toHaveBeenCalledWith('provider.disconnect', {
      provider_id: 'github-copilot',
      connection_id: 'github-copilot:oauth',
    });
  });

  it('renders Disconnect for connected oauth connections and refreshes settings after disconnect', async () => {
    currentSettings = settingsPayload({ oauthConfigured: true });
    mountedComponent = mountSettingsView();
    await openProvidersPanel();

    expect(providerRow('GitHub Copilot').textContent).toContain('Disconnect');

    currentSettings = settingsPayload({ oauthConfigured: false });
    buttonByText('Disconnect').click();
    await waitForCondition(() =>
      providerRow('GitHub Copilot').textContent.includes('Connect'),
    );

    expect(rpcMock).toHaveBeenCalledWith('provider.disconnect', {
      provider_id: 'github-copilot',
      connection_id: 'github-copilot:oauth',
    });
    expect(
      rpcMock.mock.calls.filter((call) => call[0] === 'settings.get'),
    ).toHaveLength(2);
  });
});

function mountSettingsView(props = {}) {
  return mount(SettingsView, { target: document.body, props });
}

async function openProvidersPanel() {
  await waitForCondition(() => buttonByText('Providers'));
  buttonByText('Providers').click();
  flushSync();
  await waitForCondition(() =>
    document.body.textContent.includes('GitHub Copilot'),
  );
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

function createSettingsRpcMock(getSettings) {
  return async (method, params) => {
    if (method === 'settings.get') {
      return getSettings();
    }

    if (method === 'provider.connect') {
      expect(params).toEqual({
        provider_id: 'github-copilot',
        connection_id: 'github-copilot:oauth',
      });

      return {
        user_code: 'ABCD-1234',
        verification_uri: 'https://github.com/login/device',
        expires_in: 900,
      };
    }

    if (method === 'provider.disconnect') {
      return {
        provider_id: params.provider_id,
        connection_id: params.connection_id,
        status: 'disconnected',
      };
    }

    throw new Error(`Unexpected RPC method: ${method}`);
  };
}

function settingsPayload({ oauthConfigured }) {
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
          id: 'github-copilot',
          name: 'GitHub Copilot',
          base_url: 'https://api.githubcopilot.com',
          models_endpoint: '/models',
          credentials_configured: oauthConfigured,
          status: oauthConfigured ? 'configured' : 'missing_credentials',
          model_count: 4,
          connections: [
            {
              id: 'github-copilot:oauth',
              type: 'oauth',
              label: 'Sign in with GitHub',
              configured: oauthConfigured,
            },
          ],
        },
        {
          id: 'openrouter',
          name: 'OpenRouter',
          base_url: 'https://openrouter.example.test',
          models_endpoint: '/models',
          credentials_configured: true,
          status: 'configured',
          model_count: 1,
          connections: [
            {
              id: 'openrouter:api-key',
              type: 'api_key',
              label: 'API Key',
              configured: true,
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
