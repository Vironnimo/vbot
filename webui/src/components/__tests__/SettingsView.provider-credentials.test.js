// @vitest-environment jsdom

// Provider credentials in Settings: API keys per account and the OAuth device
// flow. Each test swaps `currentSettings` to what the server reports after
// the credential RPC, as the real backend does.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount } from 'svelte';

import { t } from '../../lib/i18n.js';
import {
  buttonByText,
  cleanupSettingsViewHarness,
  createSettingsRpcMock,
  openProvidersPanel,
  providerRow,
  resetSettingsViewHarness,
  rpcMock,
  setInputValue,
  settingsPayload,
  waitForCondition,
} from './SettingsView.support.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: SettingsView } = await import('../SettingsView.svelte');

const COPILOT = {
  provider_id: 'github-copilot',
  connection_id: 'github-copilot:oauth',
};
const OPENROUTER_KEY = {
  provider_id: 'openrouter',
  connection_id: 'openrouter:api-key',
};
const USER_CODE = 'ABCD-1234';
const ACCOUNT_INPUT = '.provider-connect-modal input[type="text"]';

function account(id, source, credentialKey, usable = true) {
  return { id, usable, source, credential_key: credentialKey };
}

function apiKeyProvider(id, name, accounts) {
  const configured = accounts.length > 0;
  return {
    id,
    name,
    base_url: `https://${id}.example.test`,
    models_endpoint: null,
    credentials_configured: configured,
    status: configured ? 'configured' : 'missing_credentials',
    model_count: 1,
    connections: [
      {
        id: `${id}:api-key`,
        type: 'api_key',
        label: 'API Key',
        configured,
        credential_key: `${id.toUpperCase()}_API_KEY`,
        accounts,
      },
    ],
  };
}

function credentialSettings({
  copilot = false,
  anthropic = false,
  openrouterAccounts = [account('default', 'data_dir', 'OPENROUTER_API_KEY')],
} = {}) {
  const settings = settingsPayload();
  settings.providers.items = [
    {
      id: 'github-copilot',
      name: 'GitHub Copilot',
      base_url: 'https://api.githubcopilot.com',
      models_endpoint: '/models',
      credentials_configured: copilot,
      status: copilot ? 'configured' : 'missing_credentials',
      model_count: 4,
      connections: [
        {
          id: 'github-copilot:oauth',
          type: 'oauth',
          label: 'Sign in with GitHub',
          configured: copilot,
          connectable: true,
          accounts: copilot ? [account('default', 'oauth', '')] : [],
        },
      ],
    },
    apiKeyProvider(
      'anthropic',
      'Anthropic',
      anthropic ? [account('default', 'data_dir', 'ANTHROPIC_API_KEY')] : [],
    ),
    apiKeyProvider('openrouter', 'OpenRouter', openrouterAccounts),
  ];
  return settings;
}

function modalRoot() {
  return document.body.querySelector('.provider-connect-modal') ?? undefined;
}

function rowButton(providerName, label) {
  const button = Array.from(
    providerRow(providerName).querySelectorAll('button'),
  ).find((candidate) => candidate.textContent.trim() === label);
  expect(button, `${label} in ${providerName}`).toBeTruthy();
  return button;
}

function successToasts(toastMock) {
  return toastMock.mock.calls.filter(([toast]) => toast?.variant === 'success');
}

