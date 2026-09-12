import { describe, expect, it, vi } from 'vitest';
import {
  RPC_ERROR_INVALID_CLIENT_REQUEST,
  cancelProcess,
  cancelRun,
  cancelToolCall,
  listSessionActivity,
  listSessions,
  deleteSession,
  renameSession,
  getTaskModelOptions,
  listTaskModelTargets,
  updateTaskModelSettings,
} from '../api.js';
import { jsonResponse } from './api.support.js';

describe('renameSession()', () => {
  it('renames a session through session.rename', async () => {
    const fetchFunction = vi
      .fn()
      .mockResolvedValue(
        jsonResponse({ ok: true, result: { title: 'Release planning' } }),
      );

    await expect(
      renameSession('alpha', 'session-1', 'Release planning', {
        fetch: fetchFunction,
      }),
    ).resolves.toEqual({ title: 'Release planning' });

    expect(JSON.parse(fetchFunction.mock.calls[0][1].body)).toEqual({
      method: 'session.rename',
      params: {
        agent_id: 'alpha',
        session_id: 'session-1',
        title: 'Release planning',
      },
    });
  });

  it('sends an empty title verbatim as the clear signal', async () => {
    const fetchFunction = vi
      .fn()
      .mockResolvedValue(jsonResponse({ ok: true, result: { title: null } }));

    await renameSession('alpha', 'session-1', '', { fetch: fetchFunction });

    expect(JSON.parse(fetchFunction.mock.calls[0][1].body)).toEqual({
      method: 'session.rename',
      params: { agent_id: 'alpha', session_id: 'session-1', title: '' },
    });
  });

  it('rejects an empty agent or session id before sending', () => {
    expect(() => renameSession('', 'session-1', 'x')).toThrow(
      expect.objectContaining({
        code: RPC_ERROR_INVALID_CLIENT_REQUEST,
        method: 'session.rename',
      }),
    );
    expect(() => renameSession('alpha', '', 'x')).toThrow(
      expect.objectContaining({
        code: RPC_ERROR_INVALID_CLIENT_REQUEST,
        method: 'session.rename',
      }),
    );
  });
});

describe('listSessions()', () => {
  it('posts one bounded multi-Agent page with filters and a cursor', async () => {
    const fetchFunction = vi.fn().mockResolvedValue(
      jsonResponse({
        ok: true,
        result: { sessions: [], next_cursor: null, total_count: 0 },
      }),
    );
    const cursor = {
      active_sort: 2460000,
      agent_id: 'alpha',
      session_id: 'session-35',
    };

    await listSessions(
      ['alpha', 'builder@vbot'],
      {
        limit: 20,
        cursor,
        includeSubagents: false,
        includeMemoryReflections: false,
        includeSkillReflections: true,
        includeCron: false,
        requiredSession: { agentId: 'alpha', sessionId: 'session-1' },
      },
      { fetch: fetchFunction },
    );

    expect(JSON.parse(fetchFunction.mock.calls[0][1].body)).toEqual({
      method: 'session.list',
      params: {
        agent_ids: ['alpha', 'builder@vbot'],
        limit: 20,
        cursor,
        include_subagents: false,
        include_memory_reflections: false,
        include_skill_reflections: true,
        include_cron: false,
        required_session: { agent_id: 'alpha', session_id: 'session-1' },
      },
    });
  });
});

describe('listSessionActivity()', () => {
  it('posts one batched Agent-address request', async () => {
    const fetchFunction = vi.fn().mockResolvedValue(
      jsonResponse({
        ok: true,
        result: { agents: [] },
      }),
    );

    await expect(
      listSessionActivity(['alpha', 'builder@vbot'], {
        fetch: fetchFunction,
      }),
    ).resolves.toEqual({ agents: [] });

    expect(JSON.parse(fetchFunction.mock.calls[0][1].body)).toEqual({
      method: 'session.activity_list',
      params: { agent_ids: ['alpha', 'builder@vbot'] },
    });
  });

  it('accepts an empty batch and rejects malformed Agent ids locally', async () => {
    const fetchFunction = vi
      .fn()
      .mockResolvedValue(jsonResponse({ ok: true, result: { agents: [] } }));

    await expect(
      listSessionActivity([], { fetch: fetchFunction }),
    ).resolves.toEqual({ agents: [] });
    expect(() => listSessionActivity(['alpha', '  '])).toThrow(
      expect.objectContaining({
        code: RPC_ERROR_INVALID_CLIENT_REQUEST,
        method: 'session.activity_list',
      }),
    );
  });
});

describe('deleteSession()', () => {
  it('deletes a session through session.delete', async () => {
    const fetchFunction = vi.fn().mockResolvedValue(
      jsonResponse({
        ok: true,
        result: {
          agent_id: 'alpha',
          session_id: 'session-1',
          next_session_id: 'session-2',
        },
      }),
    );

    await expect(
      deleteSession('alpha', 'session-1', { fetch: fetchFunction }),
    ).resolves.toEqual({
      agent_id: 'alpha',
      session_id: 'session-1',
      next_session_id: 'session-2',
    });

    expect(JSON.parse(fetchFunction.mock.calls[0][1].body)).toEqual({
      method: 'session.delete',
      params: { agent_id: 'alpha', session_id: 'session-1' },
    });
  });

  it('rejects an empty agent or session id before sending', () => {
    expect(() => deleteSession('', 'session-1')).toThrow(
      expect.objectContaining({
        code: RPC_ERROR_INVALID_CLIENT_REQUEST,
        method: 'session.delete',
      }),
    );
    expect(() => deleteSession('alpha', '')).toThrow(
      expect.objectContaining({
        code: RPC_ERROR_INVALID_CLIENT_REQUEST,
        method: 'session.delete',
      }),
    );
  });
});

