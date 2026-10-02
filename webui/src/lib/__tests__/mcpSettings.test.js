import { afterEach, describe, expect, it, vi } from 'vitest';
import { t } from '../i18n.js';
import {
  createMcpSettings,
  mcpConfiguration,
  mcpCredentialNames,
  mcpCredentialSlot,
  mcpDraft,
  mcpImportPlan,
  mcpProblemText,
} from '../mcpSettings.js';

const configuration = {
  id: 'example',
  description: 'Blender on the studio workstation',
  transport: 'stdio',
  command: 'python',
  args: ['', 'a b', 'ümlaut'],

  enabled: true,
  timeout: 240,
  sampling: 'ask',
  roots: 'workspace',
  environment: { LANG: 'de' },
  credential_environment: { TOKEN: 'SHARED_KEY' },
  credential_headers: {},
};
const oauthConfiguration = {
  id: 'remote',
  transport: 'http',
  url: 'https://mcp.example.com/mcp',
  oauth: true,
  oauth_client_id: 'vbot-client',
  oauth_client_secret: 'REMOTE_CLIENT_SECRET',
  oauth_scopes: ['files:read', 'files:write'],
  enabled: true,
  timeout: 120,
  sampling: 'off',
  roots: 'off',
  environment: {},
  credential_environment: {},
  credential_headers: { Extra: 'EXTRA_KEY' },
};
const clone = (value) => structuredClone(value);
const connectionsChange = {
  resource: 'connections',
  ids: ['example'],
  revision: 1,
};
const jobChange = { resource: 'jobs', ids: ['test-job'], revision: 2 };
let controller;
afterEach(() => {
  controller?.dispose();
});

function setup(operation) {
  let state;
  controller = createMcpSettings({
    onChange: (next) => {
      state = next;
    },
    operation,
  });
  return () => state;
}

