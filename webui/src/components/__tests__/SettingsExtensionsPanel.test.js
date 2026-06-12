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

const { default: SettingsExtensionsPanel } = await import(
  '../settings/SettingsExtensionsPanel.svelte'
);

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
        capabilities: {
          hooks: { tool_call: 1 },
          tools: ['word_count'],
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

    mountedComponent = mount(SettingsExtensionsPanel, { target: document.body });
    flushSync();
    await flushAsync();

    expect(document.body.textContent).toContain('guard_bash');
    expect(document.body.textContent).toContain('Loaded');
    expect(document.body.textContent).toContain('Hooks: tool_call(1)');
    expect(document.body.textContent).toContain('Tools: word_count');
    expect(document.body.textContent).toContain('broken');
    expect(document.body.textContent).toContain('import failed: boom');
  });

  it('disables an extension and shows the restart-required notice', async () => {
    rpcMock.mockImplementation((method) => {
      if (method === 'extensions.list') {
        return Promise.resolve(extensionsResult());
      }
      return Promise.resolve({ restart_required: true });
    });

    mountedComponent = mount(SettingsExtensionsPanel, { target: document.body });
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
    expect(document.body.textContent).toContain(
      'Extension changes apply after a restart',
    );
    expect(document.body.textContent).toContain('vbot server restart');
  });

  it('rejects invalid config JSON without calling settings.update', async () => {
    rpcMock.mockResolvedValue(extensionsResult());

    mountedComponent = mount(SettingsExtensionsPanel, { target: document.body });
    flushSync();
    await flushAsync();

    const textarea = document.body.querySelector('textarea');
    textarea.value = '{not json}';
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    buttonByText('Save config').click();
    await flushAsync();

    expect(document.body.textContent).toContain('Config must be a JSON object.');
    expect(
      rpcMock.mock.calls.some((call) => call[0] === 'settings.update'),
    ).toBe(false);
  });
});