describe('cancelRun()', () => {
  it('cancels a run through chat.cancel with a user reason', async () => {
    const fetchFunction = vi
      .fn()
      .mockResolvedValue(jsonResponse({ ok: true, result: { ok: true } }));

    await expect(
      cancelRun('run-1', { reason: 'user' }, { fetch: fetchFunction }),
    ).resolves.toEqual({ ok: true });

    expect(JSON.parse(fetchFunction.mock.calls[0][1].body)).toEqual({
      method: 'chat.cancel',
      params: { run_id: 'run-1', reason: 'user' },
    });
  });

  it('omits reason when none is provided', async () => {
    const fetchFunction = vi
      .fn()
      .mockResolvedValue(jsonResponse({ ok: true, result: { ok: true } }));

    await cancelRun('run-2', undefined, { fetch: fetchFunction });

    expect(JSON.parse(fetchFunction.mock.calls[0][1].body)).toEqual({
      method: 'chat.cancel',
      params: { run_id: 'run-2' },
    });
  });

  it('rejects an empty run id before sending', () => {
    expect(() => cancelRun('', { reason: 'user' })).toThrow(
      expect.objectContaining({ code: RPC_ERROR_INVALID_CLIENT_REQUEST }),
    );
  });
});

describe('cancelToolCall()', () => {
  it('cancels a tool call through chat.cancel_tool_call', async () => {
    const fetchFunction = vi
      .fn()
      .mockResolvedValue(jsonResponse({ ok: true, result: { ok: true } }));

    await expect(
      cancelToolCall(
        { agentId: 'alpha', runId: 'run-1', toolCallId: 'call-1' },
        { fetch: fetchFunction },
      ),
    ).resolves.toEqual({ ok: true });

    expect(JSON.parse(fetchFunction.mock.calls[0][1].body)).toEqual({
      method: 'chat.cancel_tool_call',
      params: {
        agent_id: 'alpha',
        run_id: 'run-1',
        tool_call_id: 'call-1',
      },
    });
  });

  it('omits agent_id when not provided', async () => {
    const fetchFunction = vi
      .fn()
      .mockResolvedValue(jsonResponse({ ok: true, result: { ok: true } }));

    await cancelToolCall(
      { runId: 'run-1', toolCallId: 'call-1' },
      { fetch: fetchFunction },
    );

    expect(JSON.parse(fetchFunction.mock.calls[0][1].body)).toEqual({
      method: 'chat.cancel_tool_call',
      params: { run_id: 'run-1', tool_call_id: 'call-1' },
    });
  });

  it('rejects a missing run id or tool call id before sending', () => {
    expect(() => cancelToolCall({ toolCallId: 'call-1' })).toThrow(
      expect.objectContaining({ code: RPC_ERROR_INVALID_CLIENT_REQUEST }),
    );
    expect(() => cancelToolCall({ runId: 'run-1' })).toThrow(
      expect.objectContaining({ code: RPC_ERROR_INVALID_CLIENT_REQUEST }),
    );
  });
});

describe('cancelProcess()', () => {
  it('cancels an owned background Process through chat.cancel_process', async () => {
    const fetchFunction = vi.fn().mockResolvedValue(
      jsonResponse({
        ok: true,
        result: {
          process_id: 'process-1',
          status: 'cancelled',
        },
      }),
    );

    await expect(
      cancelProcess(
        {
          agentId: 'builder@project-one',
          processId: 'process-1',
        },
        { fetch: fetchFunction },
      ),
    ).resolves.toEqual({
      process_id: 'process-1',
      status: 'cancelled',
    });

    expect(JSON.parse(fetchFunction.mock.calls[0][1].body)).toEqual({
      method: 'chat.cancel_process',
      params: {
        agent_id: 'builder@project-one',
        process_id: 'process-1',
      },
    });
  });

  it('rejects a missing Agent or Process Session before sending', () => {
    expect(() => cancelProcess({ processId: 'process-1' })).toThrow(
      expect.objectContaining({ code: RPC_ERROR_INVALID_CLIENT_REQUEST }),
    );
    expect(() => cancelProcess({ agentId: 'builder' })).toThrow(
      expect.objectContaining({ code: RPC_ERROR_INVALID_CLIENT_REQUEST }),
    );
  });
});

describe('task model API helpers', () => {
  it('wrap task-model RPCs with validated params', async () => {
    const fetchFunction = vi
      .fn()
      .mockResolvedValue(jsonResponse({ ok: true, result: { targets: [] } }));

    await listTaskModelTargets('speech_to_text', { fetch: fetchFunction });

    expect(JSON.parse(fetchFunction.mock.calls[0][1].body)).toEqual({
      method: 'task_model.list_targets',
      params: { task_type: 'speech_to_text' },
    });

    expect(() => updateTaskModelSettings([])).toThrow(
      expect.objectContaining({ code: RPC_ERROR_INVALID_CLIENT_REQUEST }),
    );
    expect(() => getTaskModelOptions('', 'target')).toThrow(
      expect.objectContaining({ code: RPC_ERROR_INVALID_CLIENT_REQUEST }),
    );
  });
});