describe('MCP settings', () => {
  it('inspects capabilities through the read-only management operation', async () => {
    const operation = vi.fn().mockResolvedValue({ tools: [], total: 0 });
    const state = setup(operation);
    await controller.inspect('example', { query: 'scene', offset: 10 });
    expect(operation).toHaveBeenCalledWith('mcp', 'inspect', {
      id: 'example',
      query: 'scene',
      offset: 10,
    });
    expect(state().inspector).toMatchObject({
      id: 'example',
      query: 'scene',
      loading: false,
      data: { total: 0 },
    });
  });
  it('ignores old searches and does not reopen a dismissed inspector', async () => {
    let finish;
    const operation = vi
      .fn()
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finish = resolve;
          }),
      )
      .mockResolvedValue({ total: 0 });
    const state = setup(operation);
    const pending = controller.inspect('first');
    await controller.inspect('second');
    controller.closeInspector();
    finish({ total: 99 });
    await pending;
    expect(state().inspector).toBeNull();
  });
  it('keeps inspector failures local and allows retry', async () => {
    const operation = vi
      .fn()
      .mockRejectedValueOnce(new Error('test-owned-inspect-error'))
      .mockResolvedValue({ total: 0 });
    const state = setup(operation);
    await controller.inspect('example');
    expect(state().inspector.error).toBe('test-owned-inspect-error');
    expect(state().error).toBe('');
    await controller.inspect('example');
    expect(state().inspector.error).toBe('');
  });
  it('preserves complete configuration and exact arguments on edit', () => {
    expect(mcpConfiguration(mcpDraft(configuration))).toEqual(configuration);
  });
  it('trims the description and leaves an empty one out', () => {
    const draft = mcpDraft(configuration);
    draft.description = '  Studio Blender  ';
    expect(mcpConfiguration(draft).description).toBe('Studio Blender');
    draft.description = '   ';
    expect(mcpConfiguration(draft)).not.toHaveProperty('description');
  });
  it('removes local-only settings when switching to HTTP', () => {
    const draft = mcpDraft(configuration);
    Object.assign(draft, {
      transport: 'http',
      url: 'http://localhost/mcp',
      oauth: true,
    });
    const result = mcpConfiguration(draft);
    expect(result).toMatchObject({
      transport: 'http',
      url: draft.url,
      oauth: true,
    });
    expect(result).not.toHaveProperty('command');
    expect(result).not.toHaveProperty('args');
  });
  it('keeps a pre-registered OAuth client only while OAuth is on', () => {
    const draft = mcpDraft(oauthConfiguration);
    expect(mcpConfiguration(draft)).toEqual(oauthConfiguration);
    expect(mcpCredentialNames(oauthConfiguration)).toEqual([
      'EXTRA_KEY',
      'REMOTE_CLIENT_SECRET',
    ]);
    draft.oauth_scopes = ' files:read  files:read admin ';
    expect(mcpConfiguration(draft).oauth_scopes).toEqual([
      'files:read',
      'admin',
    ]);
    const signedOut = mcpConfiguration({ ...draft, oauth: false });
    for (const field of [
      'oauth_client_id',
      'oauth_client_secret',
      'oauth_scopes',
    ])
      expect(signedOut).not.toHaveProperty(field);
  });
  it('rejects duplicate mapping keys instead of discarding an entry', () => {
    const draft = mcpDraft(configuration);
    draft.environment.push({ name: 'LANG', value: 'en' });
    expect(() => mcpConfiguration(draft)).toThrow();
  });
  it('does not overwrite a connection created by another client', async () => {
    const operation = vi
      .fn()
      .mockResolvedValue({ connections: [{ id: 'example', configuration }] });
    const state = setup(operation);
    expect(await controller.save(mcpDraft(configuration), null)).toBe(false);
    expect(operation.mock.calls.map((call) => call[1])).toEqual(['list']);
    expect(state().error).toBeTruthy();
  });
  it('keeps the draft unsaved when the existing connection changed elsewhere', async () => {
    const operation = vi.fn().mockResolvedValue({
      connections: [
        {
          id: 'example',
          configuration: { ...configuration, command: 'changed' },
        },
      ],
    });
    setup(operation);
    expect(await controller.save(mcpDraft(configuration), configuration)).toBe(
      false,
    );
    expect(operation).toHaveBeenCalledTimes(1);
  });
  it('saves through the management operation then reconciles the server record', async () => {
    let records = [];
    const operation = vi.fn(async (_extension, name, args) => {
      if (name === 'save')
        records = [
          {
            id: args.connection.id,
            configuration: args.connection,
            state: 'connecting',
          },
        ];
      return { connections: records };
    });
    const state = setup(operation);
    expect(await controller.save(mcpDraft(configuration), null)).toBe(true);
    expect(operation).toHaveBeenCalledWith('mcp', 'save', {
      connection: configuration,
    });
    expect(state().connections[0].configuration).toEqual(configuration);
  });
  it('finishes a pending test when its job change arrives, without exposing its catalog', async () => {
    let finished = false;
    const operation = vi.fn(async (_extension, name) => {
      if (name === 'test') return { job_id: 'test-job', state: 'running' };
      if (name === 'job')
        return finished
          ? {
              state: 'completed',
              result: {
                verified: ['catalog'],
                catalog: { secretSentinel: 'never-render' },
              },
            }
          : { state: 'running' };
      return { connections: [] };
    });
    const state = setup(operation);
    await controller.test('example');
    expect(state().job.job_id).toBe('test-job');
    finished = true;
    controller.handleInvalidation({ owner: 'mcp', change: jobChange });
    await vi.waitFor(() => expect(state().job).toBeNull());
    expect(JSON.stringify(state())).not.toContain('never-render');
    expect(state().notice).toBe(t('mcp.testPassed', { checks: 'catalog' }));
  });
  it('reports a failed test once and stops asking for its job', async () => {
    const operation = vi.fn(async (_extension, name) => {
      if (name === 'test') return { job_id: 'test-job', state: 'running' };
      if (name === 'job')
        return { state: 'failed', error: 'test-owned-failure' };
      return { connections: [] };
    });
    const state = setup(operation);
    await controller.test('example');
    expect(operation).toHaveBeenCalledWith('mcp', 'job', {
      job_id: 'test-job',
    });
    expect(state().error).toBe('test-owned-failure');
    const calls = operation.mock.calls.length;
    controller.handleInvalidation({ owner: 'mcp', change: jobChange });
    await vi.waitFor(() => expect(operation).toHaveBeenCalledTimes(calls + 1));
    expect(operation.mock.calls.at(-1)[1]).toBe('list');
    expect(state().error).toBe('test-owned-failure');
  });
  it('cancels a waiting job through the same backend contract', async () => {
    let cancelled = false;
    const operation = vi.fn(async (_extension, name) => {
      if (name === 'test') return { job_id: 'test-job', state: 'running' };
      if (name === 'cancel-job') cancelled = true;
      if (name === 'job') return { state: cancelled ? 'cancelled' : 'running' };
      return { connections: [] };
    });
    const state = setup(operation);
    await controller.test('example');
    await controller.cancel();
    expect(operation).toHaveBeenCalledWith('mcp', 'cancel-job', {
      job_id: 'test-job',
    });
    expect(state().job).toBeNull();
  });
  it('sends secret values only to the credential operation and never to view state', async () => {
    const operation = vi.fn().mockResolvedValue({ connections: [] });
    const state = setup(operation);
    await controller.credential('example', 'SHARED_KEY', 'test-owned-secret');
    expect(operation).toHaveBeenCalledWith('mcp', 'credential', {
      id: 'example',
      key: 'SHARED_KEY',
      value: 'test-owned-secret',
    });
    expect(JSON.stringify(state())).not.toContain('test-owned-secret');
  });
  it('ignores stale refresh responses and changes after disposal', async () => {
    let finish;
    const operation = vi
      .fn()
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finish = resolve;
          }),
      )
      .mockResolvedValue({ connections: [] });
    const state = setup(operation);
    const old = controller.refresh();
    await controller.refresh();
    finish({ connections: [clone(configuration)] });
    await old;
    expect(state().connections).toEqual([]);
    controller.dispose();
    const count = operation.mock.calls.length;
    controller.handleInvalidation({ owner: null, change: null });
    await Promise.resolve();
    expect(operation).toHaveBeenCalledTimes(count);
  });
  it('reads again for its own connection and job changes, once after a burst', async () => {
    const reads = [];
    const operation = vi.fn(
      () =>
        new Promise((resolve) => {
          reads.push(resolve);
        }),
    );
    setup(operation);
    controller.handleInvalidation({
      owner: 'calendar',
      change: connectionsChange,
    });
    controller.handleInvalidation({
      owner: 'mcp',
      change: { resource: 'pending_inputs', ids: ['input'], revision: 3 },
    });
    expect(operation).not.toHaveBeenCalled();
    controller.handleInvalidation({ owner: 'mcp', change: connectionsChange });
    controller.handleInvalidation({ owner: 'mcp', change: jobChange });
    controller.handleInvalidation({ owner: null, change: null });
    expect(operation).toHaveBeenCalledTimes(1);
    reads.shift()({ connections: [] });
    await vi.waitFor(() => expect(operation).toHaveBeenCalledTimes(2));
    reads.shift()({ connections: [] });
    await Promise.resolve();
    expect(operation).toHaveBeenCalledTimes(2);
  });
  it('clears a failed read on the next read but keeps an action error', async () => {
    let failRead = true;
    const operation = vi.fn(async (_extension, name) => {
      if (name === 'reconnect') throw new Error('test-owned-action-error');
      if (failRead) throw new Error('test-owned-read-error');
      return { connections: [] };
    });
    const state = setup(operation);
    await controller.refresh();
    expect(state().error).toBe('test-owned-read-error');
    failRead = false;
    await controller.refresh();
    expect(state().error).toBe('');
    expect(await controller.mutate('reconnect', 'example')).toBe(false);
    expect(operation).toHaveBeenCalledWith('mcp', 'reconnect', {
      id: 'example',
    });
    await controller.refresh();
    expect(state().error).toBe('test-owned-action-error');
  });
  it('words a diagnosed problem, preferring the install hint and the server advice for unknown codes', () => {
    expect(mcpProblemText(null)).toBe('');
    expect(
      mcpProblemText({
        code: 'command_not_found',
        command: 'npx',
        requirement: 'Node.js',
        message: 'server-owned advice',
      }),
    ).toBe(t('mcp.problemInstall', { command: 'npx', requirement: 'Node.js' }));
    expect(
      mcpProblemText({ code: 'unauthorized', status: 401, message: 'x' }),
    ).toBe(t('mcp.problem.unauthorized', { status: 401 }));
    expect(
      mcpProblemText({
        code: 'future_problem',
        message: 'server-owned advice',
      }),
    ).toBe('server-owned advice');
  });
  it('plans an import of the chosen servers with typed credentials', () => {
    const credential = (name, target, state) => ({ name, target, state });
    const servers = [
      {
        name: 'files',
        id: 'files',
        credentials: [
          credential('FILES_TOKEN', 'TOKEN', 'missing'),
          credential('FILES_KEY', 'Authorization', 'missing'),
        ],
      },
      {
        name: 'search',
        id: 'search_web',
        credentials: [credential('SEARCH_KEY', 'KEY', 'missing')],
      },
      {
        name: 'paused',
        id: 'paused',
        disabled: true,
        credentials: [credential('PAUSED_KEY', 'KEY', 'missing')],
      },
      { name: 'taken', id: 'taken', conflict: true, credentials: [] },
      { name: 'broken', id: 'broken', error: 'invalid', credentials: [] },
      { name: 'skipped', id: 'skipped', credentials: [] },
    ];
    const values = {
      [mcpCredentialSlot(servers[0], servers[0].credentials[0])]: 'one',
      [mcpCredentialSlot(servers[0], servers[0].credentials[1])]: 'two',
      [mcpCredentialSlot(servers[2], servers[2].credentials[0])]: 'three',
    };
    const chosen = new Set(['files', 'search', 'paused', 'taken', 'broken']);
    expect(mcpImportPlan(servers, chosen, values)).toEqual({
      servers: ['files', 'search', 'paused'],
      ids: { files: 'files', search: 'search_web', paused: 'paused' },
      credentials: [
        { id: 'files', key: 'FILES_TOKEN', value: 'one' },
        { id: 'files', key: 'FILES_KEY', value: 'two' },
        { id: 'paused', key: 'PAUSED_KEY', value: 'three' },
      ],
      enable: ['files'],
    });
  });
  it('imports, stores typed credentials, then enables the completed connections', async () => {
    const operation = vi.fn(async (_extension, name, args) => {
      if (name === 'import' && !args.apply) return { servers: [] };
      if (name === 'credential' && args.key === 'BAD')
        throw new Error('test-owned-credential-error');
      return { connections: [] };
    });
    const state = setup(operation);
    await controller.previewImport('{"mcpServers": {}}', { files: 'docs' });
    expect(operation).toHaveBeenLastCalledWith('mcp', 'import', {
      source: '{"mcpServers": {}}',
      ids: { files: 'docs' },
    });
    const plan = {
      servers: ['files'],
      ids: { files: 'docs' },
      credentials: [{ id: 'docs', key: 'FILES_TOKEN', value: 'secret' }],
      enable: ['docs'],
    };
    expect(await controller.importServers('setup', plan)).toEqual({
      ok: true,
      saved: true,
    });
    expect(operation.mock.calls.slice(-4).map((call) => call.slice(1))).toEqual(
      [
        [
          'import',
          {
            source: 'setup',
            apply: true,
            servers: ['files'],
            ids: { files: 'docs' },
          },
        ],
        ['credential', { id: 'docs', key: 'FILES_TOKEN', value: 'secret' }],
        ['enable', { id: 'docs' }],
        ['list', {}],
      ],
    );
    expect(state().notice).toBe(t('mcp.imported', { count: 1 }));
    expect(JSON.stringify(state())).not.toContain('secret');
    const failing = {
      ...plan,
      credentials: [{ id: 'docs', key: 'BAD', value: 'x' }],
    };
    expect(await controller.importServers('setup', failing)).toEqual({
      ok: false,
      saved: true,
    });
    expect(state().error).toBe('test-owned-credential-error');
  });
});
