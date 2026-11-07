// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import {
  createAutosaveCoordinator,
  createAutosaveParticipant,
} from '../../lib/autosave.js';
import { init, t } from '../../lib/i18n.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';
import { rpcBackedApiMock } from './apiMock.support.js';

const rpcMock = vi.fn();
const onRefreshProviderSettingsMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => rpcBackedApiMock(rpcMock));

const { default: SettingsProvidersPanel } =
  await import('../settings/SettingsProvidersPanel.svelte');
const { default: AutosaveContextHost } =
  await import('./AutosaveContextHost.support.svelte');

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
    const coordinator = createAutosaveCoordinator();
    mountedComponent = mount(AutosaveContextHost, {
      target: document.body,
      props: {
        component: SettingsProvidersPanel,
        componentProps: {
          settings: { providers: { items } },
          visible: true,
          onRefreshProviderSettings: onRefreshProviderSettingsMock,
        },
        coordinator,
      },
    });
    flushSync();
    expect(document.querySelectorAll('.s-provider-card')).toHaveLength(2);
    // Each OpenCode row keeps its billing note behind a "?" hint.
    for (const name of ['OpenCode Go', 'OpenCode Zen']) {
      expect(
        document.querySelector(
          `.s-provider-head .info-hint[aria-label="Billing for ${name}"]`,
        ),
      ).toBeTruthy();
    }
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
    // Removing a key can disconnect its Provider and with it remove an
    // editor in its row, so pending edits save first.
    let pendingEdit = true;
    coordinator.register(
      createAutosaveParticipant({
        getSnapshot: () => pendingEdit,
        hasChanges: () => pendingEdit,
        save: async () => {
          expect(rpcMock).not.toHaveBeenCalledWith(
            'provider.unset_key',
            expect.anything(),
          );
          pendingEdit = false;
          return true;
        },
      }),
    );
    findButton('Remove shared key').click();
    await waitForCondition(() =>
      rpcMock.mock.calls.some(([method]) => method === 'provider.unset_key'),
    );
    expect(pendingEdit).toBe(false);
    expect(rpcMock).toHaveBeenCalledWith('provider.unset_key', {
      provider_id: 'opencode-go',
      connection_id: 'opencode-go:api-key',
      account: 'work',
    });
  });

  it('tracks raw local Context input, waits for number completion, and preserves newer edits through failure and retry', async () => {
    vi.useFakeTimers();
    try {
      const modelId = 'ollama/local';
      const model = {
        id: modelId,
        model_id: 'local',
        provider_id: 'ollama',
        local: true,
        capabilities: { tools: true },
        context_window: 131072,
      };
      const settings = {
        providers: {
          items: [
            {
              id: 'ollama',
              name: 'Ollama',
              connections: [
                {
                  id: 'ollama:local',
                  type: 'none',
                  added: true,
                  configured: true,
                  enabled: true,
                  accounts: [{ id: 'default', usable: true, source: 'none' }],
                },
              ],
            },
          ],
        },
        local_models: { context_windows: {} },
      };
      let releaseFirst;
      const firstWrite = new Promise((resolve) => {
        releaseFirst = resolve;
      });
      let serverWindows = {};
      const writes = [];
      const props = reactiveProps({
        settings,
        visible: true,
        onRefreshProviderSettings: async () => {
          props.settings = {
            ...props.settings,
            local_models: { context_windows: serverWindows },
          };
        },
      });
      rpcMock.mockImplementation(async (method, params) => {
        if (method === 'model.list') return { models: [model] };
        if (method === 'settings.update') {
          writes.push(params);
          if (writes.length === 1) await firstWrite;
          if (writes.length === 2) throw new Error('test-write-refused');
          serverWindows = {
            ...serverWindows,
            ...params.local_models.context_windows,
          };
          return {
            ...settings,
            local_models: { context_windows: serverWindows },
          };
        }
        return {};
      });
      const coordinator = createAutosaveCoordinator();
      mountedComponent = mount(AutosaveContextHost, {
        target: document.body,
        props: {
          component: SettingsProvidersPanel,
          componentProps: props,
          coordinator,
        },
      });
      const settle = async () => {
        for (let index = 0; index < 30; index += 1) {
          await Promise.resolve();
          flushSync();
        }
      };
      await settle();
      const input = document.querySelector('.s-local-context-input');
      input.focus();
      const edit = (value) => {
        input.value = value;
        input.dispatchEvent(new Event('input', { bubbles: true }));
        flushSync();
      };
      edit('8192');
      await vi.advanceTimersByTimeAsync(800);
      edit('16384');
      await vi.advanceTimersByTimeAsync(800);
      expect(writes).toHaveLength(0);
      expect(coordinator.hasPending()).toBe(true);
      input.blur();
      await settle();
      expect(writes[0]).toEqual({
        local_models: { context_windows: { [modelId]: 16384 } },
      });
      expect(input.disabled).toBe(false);
      edit('32768');
      const flushing = coordinator.flushPending();
      releaseFirst();
      expect(await flushing).toBe(false);
      await settle();
      expect(document.querySelector('.s-local-context-input')).toBe(input);
      expect(input.value).toBe('32768');
      expect(coordinator.hasPending()).toBe(true);
      await vi.advanceTimersByTimeAsync(1600);
      expect(writes).toHaveLength(2);
      document.querySelector('.save-status button').click();
      await settle();
      expect(writes).toHaveLength(3);
      expect(writes[2]).toEqual({
        local_models: { context_windows: { [modelId]: 32768 } },
      });
      expect(coordinator.hasPending()).toBe(false);
      expect(input.value).toBe('32768');
    } finally {
      vi.useRealTimers();
    }
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

  it('connects an external program login without exposing API key or OAuth controls', async () => {
    const connection = {
      id: 'claude-agent:subscription',
      type: 'external',
      label: 'Claude Code login',
      added: false,
      configured: false,
      enabled: false,
      usable: false,
      accounts: [],
      setup_command: 'claude auth login',
      setup_url: 'https://example.test/setup',
      external_auth: { connected: false, status: 'missing' },
    };
    rpcMock.mockImplementation((method) =>
      Promise.resolve(
        method === 'provider.connect'
          ? { connected: true, status: 'connected' }
          : {},
      ),
    );
    mountedComponent = mount(SettingsProvidersPanel, {
      target: document.body,
      props: {
        settings: {
          providers: {
            items: [
              {
                id: 'claude-agent',
                name: 'Claude Agent',
                connections: [connection],
              },
            ],
          },
        },
        visible: true,
        onRefreshProviderSettings: onRefreshProviderSettingsMock,
      },
    });
    flushSync();
    [...document.querySelectorAll('button')]
      .filter((button) => button.textContent.trim() === 'Add provider')
      .at(-1)
      .click();
    flushSync();
    [...document.querySelectorAll('button')]
      .find((button) => button.textContent.includes('Claude Agent'))
      .click();
    flushSync();
    expect(document.body.textContent).toContain('claude auth login');
    expect(
      document.querySelector('a[href="https://example.test/setup"]'),
    ).toBeTruthy();
    expect(document.querySelector('#provider-api-key')).toBeNull();
    expect(document.querySelector('#provider-account-name')).toBeNull();
    const executable = document.querySelector('#claude-executable-connect');
    executable.value = 'C:/native/claude.exe';
    executable.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    findButton('Check login and connect').click();
    await waitForCondition(
      () => onRefreshProviderSettingsMock.mock.calls.length > 0,
    );
    expect(rpcMock).toHaveBeenCalledWith('provider.connect', {
      provider_id: 'claude-agent',
      connection_id: 'claude-agent:subscription',
      account: 'default',
    });
    expect(rpcMock).toHaveBeenCalledWith('settings.update', {
      providers: { claude_agent: { executable_path: 'C:/native/claude.exe' } },
      base: { providers: { claude_agent: { executable_path: '' } } },
    });
    expect(
      rpcMock.mock.calls.findIndex(([method]) => method === 'settings.update'),
    ).toBeLessThan(
      rpcMock.mock.calls.findIndex(([method]) => method === 'provider.connect'),
    );
    expect(document.querySelector('.provider-connect-modal')).toBeNull();
  });

  it('keeps unchecked external setup editable and localizes readiness', async () => {
    const writes = [];
    rpcMock.mockImplementation((method) =>
      method === 'settings.update'
        ? new Promise((resolve) => writes.push(resolve))
        : Promise.resolve({}),
    );
    const props = reactiveProps({
      visible: true,
      settings: {
        providers: {
          items: [
            {
              id: 'claude-agent',
              name: 'Claude Agent',
              connections: [
                {
                  id: 'claude-agent:subscription',
                  type: 'external',
                  label: 'Subscription',
                  added: true,
                  configured: true,
                  enabled: true,
                  usable: true,
                  accounts: [],
                  external_auth: {
                    status: 'not_checked',
                    connected: false,
                    message: 'RAW_SERVER_DIAGNOSTIC',
                  },
                },
              ],
            },
          ],
        },
      },
    });
    mountedComponent = mount(SettingsProvidersPanel, {
      target: document.body,
      props,
    });
    flushSync();
    expect(document.body.textContent).toContain('Not checked');
    expect(document.body.textContent).not.toContain('RAW_SERVER_DIAGNOSTIC');
    expect(document.body.textContent).not.toContain('Not usable');
    const executable = document.querySelector(
      'input[aria-label="Claude Code executable"]',
    );
    executable.value = 'C:/native/claude.exe';
    executable.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    findButton('Save').click();
    await waitForCondition(() =>
      rpcMock.mock.calls.some(([method]) => method === 'settings.update'),
    );
    expect(rpcMock).toHaveBeenCalledWith('settings.update', {
      providers: { claude_agent: { executable_path: 'C:/native/claude.exe' } },
      base: { providers: { claude_agent: { executable_path: '' } } },
    });
    const settled = () =>
      !document.body.textContent.includes(t('common.saving'));
    writes[0]({});
    await waitForCondition(settled);
    // Unchanged pre-save props must not roll back a successful local save.
    expect(executable.value).toBe('C:/native/claude.exe');

    executable.value = 'C:/native/newer.exe';
    executable.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    findButton('Save').click();
    await waitForCondition(() => writes.length === 2);
    props.settings.providers.items[0].executable_path = 'C:/remote/current.exe';
    flushSync();
    expect(executable.value).toBe('C:/native/newer.exe');
    writes[1]({});
    await waitForCondition(settled);
    expect(executable.value).toBe('C:/remote/current.exe');

    executable.value = 'C:/native/third.exe';
    executable.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    findButton('Save').click();
    await waitForCondition(() => writes.length === 3);
    props.settings.providers.items[0].executable_path = 'C:/native/third.exe';
    executable.value = 'C:/native/unsaved.exe';
    executable.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    writes[2]({});
    await waitForCondition(settled);
    // A newer local draft retains its base/conflict protection.
    expect(executable.value).toBe('C:/native/unsaved.exe');
    findButton('Save').click();
    await waitForCondition(() => writes.length === 4);
    writes[3]({});
    await waitForCondition(settled);
    // A prior acknowledgement must not roll back the following successful save.
    expect(executable.value).toBe('C:/native/unsaved.exe');
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
