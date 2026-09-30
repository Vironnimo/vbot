// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../lib/i18n.js';
import { rpcBackedApiMock } from './apiMock.support.js';

const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('svelte/reactivity', async () => {
  return import('../../../node_modules/svelte/src/reactivity/index-client.js');
});

vi.mock('$lib/api.js', () => rpcBackedApiMock(rpcMock));

const { default: SettingsExtensionsPanel } =
  await import('../settings/SettingsExtensionsPanel.svelte');

function guardBash(overrides = {}) {
  return {
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
    ...overrides,
  };
}

function brokenExtension() {
  return {
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
  };
}

function withSchema(fields, overrides = {}) {
  return guardBash({ settings_schema: fields, ...overrides });
}

// extensions.list answers with `extensions`; every write succeeds.
function serveExtensions(extensions, overrides = {}) {
  rpcMock.mockImplementation((method, params) => {
    if (typeof overrides[method] === 'function') {
      return overrides[method](params);
    }
    if (method === 'extensions.list') {
      return Promise.resolve({ extensions });
    }
    return Promise.resolve({});
  });
}

function buttonByText(text) {
  return [...document.body.querySelectorAll('button')].find(
    (button) => button.textContent.trim() === text,
  );
}

function settingsUpdates() {
  return rpcMock.mock.calls.filter((call) => call[0] === 'settings.update');
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
    vi.useRealTimers();
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
  });

  async function mountPanel(props = {}) {
    mountedComponent = mount(SettingsExtensionsPanel, {
      target: document.body,
      props,
    });
    flushSync();
    await flushAsync();
  }

  it('renders extension rows with attention-only status, details, and failure detail', async () => {
    serveExtensions([guardBash(), brokenExtension()]);
    await mountPanel();

    const [card, brokenCard] = document.querySelectorAll('.s-ext-card');
    expect(card.textContent).toContain('guard_bash');
    // A healthy Extension needs no chip; a failed one does.
    expect(card.querySelector('.chip')).toBeNull();
    expect(brokenCard.querySelector('.chip').textContent.trim()).toBe(
      t('settings.extensions.statusFailed'),
    );
    // Version and capabilities open in the row's details.
    const details = card.querySelector('.s-disclosure-sub');
    const disclosure = card.querySelector(
      'button[aria-label="Details for extension guard_bash"]',
    );
    expect(details.hidden).toBe(true);
    disclosure.click();
    flushSync();
    expect(disclosure.getAttribute('aria-expanded')).toBe('true');
    expect(details.hidden).toBe(false);
    expect(details.textContent).toContain('v1.2.0');
    expect(
      Array.from(
        card.querySelectorAll('.s-ext-capabilities__part'),
        (part) => part.textContent,
      ),
    ).toEqual([
      `${t('settings.extensions.hooks')}: tool_call(1)`,
      `${t('settings.extensions.tools')}: word_count`,
      `${t('settings.extensions.commands')}: /workflow`,
    ]);
    expect(brokenCard.textContent).toContain('import failed: boom');
    expect(buttonByText('Refresh')).toBeUndefined();
  });

  it('hides the capability list of the loaded MCP Extension and embeds its connection manager', async () => {
    rpcMock.mockImplementation(async (method, params) => {
      if (method === 'extensions.list')
        return {
          extensions: [
            {
              name: 'mcp',
              status: 'loaded',
              disabled: false,
              config: {},
              capabilities: {
                tools: [{ name: 'test-owned-internal-tool', ready: true }],
              },
            },
          ],
        };
      if (params?.operation === 'list') return { connections: [] };
      if (method === 'agent.list') return { agents: [] };
      if (method === 'project.list') return { projects: [] };
      return {};
    });
    await mountPanel();
    for (let index = 0; index < 10; index += 1) await flushAsync();

    expect(buttonByText('Add MCP connection')).toBeTruthy();
    expect(document.body.textContent).not.toContain('test-owned-internal-tool');
  });

  it('offers Retry only when the extension list fails to load', async () => {
    rpcMock
      .mockRejectedValueOnce(new Error('extension list unavailable'))
      .mockResolvedValueOnce({ extensions: [guardBash()] });
    await mountPanel();

    expect(buttonByText('Refresh')).toBeUndefined();
    buttonByText('Retry').click();
    await flushAsync();

    expect(document.body.textContent).toContain('guard_bash');
    expect(buttonByText('Retry')).toBeUndefined();
  });

  it('reloads all extensions from one action with an explanatory hint, then re-lists', async () => {
    serveExtensions([guardBash()]);
    await mountPanel();
    const reload = t('settings.extensions.reload');
    expect(
      [...document.querySelectorAll('button')].filter(
        (button) => button.textContent.trim() === reload,
      ),
    ).toHaveLength(1);

    const infoHint = document.querySelector(
      'button[aria-label="About reloading extensions"]',
    );
    infoHint.click();
    flushSync();
    expect(document.body.textContent).toContain(
      t('settings.extensions.reloadHelp'),
    );

    const listCallsBefore = rpcMock.mock.calls.filter(
      (call) => call[0] === 'extensions.list',
    ).length;
    buttonByText(reload).click();
    await flushAsync();
    expect(rpcMock).toHaveBeenCalledWith('extensions.reload');
    expect(
      rpcMock.mock.calls.filter((call) => call[0] === 'extensions.list').length,
    ).toBeGreaterThan(listCallsBefore);
  });

  it.each([
    ['disables', guardBash(), 'true', ['guard_bash'], []],
    [
      'enables',
      guardBash({ disabled: true, status: 'disabled' }),
      'false',
      [],
      ['guard_bash'],
    ],
  ])(
    '%s an extension live through the disabled set',
    async (_label, extension, checked, disabled, savedDisabled) => {
      serveExtensions([extension]);
      await mountPanel();

      const toggle = document.querySelector(
        'button[role="switch"][aria-label="Enable extension guard_bash"]',
      );
      expect(toggle.getAttribute('aria-checked')).toBe(checked);
      toggle.click();
      await flushAsync();

      expect(settingsUpdates()).toEqual([
        [
          'settings.update',
          {
            extensions: { disabled, config: {} },
            // The saved section it replaces, so a newer write is not lost.
            base: { extensions: { disabled: savedDisabled, config: {} } },
          },
        ],
      ]);
    },
  );

  it('rebases a refused configuration save onto Extensions saved elsewhere', async () => {
    const schema = [
      { key: 'level', type: 'text', label: 'Level' },
      { key: 'mode', type: 'text', label: 'Mode' },
    ];
    const other = guardBash({ name: 'other' });
    // Another writer changes `mode` and disables `other` after the panel loaded.
    const listings = [
      [withSchema(schema, { config: { level: 'info', mode: 'a' } }), other],
      [
        withSchema(schema, { config: { level: 'info', mode: 'b' } }),
        { ...other, disabled: true, status: 'disabled' },
      ],
    ];
    let listCount = 0;
    let refused = false;
    serveExtensions([], {
      'extensions.list': () =>
        Promise.resolve({
          extensions: listings[Math.min(listCount++, listings.length - 1)],
        }),
      'settings.update': () => {
        if (refused) return Promise.resolve({});
        refused = true;
        return Promise.reject(
          Object.assign(new Error('Settings changed'), {
            code: 'settings_conflict',
          }),
        );
      },
    });
    await mountPanel();

    const level = document.querySelector('#extension-guard_bash-level');
    level.value = 'warn';
    level.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    document.querySelector('.s-ext-config-actions .save-status button').click();
    for (let step = 0; step < 5 && settingsUpdates().length < 2; step += 1) {
      await flushAsync();
    }

    expect(settingsUpdates()[1][1]).toEqual({
      extensions: {
        disabled: ['other'],
        config: { guard_bash: { level: 'warn', mode: 'b' } },
      },
      base: {
        extensions: {
          disabled: ['other'],
          config: { guard_bash: { level: 'info', mode: 'b' } },
        },
      },
    });
    expect(document.querySelector('#extension-guard_bash-mode').value).toBe(
      'b',
    );
  });

  it('shows the waiting hint and names unset secret fields', async () => {
    serveExtensions([
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
    ]);
    await mountPanel();

    expect(document.querySelector('.s-ext-waiting')).toBeTruthy();
    expect(document.querySelector('.s-ext-waiting-for').textContent).toContain(
      'Token',
    );
  });

  it('submits a secret field through its form', async () => {
    serveExtensions([
      withSchema(
        [
          {
            key: 'token',
            type: 'secret',
            label: 'Token',
            env_key: 'HASS_TOKEN',
            set: false,
          },
        ],
        { name: 'homeassistant' },
      ),
    ]);
    await mountPanel();

    const input = document.body.querySelector('input[type="password"]');
    input.value = 'new-token';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input
      .closest('form')
      .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    await flushAsync();

    expect(rpcMock).toHaveBeenCalledWith('extensions.set_secret', {
      name: 'homeassistant',
      key: 'token',
      value: 'new-token',
    });
  });

  it('shows configuration controls only for extensions declaring a schema', async () => {
    serveExtensions([
      guardBash(),
      withSchema([{ key: 'url', type: 'text', label: 'Server URL' }], {
        name: 'homeassistant',
      }),
    ]);
    await mountPanel();

    expect(document.querySelector('[id^="extension-guard_bash-"]')).toBeNull();
    expect(
      document
        .querySelector('#extension-homeassistant-url')
        .closest('.s-disclosure-sub'),
    ).toBeTruthy();
    // No free-form JSON config editor.
    expect(document.querySelector('textarea')).toBeNull();
  });

  // Every non-secret schema control feeds the same debounced settings.update.
  it.each([
    [
      'text',
      { key: 'level', type: 'text', label: 'Level' },
      (control) => {
        control.value = 'warn';
        control.dispatchEvent(new Event('input', { bubbles: true }));
      },
      'input[type="text"]',
      { level: 'warn' },
    ],
    [
      'toggle',
      { key: 'verbose', type: 'toggle', label: 'Verbose', default: false },
      (control) => control.click(),
      '.s-ext-schema button[role="switch"]',
      { verbose: true },
    ],
  ])(
    'auto-saves a declared %s setting 800 ms after the last edit',
    async (_type, field, edit, selector, config) => {
      serveExtensions([withSchema([field])]);
      await mountPanel();
      vi.useFakeTimers();

      edit(document.body.querySelector(selector));
      flushSync();
      vi.advanceTimersByTime(799);
      await flushAsync();
      expect(settingsUpdates()).toHaveLength(0);

      vi.advanceTimersByTime(1);
      await flushAsync();
      expect(settingsUpdates()).toHaveLength(1);
      expect(settingsUpdates()[0][1].extensions.config.guard_bash).toEqual(
        config,
      );
    },
  );

  it('keeps focus and newer extension edits while a save is in flight', async () => {
    let finish;
    serveExtensions(
      [withSchema([{ key: 'level', type: 'text', label: 'Level' }])],
      {
        'settings.update': () =>
          finish
            ? Promise.resolve({})
            : new Promise((resolve) => {
                finish = resolve;
              }),
      },
    );
    await mountPanel();
    vi.useFakeTimers();
    const input = document.querySelector('input[type="text"]');
    const type = (value) => {
      input.value = value;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      flushSync();
    };
    input.focus();
    type('first');
    await vi.advanceTimersByTimeAsync(800);
    expect(input.disabled).toBe(false);
    type('latest');
    finish({});
    await flushAsync();
    expect(document.activeElement).toBe(input);
    expect(input.value).toBe('latest');

    await vi.advanceTimersByTimeAsync(800);
    await flushAsync();
    expect(settingsUpdates()).toHaveLength(2);
    expect(settingsUpdates()[1][1].extensions.config.guard_bash.level).toBe(
      'latest',
    );
    expect(document.activeElement).toBe(input);
  });

  it('saves a changed form from its save state and confirms the save', async () => {
    serveExtensions([
      withSchema([{ key: 'level', type: 'text', label: 'Level' }]),
    ]);
    await mountPanel();
    const saveState = document.querySelector(
      '.s-ext-config-actions .save-status',
    );
    expect(saveState.querySelector('button')).toBeNull();

    const input = document.querySelector('input[type="text"]');
    input.value = 'warn';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    saveState.querySelector('button').click();
    await flushAsync();

    expect(settingsUpdates()).toHaveLength(1);
    expect(settingsUpdates()[0][1].extensions.config.guard_bash).toEqual({
      level: 'warn',
    });
    expect(saveState.querySelector('[role="status"]').textContent.trim()).toBe(
      t('common.saved'),
    );
  });
});
