// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { reactiveProps } from './_reactiveProps.svelte.js';
import { rpcBackedApiMock } from './apiMock.js';

const rpcMock = vi.fn();
const onReloadSettingsMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
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
    onReloadSettingsMock.mockReset();
    onReloadSettingsMock.mockResolvedValue(undefined);
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
  });

  it('reflects a provider change via a settings reload when modelsRefreshToken changes', async () => {
    const props = reactiveProps({
      settings: { providers: { items: [] } },
      visible: true,
      onReloadSettings: onReloadSettingsMock,
      modelsRefreshToken: 0,
    });
    mountedComponent = mount(SettingsProvidersPanel, {
      target: document.body,
      props,
    });
    flushSync();

    // The panel reads its display from the settings prop, so mount alone must
    // not trigger a reload.
    expect(onReloadSettingsMock).not.toHaveBeenCalled();

    props.modelsRefreshToken = 1;
    flushSync();
    await waitForCondition(() => onReloadSettingsMock.mock.calls.length >= 1);
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
        onReloadSettings: onReloadSettingsMock,
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
    expect(onReloadSettingsMock).toHaveBeenCalled();
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
