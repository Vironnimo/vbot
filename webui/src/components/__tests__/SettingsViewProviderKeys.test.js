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
      account: 'default',
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
      account: 'default',
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

    const accountInput = document.body.querySelector(
      '.provider-connect-modal input[type="text"]',
    );
    expect(accountInput.value).toBe('default');
    expect(accountInput.disabled).toBe(true);

    buttonByText('Save key').click();
    await waitForCondition(() =>
      rpcMock.mock.calls.some((call) => call[0] === 'provider.set_key'),
    );

    expect(rpcMock).toHaveBeenCalledWith('provider.set_key', {
      provider_id: 'openrouter',
      connection_id: 'openrouter:api-key',
      account: 'default',
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
      account: 'default',
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

  it('renders multiple accounts with per-account status, source, and actions', async () => {
    currentSettings = settingsPayload({
      openrouterAccounts: [
        {
          id: 'default',
          usable: true,
          source: 'process_env',
          credential_key: 'OPENROUTER_API_KEY',
        },
        {
          id: 'work',
          usable: false,
          source: 'data_dir',
          credential_key: 'OPENROUTER_API_KEY__WORK',
        },
      ],
    });
    mountedComponent = mountSettingsView();
    await openProvidersPanel();

    const row = providerRow('OpenRouter');
    const accountRows = Array.from(
      row.querySelectorAll('.s-connection-account-row'),
    );
    expect(accountRows).toHaveLength(2);

    expect(accountRows[0].textContent).toContain('Default');
    expect(accountRows[0].textContent).toContain('Connected');
    expect(accountRows[0].textContent).toContain('Process env');
    expect(accountRows[1].textContent).toContain('work');
    expect(accountRows[1].textContent).toContain('Not usable');
    expect(accountRows[1].textContent).toContain('.env file');

    // The process-env account cannot be removed; the data-dir one can.
    expect(removeButton(accountRows[0]).disabled).toBe(true);
    expect(removeButton(accountRows[1]).disabled).toBe(false);

    expect(row.textContent).toContain('Add account…');
  });

  it('removes a named account with the account in the unset payload', async () => {
    currentSettings = settingsPayload({
      openrouterAccounts: [
        {
          id: 'default',
          usable: true,
          source: 'data_dir',
          credential_key: 'OPENROUTER_API_KEY',
        },
        {
          id: 'work',
          usable: true,
          source: 'data_dir',
          credential_key: 'OPENROUTER_API_KEY__WORK',
        },
      ],
    });
    unsetKeyResult = {
      ...unsetKeyResult,
      account: 'work',
    };
    mountedComponent = mountSettingsView();
    await openProvidersPanel();

    const row = providerRow('OpenRouter');
    const accountRows = Array.from(
      row.querySelectorAll('.s-connection-account-row'),
    );
    removeButton(accountRows[1]).click();
    await waitForCondition(() =>
      rpcMock.mock.calls.some((call) => call[0] === 'provider.unset_key'),
    );

    expect(rpcMock).toHaveBeenCalledWith('provider.unset_key', {
      provider_id: 'openrouter',
      connection_id: 'openrouter:api-key',
      account: 'work',
    });
  });

  it('adds a named account through the connection-scoped modal', async () => {
    mountedComponent = mountSettingsView();
    await openProvidersPanel();

    buttonByText('Add account…').click();
    flushSync();

    expect(modalRoot()).toBeTruthy();
    setInputValue(
      '.provider-connect-modal input[type="password"]',
      'sk-or-second',
    );
    setInputValue('.provider-connect-modal input[type="text"]', 'work');

    // The stored-key hint previews the account-derived credential key.
    expect(modalRoot().textContent).toContain('OPENROUTER_API_KEY__WORK');

    buttonByText('Save key').click();
    await waitForCondition(() =>
      rpcMock.mock.calls.some((call) => call[0] === 'provider.set_key'),
    );

    expect(rpcMock).toHaveBeenCalledWith('provider.set_key', {
      provider_id: 'openrouter',
      connection_id: 'openrouter:api-key',
      account: 'work',
      value: 'sk-or-second',
    });
  });

  it('blocks an invalid account name in the key form with an inline error', async () => {
    mountedComponent = mountSettingsView();
    await openProvidersPanel();

    buttonByText('Add account…').click();
    flushSync();

    setInputValue('.provider-connect-modal input[type="password"]', 'sk-x');
    setInputValue('.provider-connect-modal input[type="text"]', 'Not Valid');

    expect(modalRoot().textContent).toContain(
      'Account names use 1–32 lowercase letters, digits, or underscores',
    );
    expect(buttonByText('Save key').disabled).toBe(true);
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
          account: params.account,
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

function removeButton(scope) {
  const button = Array.from(scope.querySelectorAll('button')).find(
    (candidate) => candidate.textContent.trim() === 'Remove',
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
  openrouterAccounts = null,
} = {}) {
  const resolvedOpenrouterAccounts =
    openrouterAccounts ??
    (openrouterConfigured
      ? [
          {
            id: 'default',
            usable: true,
            source: 'data_dir',
            credential_key: 'OPENROUTER_API_KEY',
          },
        ]
      : []);

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
              accounts: anthropicConfigured
                ? [
                    {
                      id: 'default',
                      usable: true,
                      source: 'data_dir',
                      credential_key: 'ANTHROPIC_API_KEY',
                    },
                  ]
                : [],
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
              accounts: resolvedOpenrouterAccounts,
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
