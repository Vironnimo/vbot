import { describe, expect, it, vi } from 'vitest';
import {
  RPC_ERROR_INVALID_CLIENT_REQUEST,
  addProject,
  addAgentMemory,
  clearOverride,
  detectProject,
  setOverride,
  listProjects,
  listAgentMemories,
  removeProject,
  removeAgentMemory,
  setProject,
  showProject,
  reorderAgents,
  renameAgent,
  replaceAgentMemory,
} from '../api.js';
import { jsonResponse } from './api.support.js';

describe('agent API', () => {
  it('posts the explicit Agent rename contract', async () => {
    const fetchFunction = vi
      .fn()
      .mockResolvedValue(
        jsonResponse({ ok: true, result: { id: 'researcher' } }),
      );

    const result = await renameAgent('coder', 'researcher', {
      baseUrl: 'http://localhost:8420',
      fetch: fetchFunction,
    });

    expect(result).toEqual({ id: 'researcher' });
    expect(JSON.parse(fetchFunction.mock.calls[0][1].body)).toEqual({
      method: 'agent.rename',
      params: { id: 'coder', new_id: 'researcher' },
    });
  });

  it('posts the complete Agent order with its expected revision', async () => {
    const fetchFunction = vi.fn().mockResolvedValue(
      jsonResponse({
        ok: true,
        result: { agents: [], order_revision: 4 },
      }),
    );

    await reorderAgents(['writer', 'coder'], 3, { fetch: fetchFunction });

    expect(JSON.parse(fetchFunction.mock.calls[0][1].body)).toEqual({
      method: 'agent.reorder',
      params: {
        agent_ids: ['writer', 'coder'],
        expected_revision: 3,
      },
    });
  });

  it('posts the structured Memory CRUD contracts', async () => {
    const fetchFunction = vi
      .fn()
      .mockResolvedValue(jsonResponse({ ok: true, result: { scopes: {} } }));

    await listAgentMemories('coder', { fetch: fetchFunction });
    await addAgentMemory('coder', 'agent', 'Keep tests focused.', {
      fetch: fetchFunction,
    });
    await replaceAgentMemory('coder', 'agent', 2, 'Keep tests deterministic.', {
      fetch: fetchFunction,
    });
    await removeAgentMemory('coder', 'agent', 2, { fetch: fetchFunction });

    expect(
      fetchFunction.mock.calls.map((call) => JSON.parse(call[1].body)),
    ).toEqual([
      { method: 'memory.list', params: { agent_id: 'coder' } },
      {
        method: 'memory.add',
        params: {
          agent_id: 'coder',
          scope: 'agent',
          content: 'Keep tests focused.',
        },
      },
      {
        method: 'memory.replace',
        params: {
          agent_id: 'coder',
          scope: 'agent',
          entry_id: 2,
          content: 'Keep tests deterministic.',
        },
      },
      {
        method: 'memory.remove',
        params: { agent_id: 'coder', scope: 'agent', entry_id: 2 },
      },
    ]);
  });

  it('rejects invalid Memory scope and entry ids before sending', () => {
    expect(() => addAgentMemory('coder', 'other', 'Fact')).toThrow(
      expect.objectContaining({
        code: RPC_ERROR_INVALID_CLIENT_REQUEST,
        method: 'memory.add',
      }),
    );
    expect(() => removeAgentMemory('coder', 'agent', 0)).toThrow(
      expect.objectContaining({
        code: RPC_ERROR_INVALID_CLIENT_REQUEST,
        method: 'memory.remove',
      }),
    );
  });
});

