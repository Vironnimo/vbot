// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { init } from '../../lib/i18n.js';
import { rpcBackedApiMock } from './apiMock.js';

const rpc = vi.fn();
vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
vi.mock('$lib/api.js', () => rpcBackedApiMock(rpc));
const { default: Panel } = await import('../settings/SettingsMcpPanel.svelte');
const { default: Extensions } =
  await import('../settings/SettingsExtensionsPanel.svelte');
let component;
let records;
const original = {
  id: 'example',
  transport: 'stdio',
  command: 'python',
  args: ['a b', ''],
  agents: ['alice'],
  enabled: true,
  timeout: 120,
  credential_environment: { TOKEN: 'TEST_KEY' },
};
async function settle() {
  for (let index = 0; index < 15; index++) {
    await Promise.resolve();
    flushSync();
  }
}
function button(text) {
  return [...document.querySelectorAll('button')].find(
    (item) => item.textContent.trim() === text,
  );
}
function input(label, value) {
  const node = [...document.querySelectorAll('label')].find(
    (item) => item.textContent.replace('*', '').trim() === label,
  );
  const field = document.getElementById(node.htmlFor);
  field.value = value;
  field.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}
function submit() {
  document
    .querySelector('[role="dialog"] form')
    .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
}
beforeEach(() => {
  init('en');
  records = [];
  rpc.mockReset().mockImplementation(async (method, params) => {
    if (method === 'agent.list')
      return { agents: [{ id: 'alice', name: 'Alice' }] };
    if (method === 'project.list')
      return { projects: [{ project_id: 'studio' }] };
    if (method === 'project.show')
      return { scan: { team: [{ agent_id: 'artist' }] } };
    if (method === 'extensions.list')
      return {
        extensions: [
          {
            name: 'mcp',
            status: 'loaded',
            disabled: false,
            config: {},
            capabilities: {},
          },
        ],
      };
    if (method === 'extensions.operation') {
      const { operation, arguments: args } = params;
      if (operation === 'list')
        return { connections: structuredClone(records) };
      if (operation === 'save')
        records = [
          {
            id: args.connection.id,
            configuration: args.connection,
            state: 'disconnected',
          },
        ];
      if (operation === 'remove') records = [];
      if (operation === 'disable') records[0].configuration.enabled = false;
      if (operation === 'enable') records[0].configuration.enabled = true;
      return {};
    }
    throw new Error(method);
  });
});
afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  document.body.innerHTML = '';
});

describe('MCP management surface', () => {
  it('is reachable from the loaded MCP Extension without a settings schema', async () => {
    component = mount(Extensions, { target: document.body });
    await settle();
    expect(button('Add MCP connection')).toBeTruthy();
  });
  it('creates a local connection with an exact Project Agent grant and reads it back', async () => {
    component = mount(Panel, { target: document.body });
    await settle();
    button('Add MCP connection').click();
    await settle();
    input('Connection name', 'blender');
    const nameInput = document.querySelector('[role="dialog"] input');
    expect(nameInput.pattern).toBe('[a-z][a-z0-9_]{0,31}');
    expect(nameInput.checkValidity()).toBe(true);
    input('Program', 'uvx');
    button('Add argument').click();
    flushSync();
    input('Argument 1', 'blender-mcp');
    document.querySelector('[aria-label="Allow artist@studio"]').click();
    flushSync();
    submit();
    await settle();
    expect(rpc).toHaveBeenCalledWith('extensions.operation', {
      name: 'mcp',
      operation: 'save',
      arguments: {
        connection: expect.objectContaining({
          id: 'blender',
          command: 'uvx',
          args: ['blender-mcp'],
          agents: ['artist@studio'],
        }),
      },
    });
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(
      document.querySelector('article[aria-label="blender"]'),
    ).toBeTruthy();
  });
  it('preserves exact arguments and credentials when editing and allows grant removal', async () => {
    records = [
      {
        id: 'example',
        configuration: structuredClone(original),
        state: 'connected',
      },
    ];
    component = mount(Panel, { target: document.body });
    await settle();
    button('Edit').click();
    await settle();
    input('Program', 'python3');
    document.querySelector('[aria-label="Allow alice"]').click();
    flushSync();
    submit();
    await settle();
    expect(records[0].configuration).toMatchObject({
      command: 'python3',
      args: ['a b', ''],
      agents: [],
      credential_environment: { TOKEN: 'TEST_KEY' },
    });
  });
  it('retains the editor and displays save failures', async () => {
    const handler = rpc.getMockImplementation();
    rpc.mockImplementation((method, params) =>
      params?.operation === 'save'
        ? Promise.reject(new Error('test-owned-save-error'))
        : handler(method, params),
    );
    component = mount(Panel, { target: document.body });
    await settle();
    button('Add MCP connection').click();
    await settle();
    input('Connection name', 'example');
    input('Program', 'python');
    submit();
    await settle();
    expect(
      document.querySelector('[role="dialog"] [role="alert"]').textContent,
    ).toContain('test-owned-save-error');
    expect(document.querySelector('[role="dialog"] input').value).toBe(
      'example',
    );
  });
  it('uses a write-only password field and clears it after saving', async () => {
    records = [
      {
        id: 'example',
        configuration: structuredClone(original),
        state: 'connected',
      },
    ];
    component = mount(Panel, { target: document.body });
    await settle();
    button('Credentials').click();
    flushSync();
    const field = document.querySelector('input[type="password"]');
    expect(field.value).toBe('');
    input('New secret value', 'test-owned-secret');
    submit();
    await settle();
    expect(rpc).toHaveBeenCalledWith('extensions.operation', {
      name: 'mcp',
      operation: 'credential',
      arguments: { id: 'example', key: 'TEST_KEY', value: 'test-owned-secret' },
    });
    expect(document.querySelector('input[type="password"]')).toBeNull();
    expect(document.body.textContent).not.toContain('test-owned-secret');
  });
  it('reconciles enablement and requires confirmation before removal', async () => {
    records = [
      {
        id: 'example',
        configuration: structuredClone(original),
        state: 'connected',
      },
    ];
    component = mount(Panel, { target: document.body });
    await settle();
    document.querySelector('[aria-label="Enable example"]').click();
    await settle();
    expect(
      document
        .querySelector('[aria-label="Enable example"]')
        .getAttribute('aria-checked'),
    ).toBe('false');
    button('Remove').click();
    flushSync();
    expect(records).toHaveLength(1);
    const confirm = [
      ...document.querySelectorAll('[role="dialog"] button'),
    ].find((item) => item.textContent.trim() === 'Remove');
    confirm.click();
    await settle();
    expect(records).toHaveLength(0);
    expect(document.querySelector('article')).toBeNull();
  });
});
