// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { reactiveProps } from './_reactiveProps.svelte.js';
import { rpcBackedApiMock } from './apiMock.js';

const rpcMock = vi.fn();
const onRefreshProviderSettingsMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('svelte/reactivity', async () => {
  return import('../../../node_modules/svelte/src/reactivity/index-client.js');
});

vi.mock('$lib/api.js', () => rpcBackedApiMock(rpcMock));

const { default: SettingsProvidersPanel } =
  await import('../settings/SettingsProvidersPanel.svelte');

describe('SettingsProvidersPanel', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    rpcMock.mockReset();
    rpcMock.mockResolvedValue({});
    onRefreshProviderSettingsMock.mockReset();
    onRefreshProviderSettingsMock.mockResolvedValue(undefined);
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
  });

  it('explains shared OpenCode edits and removes the selected shared Account', async () => {
    const items = ['opencode-go', 'opencode-zen'].map((id) => ({
      id,
      name: id === 'opencode-go' ? 'OpenCode Go' : 'OpenCode Zen',
      connections: [
        {
          id: `${id}:api-key`,
          type: 'api_key',
          label: 'API Key',
          credential_key: 'OPENCODE_API_KEY',
          configured: true,
          usable: true,
          accounts: [{ id: 'work', usable: true, source: 'data_dir' }],
        },
      ],
    }));
    mountedComponent = mount(SettingsProvidersPanel, {
      target: document.body,
      props: {
        settings: { providers: { items } },
        visible: true,
        onRefreshProviderSettings: onRefreshProviderSettingsMock,
      },
    });
    flushSync();
    expect(document.querySelectorAll('.s-provider-card')).toHaveLength(2);
    findButton('Replace key…').click();
    flushSync();
    const note = document.querySelector(
      '.provider-connect-modal [role="note"]',
    );
    expect(note?.textContent).toContain('OpenCode Go');
    expect(note?.textContent).toContain('Zen');
    const input = document.querySelector('#provider-api-key');
    input.value = 'replacement-test';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    findButton('Save key').click();
    await waitForCondition(
      () => onRefreshProviderSettingsMock.mock.calls.length > 0,
    );
    expect(rpcMock).toHaveBeenCalledWith('provider.set_key', {
      provider_id: 'opencode-go',
      connection_id: 'opencode-go:api-key',
      account: 'work',
      value: 'replacement-test',
    });
    findButton('Remove shared key').click();
    await waitForCondition(() =>
      rpcMock.mock.calls.some(([method]) => method === 'provider.unset_key'),
    );
    expect(rpcMock).toHaveBeenCalledWith('provider.unset_key', {
      provider_id: 'opencode-go',
      connection_id: 'opencode-go:api-key',
      account: 'work',
    });
  });

  it('reflects a provider change via a settings reload when modelsRefreshToken changes', async () => {
    const props = reactiveProps({
      settings: { providers: { items: [] } },
      visible: true,
      onRefreshProviderSettings: onRefreshProviderSettingsMock,
      modelsRefreshToken: 0,
    });
    mountedComponent = mount(SettingsProvidersPanel, {
      target: document.body,
      props,
    });
    flushSync();

    // The panel reads its display from the settings prop, so mount alone must
    // not trigger a reload.
    expect(onRefreshProviderSettingsMock).not.toHaveBeenCalled();

    props.modelsRefreshToken = 1;
    flushSync();
    await waitForCondition(
      () => onRefreshProviderSettingsMock.mock.calls.length >= 1,
    );
  });

  it('toggles provider details from the whole row without double-toggling the button', () => {
    mountedComponent = mount(SettingsProvidersPanel, {
      target: document.body,
      props: {
        settings: {
          providers: {
            items: [
              {
                id: 'local-ai',
                name: 'Local AI',
                adapter: 'openai_compatible',
                base_url: 'http://127.0.0.1:8080/v1',
                auth: 'api_key',
                custom: true,
                editable: true,
                credentials_configured: false,
                usable: false,
                model_count: 0,
                connections: [],
              },
            ],
          },
        },
        visible: true,
        onRefreshProviderSettings: onRefreshProviderSettingsMock,
      },
    });
    flushSync();

    const details = findButton('Details for local-ai', true);
    const sub = document.querySelector('.s-disclosure-sub');
    expect(details.getAttribute('aria-expanded')).toBe('false');
    expect(sub.hidden).toBe(true);

    document.querySelector('.s-provider-head .s-row-label').click();
    flushSync();
    expect(details.getAttribute('aria-expanded')).toBe('true');
    expect(sub.hidden).toBe(false);

    details.click();
    flushSync();
    expect(details.getAttribute('aria-expanded')).toBe('false');
    expect(sub.hidden).toBe(true);
  });

  it('keeps an unconfigured Custom Provider visible and deletes it through RPC', async () => {
    const customSettings = {
      id: 'local-ai',
      name: 'Local AI',
      adapter: 'openai_compatible',
      base_url: 'http://127.0.0.1:8080/v1',
      auth: 'api_key',
      models_endpoint: '/models',
      defaults: {},
      models: {},
      credentials_configured: false,
      usable: false,
      model_count: 0,
    };
    mountedComponent = mount(SettingsProvidersPanel, {
      target: document.body,
      props: {
        settings: {
          providers: {
            items: [
              {
                ...customSettings,
                custom: true,
                editable: true,
                connections: [
                  {
                    id: 'local-ai:default',
                    type: 'api_key',
                    label: 'Default',
                    configured: false,
                    usable: false,
                    accounts: [],
                  },
                ],
              },
            ],
            custom_endpoints: {
              supported: true,
              items: [customSettings],
            },
          },
        },
        visible: true,
        onRefreshProviderSettings: onRefreshProviderSettingsMock,
      },
    });
    flushSync();

    expect(document.body.textContent).toContain('Local AI');
    findButton('Details for local-ai', true).click();
    flushSync();
    findButton('Delete').click();
    flushSync();
    const deleteButtons = [...document.querySelectorAll('button')].filter(
      (element) => element.textContent.trim() === 'Delete',
    );
    deleteButtons.at(-1).click();

    await waitForCondition(() =>
      rpcMock.mock.calls.some(
        ([method]) => method === 'provider.custom_delete',
      ),
    );
    expect(rpcMock).toHaveBeenCalledWith('provider.custom_delete', {
      provider_id: 'local-ai',
    });
    expect(onRefreshProviderSettingsMock).toHaveBeenCalled();
  });

  it('adds a fresh keyless local provider through Add provider', async () => {
    const onToast = vi.fn();
    rpcMock.mockImplementation((method) => {
      if (method === 'connection.set_enabled') {
        return Promise.resolve({ added: true, enabled: true, reachable: true });
      }
      if (method === 'model.list') {
        return Promise.resolve({ models: [] });
      }
      return Promise.resolve({});
    });
    mountedComponent = mount(SettingsProvidersPanel, {
      target: document.body,
      props: {
        settings: {
          providers: {
            items: [
              {
                id: 'lmstudio',
                name: 'LM Studio',
                base_url: 'http://localhost:1234',
                connections: [
                  {
                    id: 'lmstudio:local',
                    type: 'none',
                    label: 'Local',
                    added: false,
                    configured: true,
                    enabled: false,
                    usable: false,
                    accounts: [{ id: 'default', usable: true, source: 'none' }],
                  },
                ],
              },
            ],
          },
        },
        visible: true,
        onRefreshProviderSettings: onRefreshProviderSettingsMock,
        onToast,
      },
    });
    flushSync();

    expect(document.body.textContent).not.toContain('LM Studio');
    [...document.querySelectorAll('button')]
      .filter((button) => button.textContent.trim() === 'Add provider')
      .at(-1)
      .click();
    flushSync();
    [...document.querySelectorAll('button')]
      .find((button) => button.textContent.includes('LM Studio'))
      .click();
    flushSync();
    expect(document.body.textContent).toContain(
      'Models are discovered now and loaded only when used.',
    );
    [...document.querySelectorAll('button')]
      .filter((button) => button.textContent.trim() === 'Add provider')
      .at(-1)
      .click();

    await waitForCondition(() =>
      rpcMock.mock.calls.some(
        ([method]) => method === 'connection.set_enabled',
      ),
    );
    expect(rpcMock).toHaveBeenCalledWith('connection.set_enabled', {
      provider_id: 'lmstudio',
      connection_id: 'lmstudio:local',
      enabled: true,
    });
    expect(onRefreshProviderSettingsMock).toHaveBeenCalled();
    expect(onToast).toHaveBeenCalledWith(
      expect.objectContaining({
        variant: 'success',
      }),
    );
  });
});

function findButton(label, aria = false) {
  return [...document.querySelectorAll('button')].find((element) =>
    aria
      ? element.getAttribute('aria-label') === label
      : element.textContent.trim() === label,
  );
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