describe('SettingsView provider credentials', () => {
  let mountedComponent;
  let currentSettings;
  let unsetKeyResult;
  let toastMock;

  beforeEach(() => {
    resetSettingsViewHarness();
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
    });
    toastMock = vi.fn();
    currentSettings = credentialSettings();
    unsetKeyResult = {
      ...OPENROUTER_KEY,
      account: 'default',
      credential_key: 'OPENROUTER_API_KEY',
      removed: true,
      configured: false,
    };
    const backend = createSettingsRpcMock();
    rpcMock.mockImplementation(async (method, params) => {
      switch (method) {
        case 'settings.get':
          return structuredClone(currentSettings);
        case 'provider.connect':
          return {
            user_code: USER_CODE,
            verification_uri: 'https://github.com/login/device',
            expires_in: 900,
            account: params.account,
          };
        case 'provider.disconnect':
          return { ...params, status: 'disconnected' };
        case 'provider.set_key':
          return { ...params, credential_key: 'KEY', configured: true };
        case 'provider.unset_key':
          return unsetKeyResult;
        default:
          return backend(method, params);
      }
    });
    mountedComponent = null;
  });

  afterEach(async () => {
    mountedComponent = await cleanupSettingsViewHarness(mountedComponent);
  });

  async function openProviders() {
    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: { onToast: toastMock },
    });
    flushSync();
    await openProvidersPanel();
  }

  describe('API keys', () => {
    it('shows only connected providers with per-connection actions', async () => {
      await openProviders();

      expect(document.querySelectorAll('.s-provider-card')).toHaveLength(1);
      expect(
        providerRow('OpenRouter').querySelector('.chip.success'),
      ).toBeTruthy();
      expect(rowButton('OpenRouter', 'Replace key…')).toBeTruthy();
      expect(rowButton('OpenRouter', 'Remove')).toBeTruthy();
    });

    it('saves a new provider API key through the add modal', async () => {
      await openProviders();
      buttonByText('Add provider').click();
      flushSync();
      Array.from(modalRoot().querySelectorAll('button'))
        .find((button) => button.textContent.includes('Anthropic'))
        .click();
      flushSync();

      currentSettings = credentialSettings({ anthropic: true });
      setInputValue('.provider-connect-modal input', 'sk-ant-test');
      buttonByText('Save key').click();
      await waitForCondition(() => successToasts(toastMock).length > 0);

      expect(rpcMock).toHaveBeenCalledWith('provider.set_key', {
        provider_id: 'anthropic',
        connection_id: 'anthropic:api-key',
        account: 'default',
        value: 'sk-ant-test',
      });
      await waitForCondition(() =>
        document.body.textContent.includes('Anthropic'),
      );
      expect(providerRow('Anthropic')).toBeTruthy();
      expect(modalRoot()).toBeUndefined();
    });

    it('replaces an existing API key through the account-scoped modal', async () => {
      await openProviders();
      rowButton('OpenRouter', 'Replace key…').click();
      flushSync();

      setInputValue('.provider-connect-modal input', 'sk-or-replacement');
      const accountInput = document.body.querySelector(ACCOUNT_INPUT);
      expect(accountInput.value).toBe('default');
      expect(accountInput.disabled).toBe(true);
      buttonByText('Save key').click();
      await waitForCondition(() =>
        rpcMock.mock.calls.some((call) => call[0] === 'provider.set_key'),
      );

      expect(rpcMock).toHaveBeenCalledWith('provider.set_key', {
        ...OPENROUTER_KEY,
        account: 'default',
        value: 'sk-or-replacement',
      });
    });

    it.each([
      ['removes the key and reloads settings', true, 'success'],
      ['warns when the process environment still provides it', false, 'warn'],
    ])('%s', async (_label, removed, variant) => {
      unsetKeyResult = { ...unsetKeyResult, removed, configured: !removed };
      await openProviders();
      if (removed) {
        currentSettings = credentialSettings({ openrouterAccounts: [] });
      }

      rowButton('OpenRouter', 'Remove').click();
      await waitForCondition(() => toastMock.mock.calls.length > 0);

      expect(rpcMock).toHaveBeenCalledWith('provider.unset_key', {
        ...OPENROUTER_KEY,
        account: 'default',
      });
      expect(toastMock).toHaveBeenCalledWith(
        expect.objectContaining({ variant }),
      );
      if (removed) {
        await waitForCondition(
          () => !document.body.textContent.includes('OpenRouter'),
        );
      }
    });

    it('renders accounts with status, source and actions, and removes a named account', async () => {
      currentSettings = credentialSettings({
        openrouterAccounts: [
          account('default', 'process_env', 'OPENROUTER_API_KEY'),
          account('work', 'data_dir', 'OPENROUTER_API_KEY__WORK', false),
        ],
      });
      unsetKeyResult = { ...unsetKeyResult, account: 'work' };
      await openProviders();

      const rows = Array.from(
        providerRow('OpenRouter').querySelectorAll('.s-connection-account-row'),
      );
      expect(rows).toHaveLength(2);
      const [defaultRow, workRow] = rows;
      expect(
        defaultRow.querySelector('.s-connection-account-id').textContent,
      ).toContain('Default');
      expect(defaultRow.querySelector('.chip.success')).toBeTruthy();
      expect(
        workRow.querySelector('.s-connection-account-id').textContent,
      ).toContain('work');
      expect(workRow.querySelector('.chip.warn')).toBeTruthy();
      for (const row of rows) {
        expect(row.querySelector('.s-connection-account-source')).toBeTruthy();
      }
      expect(rowButton('OpenRouter', 'Add account…')).toBeTruthy();

      // The process-env account cannot be removed; the data-dir one can.
      const removeButton = (row) =>
        Array.from(row.querySelectorAll('button')).find(
          (button) => button.textContent.trim() === 'Remove',
        );
      expect(removeButton(defaultRow).disabled).toBe(true);
      expect(removeButton(workRow).disabled).toBe(false);
      removeButton(workRow).click();
      await waitForCondition(() =>
        rpcMock.mock.calls.some((call) => call[0] === 'provider.unset_key'),
      );
      expect(rpcMock).toHaveBeenCalledWith('provider.unset_key', {
        ...OPENROUTER_KEY,
        account: 'work',
      });
    });

    it('adds a named account after rejecting an invalid account name', async () => {
      await openProviders();
      rowButton('OpenRouter', 'Add account…').click();
      flushSync();

      setInputValue(
        '.provider-connect-modal input[type="password"]',
        'sk-or-second',
      );
      setInputValue(ACCOUNT_INPUT, 'Not Valid');
      expect(
        document.querySelector(ACCOUNT_INPUT).getAttribute('aria-invalid'),
      ).toBe('true');
      expect(buttonByText('Save key').disabled).toBe(true);

      setInputValue(ACCOUNT_INPUT, 'work');
      // The stored-key hint previews the account-derived credential key.
      expect(modalRoot().textContent).toContain('OPENROUTER_API_KEY__WORK');
      buttonByText('Save key').click();
      await waitForCondition(() =>
        rpcMock.mock.calls.some((call) => call[0] === 'provider.set_key'),
      );
      expect(rpcMock).toHaveBeenCalledWith('provider.set_key', {
        ...OPENROUTER_KEY,
        account: 'work',
        value: 'sk-or-second',
      });
    });
  });

  describe('OAuth device flow', () => {
    function completeAuth(accountId, success) {
      mountedComponent.handleProviderAuthCompleted({
        type: 'provider_auth_completed',
        payload: { ...COPILOT, account: accountId, success },
      });
      flushSync();
    }

    async function startDeviceFlowFromAddModal() {
      await openProviders();
      buttonByText('Add provider').click();
      flushSync();
      Array.from(modalRoot().querySelectorAll('button'))
        .find((button) => button.textContent.includes('GitHub Copilot'))
        .click();
      flushSync();
      await waitForCondition(() => buttonByText('Connect'));
      buttonByText('Connect').click();
      await waitForCondition(() =>
        document.body.textContent.includes(USER_CODE),
      );
    }

    it('hides disconnected OAuth providers and offers them in the add modal', async () => {
      await openProviders();

      expect(document.body.textContent).not.toContain('GitHub Copilot');
      expect(
        providerRow('OpenRouter').querySelector('.chip.success'),
      ).toBeTruthy();
      buttonByText('Add provider').click();
      flushSync();
      expect(modalRoot().textContent).toContain('GitHub Copilot');
      expect(modalRoot().textContent).not.toContain('OpenRouter');
    });

    it('starts provider.connect, shows the device code, and copies it', async () => {
      await startDeviceFlowFromAddModal();

      expect(rpcMock).toHaveBeenCalledWith('provider.connect', {
        ...COPILOT,
        account: 'default',
      });
      expect(document.body.textContent).toContain(
        t('settings.providers.device_flow.title', {
          provider: 'GitHub Copilot',
        }),
      );
      expect(document.body.textContent).toContain(
        'https://github.com/login/device',
      );

      buttonByText('Copy').click();
      await waitForCondition(() => successToasts(toastMock).length > 0);
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith(USER_CODE);
    });

    it('closes the modal and shows a success toast after auth completion', async () => {
      await startDeviceFlowFromAddModal();
      currentSettings = credentialSettings({ copilot: true });

      completeAuth('default', true);
      await waitForCondition(() => successToasts(toastMock).length > 0);
      await waitForCondition(
        () => !document.body.textContent.includes(USER_CODE),
      );
      await waitForCondition(() =>
        providerRow('GitHub Copilot').textContent.includes('Disconnect'),
      );
    });

    it('keeps the modal open with an inline error after auth failure', async () => {
      await startDeviceFlowFromAddModal();

      completeAuth('default', false);
      await waitForCondition(() =>
        modalRoot()?.querySelector('[role="alert"]'),
      );
      expect(document.body.textContent).not.toContain(USER_CODE);
    });

    it('cancels an active device flow through provider.disconnect and closes the modal', async () => {
      await startDeviceFlowFromAddModal();

      Array.from(modalRoot().querySelectorAll('button'))
        .find((button) => button.textContent.trim() === 'Cancel')
        .click();
      await waitForCondition(
        () => !document.body.textContent.includes(USER_CODE),
      );
      expect(rpcMock).toHaveBeenCalledWith('provider.disconnect', {
        ...COPILOT,
        account: 'default',
      });
      expect(modalRoot()).toBeUndefined();
    });

    it('disconnects a connected OAuth connection and refreshes settings', async () => {
      currentSettings = credentialSettings({ copilot: true });
      await openProviders();

      currentSettings = credentialSettings();
      rowButton('GitHub Copilot', 'Disconnect').click();
      await waitForCondition(
        () => !document.body.textContent.includes('GitHub Copilot'),
      );
      expect(rpcMock).toHaveBeenCalledWith('provider.disconnect', {
        ...COPILOT,
        account: 'default',
      });
      expect(
        rpcMock.mock.calls.filter((call) => call[0] === 'settings.get'),
      ).toHaveLength(2);
    });

    it('validates the account name, then completes only on the account that started the flow', async () => {
      currentSettings = credentialSettings({ copilot: true });
      await openProviders();
      rowButton('GitHub Copilot', 'Add account…').click();
      flushSync();
      await waitForCondition(() => buttonByText('Connect'));

      setInputValue(ACCOUNT_INPUT, 'Not Valid');
      expect(
        document.querySelector(ACCOUNT_INPUT).getAttribute('aria-invalid'),
      ).toBe('true');
      expect(buttonByText('Connect').disabled).toBe(true);

      setInputValue(ACCOUNT_INPUT, 'work');
      buttonByText('Connect').click();
      await waitForCondition(() =>
        document.body.textContent.includes(USER_CODE),
      );
      expect(
        rpcMock.mock.calls.filter((call) => call[0] === 'provider.connect'),
      ).toEqual([['provider.connect', { ...COPILOT, account: 'work' }]]);

      // The event for another account must not complete this flow.
      completeAuth('default', true);
      expect(document.body.textContent).toContain(USER_CODE);
      expect(successToasts(toastMock)).toHaveLength(0);

      completeAuth('work', true);
      await waitForCondition(() => successToasts(toastMock).length > 0);
      await waitForCondition(
        () => !document.body.textContent.includes(USER_CODE),
      );
    });
  });
});
