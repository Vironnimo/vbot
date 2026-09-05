import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  createMcpSettings,
  mcpConfiguration,
  mcpDraft,
  MCP_REFRESH_MS,
} from '../mcpSettings.js';

const configuration = {
  id: 'example',
  transport: 'stdio',
  command: 'python',
  args: ['', 'a b', 'ümlaut'],
  agents: ['alice', 'coder@project'],
  enabled: true,
  timeout: 240,
  environment: { LANG: 'de' },
  credential_environment: { TOKEN: 'SHARED_KEY' },
  credential_headers: {},
};
const clone = (value) => structuredClone(value);
let controller;
afterEach(() => {
  controller?.dispose();
  vi.useRealTimers();
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
  it('preserves complete configuration, exact arguments, and grants on edit', () => {
    expect(mcpConfiguration(mcpDraft(configuration))).toEqual(configuration);
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
      agents: configuration.agents,
    });
    expect(result).not.toHaveProperty('command');
    expect(result).not.toHaveProperty('args');
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
        { id: 'example', configuration: { ...configuration, agents: [] } },
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
  it('polls a pending test to completion without exposing its catalog', async () => {
    vi.useFakeTimers();
    let checks = 0;
    const operation = vi.fn(async (_extension, name) => {
      if (name === 'test') return { job_id: 'test-job', state: 'running' };
      if (name === 'job')
        return ++checks === 1
          ? { state: 'running' }
          : {
              state: 'completed',
              result: {
                verified: ['catalog'],
                catalog: { secretSentinel: 'never-render' },
              },
            };
      return { connections: [] };
    });
    const state = setup(operation);
    await controller.test('example');
    expect(state().job.job_id).toBe('test-job');
    await vi.advanceTimersByTimeAsync(MCP_REFRESH_MS);
    expect(state().job).toBeNull();
    expect(JSON.stringify(state())).not.toContain('never-render');
    expect(state().notice).toBeTruthy();
  });
  it('reports test failure and stops background retries', async () => {
    vi.useFakeTimers();
    const operation = vi.fn(async (_extension, name) => {
      if (name === 'test') return { job_id: 'test-job', state: 'running' };
      return { state: 'failed', error: 'test-owned-failure' };
    });
    const state = setup(operation);
    await controller.test('example');
    const calls = operation.mock.calls.length;
    await vi.advanceTimersByTimeAsync(MCP_REFRESH_MS * 3);
    expect(state().error).toBe('test-owned-failure');
    expect(operation).toHaveBeenCalledTimes(calls);
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
  it('ignores stale refresh responses and releases timers on disposal', async () => {
    vi.useFakeTimers();
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
    await vi.advanceTimersByTimeAsync(MCP_REFRESH_MS * 3);
    expect(operation).toHaveBeenCalledTimes(count);
  });
});
