// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { rpcBackedApiMock } from './apiMock.js';

const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => rpcBackedApiMock(rpcMock));

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
          commands: [{ name: 'workflow', registered: true }],
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

function extensionWithSchema(fields) {
  return {
    ...extensionsResult().extensions[0],
    settings_schema: fields,
  };
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
    expect(document.body.textContent).toContain('Commands: /workflow');
    expect(document.body.textContent).toContain('broken');
    expect(document.body.textContent).toContain('import failed: boom');
    expect(buttonByText('Refresh')).toBeUndefined();
  });

  it('offers Retry only when the extension list fails to load', async () => {
    rpcMock
      .mockRejectedValueOnce(new Error('extension list unavailable'))
      .mockResolvedValueOnce(extensionsResult());

    mountedComponent = mount(SettingsExtensionsPanel, {
      target: document.body,
    });
    flushSync();
    await flushAsync();

    expect(buttonByText('Refresh')).toBeUndefined();
    expect(buttonByText('Retry')).toBeTruthy();

    buttonByText('Retry').click();
    await flushAsync();

    expect(document.body.textContent).toContain('guard_bash');
    expect(buttonByText('Retry')).toBeUndefined();
    expect(buttonByText('Refresh')).toBeUndefined();
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

    expect(document.querySelector('.s-ext-waiting')).toBeTruthy();
    expect(document.querySelector('.s-ext-waiting-for')?.textContent).toContain(
      'Token',
    );
  });

  it('submits a secret field through its form', async () => {
    const result = {
      extensions: [
        {
          ...extensionsResult().extensions[0],
          name: 'homeassistant',
          settings_schema: [
            {
              key: 'token',
              type: 'secret',
              label: 'Token',
              env_key: 'HASS_TOKEN',
              set: false,
            },
          ],
        },
      ],
    };
    rpcMock.mockImplementation((method) => {
      if (method === 'extensions.list') {
        return Promise.resolve(result);
      }
      return Promise.resolve({});
    });

    mountedComponent = mount(SettingsExtensionsPanel, {
      target: document.body,
    });
    flushSync();
    await flushAsync();

    const input = document.body.querySelector('input[type="password"]');
    const form = input?.closest('form');
    expect(form).toBeTruthy();

    input.value = 'new-token';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    form.dispatchEvent(
      new Event('submit', { bubbles: true, cancelable: true }),
    );
    await flushAsync();

    expect(rpcMock).toHaveBeenCalledWith('extensions.set_secret', {
      name: 'homeassistant',
      key: 'token',
      value: 'new-token',
    });
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

  it('shows the reload action once and keeps its explanation in an info hint', async () => {
    rpcMock.mockResolvedValue(extensionsResult());

    mountedComponent = mount(SettingsExtensionsPanel, {
      target: document.body,
    });
    flushSync();
    await flushAsync();

    expect(document.body.textContent.match(/Reload extensions/g)).toHaveLength(
      1,
    );

    const infoHint = document.querySelector(
      'button[aria-label="About reloading extensions"]',
    );
    expect(infoHint).toBeTruthy();
    infoHint.click();
    flushSync();

    expect(document.body.textContent).toContain(
      'Rebuilds all extensions from disk',
    );
  });

  it('shows configuration controls only for extensions declaring a schema', async () => {
    const homeAssistant = extensionWithSchema([
      { key: 'url', type: 'text', label: 'Server URL' },
    ]);
    homeAssistant.name = 'homeassistant';
    rpcMock.mockResolvedValue({
      extensions: [extensionsResult().extensions[0], homeAssistant],
    });

    mountedComponent = mount(SettingsExtensionsPanel, {
      target: document.body,
    });
    flushSync();
    await flushAsync();

    expect(
      document.querySelector(
        'button[aria-label="Configuration for extension guard_bash"]',
      ),
    ).toBeNull();
    expect(
      document.querySelector(
        'button[aria-label="Configuration for extension homeassistant"]',
      ),
    ).toBeTruthy();
    expect(document.querySelector('textarea')).toBeNull();
    expect(document.body.textContent).not.toContain('Config (JSON)');
  });

  it('auto-saves a declared text setting 800 ms after the last edit', async () => {
    const withTextSetting = extensionWithSchema([
      { key: 'level', type: 'text', label: 'Level' },
    ]);
    rpcMock.mockImplementation((method) => {
      if (method === 'extensions.list') {
        return Promise.resolve({ extensions: [withTextSetting] });
      }
      return Promise.resolve({});
    });

    mountedComponent = mount(SettingsExtensionsPanel, {
      target: document.body,
    });
    flushSync();
    await flushAsync();

    vi.useFakeTimers();

    const input = document.body.querySelector('input[type="text"]');
    input.value = 'warn';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    // No save before the debounce elapses.
    expect(
      rpcMock.mock.calls.some((call) => call[0] === 'settings.update'),
    ).toBe(false);

    vi.advanceTimersByTime(799);
    await flushAsync();
    expect(
      rpcMock.mock.calls.some((call) => call[0] === 'settings.update'),
    ).toBe(false);

    vi.advanceTimersByTime(1);
    await flushAsync();

    const updateCall = rpcMock.mock.calls.find(
      (call) => call[0] === 'settings.update',
    );
    expect(updateCall).toBeTruthy();
    // The edited extension's non-secret config is persisted through the shared
    // settings.update payload shape.
    expect(updateCall[1].extensions.config.guard_bash).toEqual({
      level: 'warn',
    });

    vi.useRealTimers();
  });

  it('auto-saves after a schema toggle is flipped', async () => {
    // The boolean schema field is the shared Toggle (role="switch"); flipping it
    // must feed the same autosave path as any other non-secret config edit.
    const withToggle = {
      ...extensionsResult().extensions[0],
      settings_schema: [
        { key: 'verbose', type: 'toggle', label: 'Verbose', default: false },
      ],
    };
    rpcMock.mockImplementation((method) => {
      if (method === 'extensions.list') {
        return Promise.resolve({ extensions: [withToggle] });
      }
      return Promise.resolve({});
    });

    mountedComponent = mount(SettingsExtensionsPanel, {
      target: document.body,
    });
    flushSync();
    await flushAsync();

    vi.useFakeTimers();

    const toggle = document.body.querySelector('button[role="switch"]');
    expect(toggle).toBeTruthy();
    toggle.click();
    flushSync();

    vi.advanceTimersByTime(800);
    await flushAsync();

    const updateCall = rpcMock.mock.calls.find(
      (call) => call[0] === 'settings.update',
    );
    expect(updateCall).toBeTruthy();
    expect(updateCall[1].extensions.config.guard_bash).toEqual({
      verbose: true,
    });

    vi.useRealTimers();
  });

  it('shows Already saved when Save settings is clicked with no changes', async () => {
    const toastMock = vi.fn();
    const withTextSetting = extensionWithSchema([
      { key: 'level', type: 'text', label: 'Level' },
    ]);
    rpcMock.mockImplementation((method) => {
      if (method === 'extensions.list') {
        return Promise.resolve({ extensions: [withTextSetting] });
      }
      return Promise.resolve({});
    });

    mountedComponent = mount(SettingsExtensionsPanel, {
      target: document.body,
      props: { onToast: toastMock },
    });
    flushSync();
    await flushAsync();

    buttonByText('Save settings').click();
    await flushAsync();

    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({ title: 'Already saved', variant: 'success' }),
    );
    expect(
      rpcMock.mock.calls.some((call) => call[0] === 'settings.update'),
    ).toBe(false);
  });
});