describe('project.* wrappers', () => {
  it('adds a project through project.add', async () => {
    const fetchFunction = vi.fn().mockResolvedValue(
      jsonResponse({
        ok: true,
        result: { project: { project_id: 'demo' }, scan: { team: [] } },
      }),
    );

    await expect(
      addProject(
        {
          cwd: 'C:/repos/demo',
          display_name: 'Demo',
          default_agent: 'builder',
          default_model: 'openai/gpt-5.2',
          auto_load: ['AGENTS.md'],
        },
        { fetch: fetchFunction },
      ),
    ).resolves.toEqual({ project: { project_id: 'demo' }, scan: { team: [] } });

    expect(JSON.parse(fetchFunction.mock.calls[0][1].body)).toEqual({
      method: 'project.add',
      params: {
        cwd: 'C:/repos/demo',
        display_name: 'Demo',
        default_agent: 'builder',
        default_model: 'openai/gpt-5.2',
        auto_load: ['AGENTS.md'],
      },
    });
  });

  it('rejects a missing cwd before sending project.add', () => {
    expect(() => addProject({ display_name: 'Demo' })).toThrow(
      expect.objectContaining({
        code: RPC_ERROR_INVALID_CLIENT_REQUEST,
        method: 'project.add',
      }),
    );
    expect(() => addProject({ cwd: '' })).toThrow(
      expect.objectContaining({
        code: RPC_ERROR_INVALID_CLIENT_REQUEST,
        method: 'project.add',
      }),
    );
  });

  it('probes a cwd through project.detect', async () => {
    const fetchFunction = vi.fn().mockResolvedValue(
      jsonResponse({
        ok: true,
        result: { cwd_exists: true, formats: {}, context_files: {} },
      }),
    );

    await expect(
      detectProject('C:/repos/demo', { fetch: fetchFunction }),
    ).resolves.toEqual({ cwd_exists: true, formats: {}, context_files: {} });

    expect(JSON.parse(fetchFunction.mock.calls[0][1].body)).toEqual({
      method: 'project.detect',
      params: { cwd: 'C:/repos/demo' },
    });
  });

  it('rejects a missing cwd before sending project.detect', () => {
    expect(() => detectProject('')).toThrow(
      expect.objectContaining({
        code: RPC_ERROR_INVALID_CLIENT_REQUEST,
        method: 'project.detect',
      }),
    );
  });

  it('lists projects through project.list with no params', async () => {
    const fetchFunction = vi
      .fn()
      .mockResolvedValue(jsonResponse({ ok: true, result: { projects: [] } }));

    await expect(listProjects({ fetch: fetchFunction })).resolves.toEqual({
      projects: [],
    });

    expect(JSON.parse(fetchFunction.mock.calls[0][1].body)).toEqual({
      method: 'project.list',
      params: {},
    });
  });

  it('shows a project through project.show', async () => {
    const fetchFunction = vi
      .fn()
      .mockResolvedValue(
        jsonResponse({ ok: true, result: { project: { project_id: 'demo' } } }),
      );

    await showProject('demo', { fetch: fetchFunction });

    expect(JSON.parse(fetchFunction.mock.calls[0][1].body)).toEqual({
      method: 'project.show',
      params: { project_id: 'demo' },
    });
  });

  it('rejects an empty project id before sending project.show', () => {
    expect(() => showProject('')).toThrow(
      expect.objectContaining({
        code: RPC_ERROR_INVALID_CLIENT_REQUEST,
        method: 'project.show',
      }),
    );
  });

  it('updates a project through project.set with the id merged in', async () => {
    const fetchFunction = vi
      .fn()
      .mockResolvedValue(
        jsonResponse({ ok: true, result: { project: { project_id: 'demo' } } }),
      );

    await setProject(
      'demo',
      { display_name: 'Renamed', cwd: 'C:/repos/moved' },
      { fetch: fetchFunction },
    );

    expect(JSON.parse(fetchFunction.mock.calls[0][1].body)).toEqual({
      method: 'project.set',
      params: {
        display_name: 'Renamed',
        cwd: 'C:/repos/moved',
        project_id: 'demo',
      },
    });
  });

  it('rejects an empty project id or non-object changes before sending project.set', () => {
    expect(() => setProject('', { cwd: 'x' })).toThrow(
      expect.objectContaining({
        code: RPC_ERROR_INVALID_CLIENT_REQUEST,
        method: 'project.set',
      }),
    );
    expect(() => setProject('demo', null)).toThrow(
      expect.objectContaining({
        code: RPC_ERROR_INVALID_CLIENT_REQUEST,
        method: 'project.set',
      }),
    );
  });

  it('removes a project through project.rm', async () => {
    const fetchFunction = vi.fn().mockResolvedValue(
      jsonResponse({
        ok: true,
        result: { project_id: 'demo', archived: true },
      }),
    );

    await removeProject('demo', { fetch: fetchFunction });

    expect(JSON.parse(fetchFunction.mock.calls[0][1].body)).toEqual({
      method: 'project.rm',
      params: {
        project_id: 'demo',
        copy_rooted_agent_identity_files: false,
      },
    });
  });

  it('rejects an empty project id before sending project.rm', () => {
    expect(() => removeProject('')).toThrow(
      expect.objectContaining({
        code: RPC_ERROR_INVALID_CLIENT_REQUEST,
        method: 'project.rm',
      }),
    );
  });

  it('sends the aggregate Rooted-Agent copy choice through project.rm', async () => {
    const fetchFunction = vi
      .fn()
      .mockResolvedValue(
        jsonResponse({ ok: true, result: { project_id: 'demo' } }),
      );

    await removeProject('demo', true, { fetch: fetchFunction });

    expect(JSON.parse(fetchFunction.mock.calls[0][1].body).params).toEqual({
      project_id: 'demo',
      copy_rooted_agent_identity_files: true,
    });
  });

  it('sets a per-agent override through project.set_override', async () => {
    const fetchFunction = vi.fn().mockResolvedValue(
      jsonResponse({
        ok: true,
        result: { project: { project_id: 'demo' }, scan: { team: [] } },
      }),
    );

    await setOverride('demo', 'builder', 'model', 'openai/gpt-mini', {
      fetch: fetchFunction,
    });

    expect(JSON.parse(fetchFunction.mock.calls[0][1].body)).toEqual({
      method: 'project.set_override',
      params: {
        project_id: 'demo',
        agent_id: 'builder',
        field: 'model',
        value: 'openai/gpt-mini',
      },
    });
  });

  it('sets a numeric temperature override value verbatim', async () => {
    const fetchFunction = vi.fn().mockResolvedValue(
      jsonResponse({
        ok: true,
        result: { project: { project_id: 'demo' }, scan: { team: [] } },
      }),
    );

    await setOverride('demo', 'builder', 'temperature', 0, {
      fetch: fetchFunction,
    });

    expect(JSON.parse(fetchFunction.mock.calls[0][1].body).params.value).toBe(
      0,
    );
  });

  it('rejects a missing id or field before sending set_override', () => {
    expect(() => setOverride('', 'builder', 'model', 'x')).toThrow(
      expect.objectContaining({
        code: RPC_ERROR_INVALID_CLIENT_REQUEST,
        method: 'project.set_override',
      }),
    );
    expect(() => setOverride('demo', '', 'model', 'x')).toThrow(
      expect.objectContaining({
        code: RPC_ERROR_INVALID_CLIENT_REQUEST,
        method: 'project.set_override',
      }),
    );
    expect(() => setOverride('demo', 'builder', '', 'x')).toThrow(
      expect.objectContaining({
        code: RPC_ERROR_INVALID_CLIENT_REQUEST,
        method: 'project.set_override',
      }),
    );
  });

  it('clears one overridden field through project.clear_override', async () => {
    const fetchFunction = vi.fn().mockResolvedValue(
      jsonResponse({
        ok: true,
        result: { project: { project_id: 'demo' }, scan: { team: [] } },
      }),
    );

    await clearOverride('demo', 'builder', 'model', { fetch: fetchFunction });

    expect(JSON.parse(fetchFunction.mock.calls[0][1].body)).toEqual({
      method: 'project.clear_override',
      params: { project_id: 'demo', agent_id: 'builder', field: 'model' },
    });
  });

  it('rejects a missing id or field before sending clear_override', () => {
    expect(() => clearOverride('', 'builder', 'model')).toThrow(
      expect.objectContaining({
        code: RPC_ERROR_INVALID_CLIENT_REQUEST,
        method: 'project.clear_override',
      }),
    );
    expect(() => clearOverride('demo', '', 'model')).toThrow(
      expect.objectContaining({
        code: RPC_ERROR_INVALID_CLIENT_REQUEST,
        method: 'project.clear_override',
      }),
    );
    expect(() => clearOverride('demo', 'builder', '')).toThrow(
      expect.objectContaining({
        code: RPC_ERROR_INVALID_CLIENT_REQUEST,
        method: 'project.clear_override',
      }),
    );
  });
});
