// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { init } from '../../lib/i18n.js';
import { rpcBackedApiMock } from './apiMock.support.js';

it('limits MCP typography to its panel and portaled dialogs', () => {
  const style = document.createElement('style');
  style.textContent = readFileSync(
    join(dirname(fileURLToPath(import.meta.url)), '../settings/mcp.css'),
    'utf8',
  );
  document.head.appendChild(style);
  try {
    const rules = [...style.sheet.cssRules].filter(
      (rule) => rule.selectorText && rule.style.getPropertyValue('font-size'),
    );
    for (const tag of ['p', 'h3', 'h4', 'summary']) {
      const chatText = document.createElement(tag);
      expect(rules.some((rule) => chatText.matches(rule.selectorText))).toBe(
        false,
      );
      for (const owner of ['mcp-panel', 'mcp-modal']) {
        const container = document.createElement('div');
        container.className = owner;
        const text = document.createElement(tag);
        container.appendChild(text);
        expect(rules.some((rule) => text.matches(rule.selectorText))).toBe(
          true,
        );
      }
    }
    // Layout rules for the MCP dialogs must not reach other dialogs either.
    const modal = document.createElement('div');
    modal.className = 'modal';
    const body = document.createElement('div');
    body.className = 'modal-body';
    modal.appendChild(body);
    const layoutRules = [...style.sheet.cssRules].filter(
      (rule) => rule.selectorText,
    );
    expect(layoutRules.some((rule) => body.matches(rule.selectorText))).toBe(
      false,
    );
  } finally {
    style.remove();
  }
});

