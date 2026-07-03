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

const { default: SettingsExtensionsPanel } =
  await import('../settings/SettingsExtensionsPanel.svelte');

function extensionsResult() {
  return {
    extensions: [
      {
        name: 'guard_bash',
        status: 'loaded',
        disabled: false,
        version: '1.2.0',
        description: 'Guards dangerous bash',
        error: null,
        config: {},
        capability_errors: [],
        ready_state: 'ready',
        capabilities: {
          hooks: { tool_call: 1 },
          tools: [{ name: 'word_count', ready: true }],
          recall_backends: [],
          startup: false,
          shutdown: false,
        },
      },
      {
        name: 'broken',
        status: 'failed',
        disabled: false,
        version: null,
        description: null,
        error: 'import failed: boom',
        config: {},
        capability_errors: [],
        ready_state: 'ready',
        capabilities: {},
      },
    ],
  };
}

function buttonByText(text) {
  return [...document.body.querySelectorAll('button')].find((button) =>
    button.textContent.trim().includes(text),
  );
}

async function flushAsync() {
  await Promise.resolve();
  await Promise.resolve();
  flushSync();
}

describe('SettingsExtensionsPanel', () => {
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

  it('renders extension cards with status, capabilities, and failure detail', async () => {
    rpcMock.mockResolvedValue(extensionsResult());

    mountedComponent = mount(SettingsExtensionsPanel, {
      target: document.body,
    });
    flushSync();
    await flushAsync();

    expect(document.body.textContent).toContain('guard_bash');
    expect(document.body.textContent).toContain('Loaded');
    expect(document.body.textContent).toContain('Hooks: tool_call(1)');
    expect(document.body.textContent).toContain('Tools: word_count');
    expect(document.body.textContent).toContain('broken');
    expect(document.body.textContent).toContain('import failed: boom');
  });

  it('shows the waiting hint and names unset secret fields', async () => {
    rpcMock.mockResolvedValue({
      extensions: [
        {
          name: 'homeassistant',
          status: 'loaded',
          disabled: false,
          version: null,
          description: null,
          error: null,
          config: {},
          capability_errors: [],
          ready_state: 'waiting',
          settings_schema: [
            {
              key: 'token',
              type: 'secret',
              label: 'Token',
              env_key: 'HASS_TOKEN',
              set: false,
            },
          ],
          capabilities: {
            hooks: {},
            tools: [{ name: 'ha_call_service', ready: false }],
            recall_backends: [],
            startup: false,
            shutdown: false,
          },
        },
      ],
    });

    mountedComponent = mount(SettingsExtensionsPanel, {
      target: document.body,
    });
    flushSync();
    await flushAsync();

    expect(document.body.textContent).toContain(
      'On, waiting for configuration',
    );
    expect(document.body.textContent).toContain('Waiting for: Token');
  });

  it('disables an extension live without showing a restart notice', async () => {
    // Disabling applies live, and the panel never surfaces a restart notice.
    rpcMock.mockImplementation((method) => {
      if (method === 'extensions.list') {
        return Promise.resolve(extensionsResult());
      }
      return Promise.resolve({});
    });

    mountedComponent = mount(SettingsExtensionsPanel, {
      target: document.body,
    });
    flushSync();
    await flushAsync();

    buttonByText('Disable').click();
    await flushAsync();

    const updateCall = rpcMock.mock.calls.find(
      (call) => call[0] === 'settings.update',
    );
    expect(updateCall).toBeTruthy();
    expect(updateCall[1]).toEqual({
      extensions: { disabled: ['guard_bash'], config: {} },
    });
    expect(document.body.textContent).not.toContain('vbot server restart');
  });

  it('enables an extension live without a restart notice', async () => {
    // Enabling now rebuilds the extension layer live: the panel writes the disabled
    // set and never surfaces a restart notice.
    const disabledExtension = {
      ...extensionsResult().extensions[0],
      disabled: true,
      status: 'disabled',
    };
    rpcMock.mockImplementation((method) => {
      if (method === 'extensions.list') {
        return Promise.resolve({ extensions: [disabledExtension] });
      }
      return Promise.resolve({});
    });

    mountedComponent = mount(SettingsExtensionsPanel, {
      target: document.body,
    });
    flushSync();
    await flushAsync();

    buttonByText('Enable').click();
    await flushAsync();

    const updateCall = rpcMock.mock.calls.find(
      (call) => call[0] === 'settings.update',
    );
    expect(updateCall).toBeTruthy();
    expect(updateCall[1]).toEqual({
      extensions: { disabled: [], config: {} },
    });
    expect(document.body.textContent).not.toContain('after a restart');
    expect(document.body.textContent).not.toContain('vbot server restart');
  });

  it('reloads all extensions and re-lists', async () => {
    rpcMock.mockImplementation((method) => {
      if (method === 'extensions.list') {
        return Promise.resolve(extensionsResult());
      }
      return Promise.resolve({});
    });

    mountedComponent = mount(SettingsExtensionsPanel, {
      target: document.body,
    });
    flushSync();
    await flushAsync();

    const listCallsBefore = rpcMock.mock.calls.filter(
      (call) => call[0] === 'extensions.list',
    ).length;

    buttonByText('Reload extensions').click();
    await flushAsync();

    const reloadCall = rpcMock.mock.calls.find(
      (call) => call[0] === 'extensions.reload',
    );
    expect(reloadCall).toBeTruthy();
    // The panel re-lists after a successful reload to show the rebuilt catalog.
    const listCallsAfter = rpcMock.mock.calls.filter(
      (call) => call[0] === 'extensions.list',
    ).length;
    expect(listCallsAfter).toBeGreaterThan(listCallsBefore);
  });

  it('rejects invalid config JSON without calling settings.update', async () => {
    rpcMock.mockResolvedValue(extensionsResult());

    mountedComponent = mount(SettingsExtensionsPanel, {
      target: document.body,
    });
    flushSync();
    await flushAsync();

    const textarea = document.body.querySelector('textarea');
    textarea.value = '{not json}';
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    buttonByText('Save config').click();
    await flushAsync();

    expect(document.body.textContent).toContain(
      'Config must be a JSON object.',
    );
    expect(
      rpcMock.mock.calls.some((call) => call[0] === 'settings.update'),
    ).toBe(false);
  });
});