const rpc = vi.fn();
vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
vi.mock(
  'svelte/reactivity',
  async () =>
    import('../../../node_modules/svelte/src/reactivity/index-client.js'),
);
vi.mock('$lib/api.js', () => rpcBackedApiMock(rpc));
const { default: Panel } = await import('../settings/SettingsMcpPanel.svelte');
let component;
let records;
const original = {
  id: 'example',
  transport: 'stdio',
  command: 'python',
  args: ['a b', ''],

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
function field(label) {
  const node = [...document.querySelectorAll('label')].find(
    (item) => item.textContent.replace('*', '').trim() === label,
  );
  return document.getElementById(node.htmlFor);
}
function input(label, value) {
  const control = field(label);
  control.value = value;
  control.dispatchEvent(new Event('input', { bubbles: true }));
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
  it('opens capabilities and searches without calling remote Tools', async () => {
    records = [
      {
        id: 'example',
        configuration: structuredClone(original),
        state: 'connected',
        counts: { tools: 1 },
      },
    ];
    const handler = rpc.getMockImplementation();
    rpc.mockImplementation((method, params) => {
      if (params?.operation === 'inspect')
        return Promise.resolve({
          ...records[0],
          catalog_available: true,
          total: 1,
          offset: 0,
          previous_offset: null,
          next_offset: null,
          tools: [
            {
              target: 'test-owned-target',
              name: 'execute_code',
              description:
                'test-owned-capability '.repeat(30) + 'test-owned-tail',
            },
          ],
          instructions: '<script>test-owned-external</script>',
          prompts: [],
        });
      return handler(method, params);
    });
    component = mount(Panel, { target: document.body });
    await settle();
    button('Capabilities & access').click();
    await settle();
    const dialog = document.querySelector('[role="dialog"]');
    expect(dialog.textContent).toContain('test-owned-capability');
    const fullDescription = dialog.querySelector('details');
    expect(fullDescription.open).toBe(false);
    fullDescription.querySelector('summary').click();
    expect(fullDescription.open).toBe(true);
    expect(fullDescription.textContent).toContain('test-owned-tail');
    expect(dialog.querySelector('[aria-label^="Allow "]')).toBeNull();
    expect(dialog.querySelector('script')).toBeNull();
    const search = dialog.querySelector('input[aria-label="Search Tools"]');
    search.value = 'scene';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    dialog
      .querySelector('form')
      .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    await settle();
    expect(rpc).toHaveBeenCalledWith('extensions.operation', {
      name: 'mcp',
      operation: 'inspect',
      arguments: { id: 'example', query: 'scene', offset: 0 },
    });
    expect(
      rpc.mock.calls.some(([, params]) =>
        ['invoke', 'explore'].includes(params?.operation),
      ),
    ).toBe(false);
  });
  it('creates a local connection without a second Agent permission list', async () => {
    component = mount(Panel, { target: document.body });
    await settle();
    button('Add MCP connection').click();
    await settle();
    input('Connection name', 'blender');
    const nameInput = field('Connection name');
    expect(nameInput.pattern).toBe('[a-z][a-z0-9_]{0,31}');
    expect(nameInput.checkValidity()).toBe(true);
    input('Program', 'uvx');
    input('Description (optional)', ' Blender on the studio workstation ');
    button('Add argument').click();
    flushSync();
    input('Argument 1', 'blender-mcp');
    flushSync();
    submit();
    await settle();
    expect(rpc).toHaveBeenCalledWith('extensions.operation', {
      name: 'mcp',
      operation: 'save',
      arguments: {
        connection: expect.objectContaining({
          id: 'blender',
          description: 'Blender on the studio workstation',
          command: 'uvx',
          args: ['blender-mcp'],
        }),
      },
    });
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(
      document.querySelector('article[aria-label="blender"]'),
    ).toBeTruthy();
    // The new connection opens its details, where it can be tested.
    expect(
      document
        .querySelector('button[aria-label="Details for blender"]')
        .getAttribute('aria-expanded'),
    ).toBe('true');
  });
  it('preserves exact arguments and credentials when editing', async () => {
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
    flushSync();
    submit();
    await settle();
    expect(records[0].configuration).toMatchObject({
      command: 'python3',
      args: ['a b', ''],

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
    expect(field('Connection name').value).toBe('example');
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
  it('shows an OAuth sign-in and signs it out on request', async () => {
    records = [
      {
        id: 'remote',
        configuration: {
          id: 'remote',
          transport: 'http',
          url: 'https://mcp.example.com/mcp',
          oauth: true,
          enabled: true,
          timeout: 120,
        },
        oauth: {
          redirect_uri: 'http://127.0.0.1:8420/api/oauth/callback',
          signed_in: true,
        },
        state: 'connected',
      },
    ];
    component = mount(Panel, { target: document.body });
    await settle();
    const details = document.querySelector('article[aria-label="remote"]');
    expect(details.textContent).toContain('Signed in');
    // The redirect URL is what a pre-registered client must be registered with.
    expect(details.textContent).toContain(
      'http://127.0.0.1:8420/api/oauth/callback',
    );
    button('Sign in again').click();
    await settle();
    expect(rpc).toHaveBeenCalledWith('extensions.operation', {
      name: 'mcp',
      operation: 'reauthorize',
      arguments: { id: 'remote' },
    });
  });
  it('imports reviewed servers from pasted setup text with a typed credential', async () => {
    const files = {
      id: 'files',
      transport: 'stdio',
      command: 'npx',
      args: ['-y', '@scope/files server'],
      enabled: false,
      timeout: 120,
      credential_environment: { TOKEN: 'FILES_TOKEN' },
    };
    const preview = {
      servers: [
        {
          name: 'files',
          id: 'files',
          selected: true,
          enabled: false,
          connection: files,
          credentials: [
            {
              name: 'FILES_TOKEN',
              kind: 'environment',
              target: 'TOKEN',
              state: 'missing',
            },
          ],
          warnings: [{ code: 'test', message: 'test-owned-warning' }],
        },
        {
          name: 'example',
          id: 'example',
          conflict: true,
          selected: false,
          enabled: true,
          connection: { ...original, args: [] },
          credentials: [],
          warnings: [],
        },
      ],
    };
    records = [
      {
        id: 'example',
        configuration: structuredClone(original),
        state: 'connected',
      },
    ];
    const handler = rpc.getMockImplementation();
    rpc.mockImplementation(async (method, params) => {
      if (params?.operation !== 'import') return handler(method, params);
      if (!params.arguments.apply) return structuredClone(preview);
      records.push({
        id: 'files',
        configuration: structuredClone(files),
        state: 'disconnected',
      });
      return { imported: ['files'] };
    });
    component = mount(Panel, { target: document.body });
    await settle();
    button('Import').click();
    await settle();
    input('Setup text', 'test-owned-setup');
    submit();
    await settle();
    const dialog = document.querySelector('[role="dialog"]');
    expect(dialog.textContent).toContain('npx -y "@scope/files server"');
    expect(dialog.textContent).toContain('test-owned-warning');
    expect(dialog.textContent).toContain(
      'A connection named example already exists.',
    );
    input('Value for TOKEN (optional)', 'test-owned-secret');
    submit();
    await settle();
    const operations = rpc.mock.calls
      .map(([, params]) => params)
      .filter((params) =>
        ['import', 'credential', 'enable'].includes(params?.operation),
      )
      .map(({ operation, arguments: args }) => [operation, args]);
    expect(operations).toEqual([
      ['import', { source: 'test-owned-setup' }],
      [
        'import',
        {
          source: 'test-owned-setup',
          apply: true,
          servers: ['files'],
          ids: { files: 'files' },
        },
      ],
      [
        'credential',
        { id: 'files', key: 'FILES_TOKEN', value: 'test-owned-secret' },
      ],
      ['enable', { id: 'files' }],
    ]);
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(
      document
        .querySelector('button[aria-label="Details for files"]')
        .getAttribute('aria-expanded'),
    ).toBe('true');
    expect(document.body.textContent).not.toContain('test-owned-secret');
  });
  it('explains a failed connection with its server output and reconnects it', async () => {
    records = [
      {
        id: 'example',
        configuration: structuredClone(original),
        state: 'failed',
        error: 'test-owned-raw-error',
        problem: {
          code: 'command_not_found',
          command: 'python',
          requirement: 'Python 3',
          message: 'test-owned-server-advice',
        },
        stderr_tail: ['test-owned-first-line', 'test-owned-last-line'],
      },
      // Imported without its credential: it waits, enabled or not, and its
      // diagnosed problem is the waiting line itself.
      ...[false, true].map((enabled) => ({
        id: enabled ? 'files' : 'notes',
        configuration: { ...structuredClone(original), enabled },
        state: enabled ? 'failed' : 'disconnected',
        error: enabled ? 'Missing MCP credential: FILES_TOKEN' : null,
        problem: enabled
          ? { code: 'credential_missing', credential: 'FILES_TOKEN' }
          : null,
        missing_credentials: ['FILES_TOKEN', 'TEST_KEY'],
      })),
    ];
    component = mount(Panel, { target: document.body });
    await settle();
    const head = (id) =>
      document.querySelector(`article[aria-label="${id}"] .s-entity__head`);
    const row = head('example').querySelector('.mcp-connection__error');
    expect(row.textContent).toContain('Install Python 3');
    expect(row.textContent).not.toContain('test-owned-raw-error');
    expect(head('example').textContent).toContain('Connection failed');
    for (const id of ['files', 'notes']) {
      expect(head(id).textContent).toContain('Needs setup');
      expect(
        head(id).querySelector('.mcp-connection__waiting').textContent.trim(),
      ).toBe('Waiting for: FILES_TOKEN, TEST_KEY');
      expect(head(id).querySelector('.mcp-connection__error')).toBeNull();
      expect(head(id).textContent).not.toContain('Connection failed');
    }
    const details = document.querySelector('article[aria-label="example"]');
    expect(details.querySelector('pre').textContent).toBe(
      'test-owned-first-line\ntest-owned-last-line',
    );
    expect(details.textContent).toContain('test-owned-raw-error');
    button('Reconnect').click();
    await settle();
    expect(rpc).toHaveBeenCalledWith('extensions.operation', {
      name: 'mcp',
      operation: 'reconnect',
      arguments: { id: 'example' },
    });
  });
  it('reads the connections again when the Extension publishes a change', async () => {
    let listener = null;
    const subscribeInvalidations = (next) => {
      listener = next;
      return () => {
        listener = null;
      };
    };
    component = mount(Panel, {
      target: document.body,
      props: { subscribeInvalidations },
    });
    await settle();
    expect(document.querySelector('article')).toBeNull();
    records = [
      {
        id: 'example',
        configuration: structuredClone(original),
        state: 'connected',
      },
    ];
    listener({
      owner: 'mcp',
      change: { resource: 'connections', ids: ['example'], revision: 1 },
      revision: 1,
    });
    await settle();
    expect(
      document.querySelector('article[aria-label="example"]'),
    ).toBeTruthy();
    await unmount(component);
    component = null;
    expect(listener).toBeNull();
  });
  it('fills a new connection from a pasted URL and hands setup text to the import', async () => {
    const sse = {
      id: 'remote',
      transport: 'sse',
      url: 'https://mcp.example.com/sse',
      enabled: true,
      timeout: 120,
    };
    const handler = rpc.getMockImplementation();
    rpc.mockImplementation(async (method, params) => {
      if (params?.operation !== 'import') return handler(method, params);
      if (params.arguments.source.startsWith('https://'))
        return {
          servers: [
            {
              name: 'remote',
              id: 'remote',
              selected: true,
              enabled: true,
              connection: sse,
              credentials: [],
              warnings: [{ code: 'test', message: 'test-owned-warning' }],
            },
          ],
        };
      return {
        servers: ['one', 'two'].map((name) => ({
          name,
          id: name,
          selected: true,
          enabled: true,
          connection: { ...sse, id: name },
          credentials: [],
          warnings: [],
        })),
      };
    });
    component = mount(Panel, { target: document.body });
    await settle();
    button('Add MCP connection').click();
    await settle();
    input('Connection name', 'studio');
    input('Command line or URL (optional)', 'https://mcp.example.com/sse');
    button('Fill in').click();
    await settle();
    const dialog = document.querySelector('[role="dialog"]');
    expect(field('Connection name').value).toBe('studio');
    expect(field('Server URL').value).toBe('https://mcp.example.com/sse');
    expect(dialog.textContent).toContain('test-owned-warning');
    expect(dialog.textContent).toContain('Legacy SSE is deprecated');
    input('Command line or URL (optional)', 'test-owned-setup');
    button('Fill in').click();
    await settle();
    expect(document.querySelector('[role="dialog"]').textContent).toContain(
      'Import MCP servers',
    );
    expect(button('Import 2')).toBeTruthy();
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
  it('connects a catalog service, completes its sign-in and grants it to chosen Agents', async () => {
    const listeners = new Set();
    const subscribeInvalidations = (next) => {
      listeners.add(next);
      return () => listeners.delete(next);
    };
    const publish = (resource) => {
      for (const next of [...listeners])
        next({ owner: 'mcp', change: { resource, ids: [], revision: 1 } });
    };
    const tracker = {
      id: 'tracker',
      name: 'Tracker',
      description: 'test-owned-tracker',
      category: 'productivity',
      url: 'https://mcp.tracker.example/mcp',
      auth: 'oauth',
      read_only_url: 'https://mcp.tracker.example/read-only',
      notes: ['test-owned-note'],
      connections: [],
    };
    const search = {
      id: 'search',
      name: 'Web Search',
      description: 'test-owned-search',
      category: 'knowledge',
      url: 'https://mcp.search.example/mcp',
      auth: 'none',
      connections: ['search'],
    };
    const request = {
      id: 'test-owned-request',
      connection: 'tracker',
      kind: 'oauth',
      payload: { url: 'https://auth.tracker.example/authorize?state=s' },
      session_id: null,
      expires_at: new Date(Date.now() + 600_000).toISOString(),
    };
    const redirect = 'http://127.0.0.1:8420/api/oauth/callback?code=c&state=s';
    let status = null;
    const handler = rpc.getMockImplementation();
    rpc.mockImplementation(async (method, params) => {
      if (method === 'agent.list')
        return {
          agents: [
            { id: 'alice', name: 'Alice', tool_access: { mode: 'all' } },
            {
              id: 'bob',
              name: 'Bob',
              tool_access: { mode: 'all', granted: ['mcp_tracker'] },
            },
            {
              id: 'librarian',
              name: 'Librarian',
              builtin: 'librarian',
              tool_access: { mode: 'all' },
            },
          ],
        };
      if (method === 'tool.list')
        return {
          tools: [
            {
              name: 'mcp_tracker',
              activation: 'configurable',
              requires_opt_in: true,
            },
          ],
        };
      if (method === 'agent.update') return params;
      const operation = params?.operation;
      if (operation === 'catalog') return { entries: [tracker, search] };
      if (operation === 'add_from_catalog') {
        status = {
          id: 'tracker',
          state: 'connecting',
          configuration: {
            id: 'tracker',
            transport: 'http',
            url: tracker.read_only_url,
            oauth: true,
            enabled: true,
            timeout: 120,
          },
          pending_requests: [],
        };
        records = [status];
        return structuredClone(status);
      }
      if (operation === 'status') return structuredClone(status);
      if (operation === 'respond') return {};
      return handler(method, params);
    });
    const dialog = () => document.querySelector('[role="dialog"]');
    component = mount(Panel, {
      target: document.body,
      props: { subscribeInvalidations },
    });
    await settle();
    button('Browse the catalog').click();
    await settle();
    expect(dialog().textContent).toContain('test-owned-note');
    expect(
      dialog().querySelector('li[aria-label="Web Search"]').textContent,
    ).toContain('Saved as connection search.');
    const query = dialog().querySelector('input[aria-label="Search services"]');
    query.value = 'track';
    query.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    const tiles = [...dialog().querySelectorAll('li[aria-label]')];
    expect(tiles.map((tile) => tile.getAttribute('aria-label'))).toEqual([
      'Tracker',
    ]);
    tiles[0].querySelector('[role="checkbox"]').click();
    flushSync();
    tiles[0].querySelector('button[aria-label="Connect Tracker"]').click();
    await settle();
    expect(rpc).toHaveBeenCalledWith('extensions.operation', {
      name: 'mcp',
      operation: 'add_from_catalog',
      arguments: { entry: 'tracker', read_only: true },
    });
    expect(dialog().textContent).toContain('Preparing the sign-in');
    // Each step change moves focus to the title naming the new step.
    expect(document.activeElement.textContent).toBe('Sign in to Tracker');

    // The sign-in waits for the browser; the row offers it too.
    status.pending_requests = [request];
    publish('pending_inputs');
    await settle();
    expect(dialog().textContent).toContain(request.payload.url);
    expect(
      document.querySelector('article[aria-label="tracker"]').textContent,
    ).toContain('Sign in');

    // A browser on another device hands back where the sign-in went.
    dialog().querySelector('details summary').click();
    input('Redirected address', redirect);
    dialog()
      .querySelector('details form')
      .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    await settle();
    expect(rpc).toHaveBeenCalledWith('extensions.operation', {
      name: 'mcp',
      operation: 'respond',
      arguments: {
        request_id: request.id,
        response: { redirect_url: redirect, action: 'accept' },
      },
    });

    // Signed in: the user's Agents can be granted the connection Tool.
    status.pending_requests = [];
    status.state = 'connected';
    publish('connections');
    await settle();
    expect(dialog().textContent).toContain('Signed in to Tracker.');
    expect(document.activeElement.textContent).toBe(
      'Choose Agents for Tracker',
    );
    const agents = [...dialog().querySelectorAll('[role="checkbox"]')];
    expect(agents.map((agent) => agent.textContent.trim())).toEqual([
      'Alice',
      'Bob',
    ]);
    expect(agents[1].disabled).toBe(true);
    agents[0].click();
    flushSync();
    button('Allow access').click();
    await settle();
    expect(
      rpc.mock.calls.filter(([method]) => method === 'agent.update'),
    ).toEqual([
      [
        'agent.update',
        { id: 'alice', tool_access: { mode: 'all', granted: ['mcp_tracker'] } },
      ],
    ]);
    expect(dialog()).toBeNull();
    expect(
      document.querySelector(
        'article[aria-label="tracker"] .mcp-connection__details',
      ).hidden,
    ).toBe(false);
    expect(document.activeElement.getAttribute('aria-label')).toBe(
      'Details for tracker',
    );
  });
  it('names a further connection to a catalog service and the Agents using the first one', async () => {
    const tracker = {
      id: 'tracker',
      name: 'Tracker',
      description: 'test-owned-tracker',
      category: 'productivity',
      url: 'https://mcp.tracker.example/mcp',
      auth: 'oauth',
      connections: ['tracker', 'tracker_2'],
    };
    const status = {
      id: 'tracker_2',
      state: 'connecting',
      configuration: {
        id: 'tracker_2',
        transport: 'http',
        url: tracker.url,
        oauth: true,
        enabled: true,
        timeout: 120,
      },
      pending_requests: [
        {
          id: 'test-owned-request',
          connection: 'tracker_2',
          kind: 'oauth',
          payload: { url: 'https://auth.tracker.example/authorize?state=s' },
          session_id: null,
          expires_at: new Date(Date.now() + 600_000).toISOString(),
        },
      ],
    };
    records = [status];
    const listeners = new Set();
    const handler = rpc.getMockImplementation();
    rpc.mockImplementation(async (method, params) => {
      if (method === 'agent.list')
        return {
          agents: [
            {
              id: 'alice',
              name: 'Alice',
              tool_access: { mode: 'all', granted: ['mcp_tracker'] },
            },
          ],
        };
      if (method === 'tool.list') return { tools: [] };
      if (params?.operation === 'catalog') return { entries: [tracker] };
      if (params?.operation === 'status') return structuredClone(status);
      return handler(method, params);
    });
    const dialog = () => document.querySelector('[role="dialog"]');
    component = mount(Panel, {
      target: document.body,
      props: {
        subscribeInvalidations: (next) => {
          listeners.add(next);
          return () => listeners.delete(next);
        },
      },
    });
    await settle();
    document.querySelector('button[aria-label="Sign in to tracker_2"]').click();
    await settle();
    // Opened from the row, the dialog finds the service by the connection.
    expect(dialog().querySelector('h3').textContent).toBe(
      'Sign in to Tracker (tracker_2)',
    );

    status.pending_requests = [];
    status.state = 'connected';
    for (const next of [...listeners])
      next({ owner: 'mcp', change: { resource: 'connections' } });
    await settle();
    expect(document.activeElement.textContent).toBe(
      'Choose Agents for Tracker (tracker_2)',
    );
    const [alice] = dialog().querySelectorAll('[role="checkbox"]');
    expect(alice.disabled).toBe(false);
    expect(
      document.getElementById(alice.getAttribute('aria-describedby'))
        .textContent,
    ).toBe('Already uses tracker');

    // Skipping closes the dialog; the connection's row takes focus.
    button('Skip').click();
    await settle();
    expect(dialog()).toBeNull();
    expect(document.activeElement.getAttribute('aria-label')).toBe(
      'Details for tracker_2',
    );
  });
});
