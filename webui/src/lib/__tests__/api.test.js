import { describe, expect, it, vi } from 'vitest';
import {
  RPC_ERROR_HTTP,
  RPC_ERROR_INVALID_CLIENT_REQUEST,
  RPC_ERROR_NETWORK,
  RPC_ERROR_RESPONSE,
  acknowledgeSessionStoreIncident,
  createRpcEnvelope,
  createSessionStoreSnapshot,
  getSessionStoreStatus,
  getStatisticsReport,
  inspectSubAgentWork,
  listQueue,
  listLogs,
  readLogFile,
  removeFromQueue,
  rpc,
  setSkillDisabled,
  shareSkill,
  updateQueueItem,
} from '../api.js';
import { jsonResponse } from './api.support.js';

describe('createRpcEnvelope()', () => {
  it('creates the server RPC envelope', () => {
    expect(createRpcEnvelope('agent.list')).toEqual({
      method: 'agent.list',
      params: {},
    });
  });

  it('rejects invalid method and params before sending', () => {
    expect(() => createRpcEnvelope('', {})).toThrow(
      expect.objectContaining({ code: RPC_ERROR_INVALID_CLIENT_REQUEST }),
    );
    expect(() => createRpcEnvelope('agent.list', [])).toThrow(
      expect.objectContaining({ code: RPC_ERROR_INVALID_CLIENT_REQUEST }),
    );
  });
});

describe('Skill mutation validation', () => {
  it('reports the public validation message and method for boolean flags', () => {
    expect(() => setSkillDisabled('review', 'yes')).toThrow(
      expect.objectContaining({
        code: RPC_ERROR_INVALID_CLIENT_REQUEST,
        message: 'Disabled flag must be a boolean',
        method: 'skill.set_disabled',
      }),
    );
    expect(() => shareSkill('review', 'yes')).toThrow(
      expect.objectContaining({
        code: RPC_ERROR_INVALID_CLIENT_REQUEST,
        message: 'Shared flag must be a boolean',
        method: 'skill.share',
      }),
    );
  });
});

describe('rpc()', () => {
  it('posts an RPC envelope and returns the result', async () => {
    const fetchFunction = vi
      .fn()
      .mockResolvedValue(jsonResponse({ ok: true, result: { agents: [] } }));

    const result = await rpc(
      'agent.list',
      { visible: true },
      { baseUrl: 'http://localhost:8420/', fetch: fetchFunction },
    );

    expect(result).toEqual({ agents: [] });
    expect(fetchFunction).toHaveBeenCalledWith(
      'http://localhost:8420/api/rpc',
      {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          method: 'agent.list',
          params: { visible: true },
        }),
        signal: undefined,
      },
    );
  });

  it('exposes Session-store status, snapshot, and incident acknowledgement RPCs', async () => {
    const fetchFunction = vi
      .fn()
      .mockResolvedValue(
        jsonResponse({ ok: true, result: { state: 'ready' } }),
      );

    await expect(
      getSessionStoreStatus({ fetch: fetchFunction }),
    ).resolves.toEqual({
      state: 'ready',
    });
    await expect(
      createSessionStoreSnapshot('manual', { fetch: fetchFunction }),
    ).resolves.toEqual({ state: 'ready' });
    await expect(
      acknowledgeSessionStoreIncident('incident-1', { fetch: fetchFunction }),
    ).resolves.toEqual({ state: 'ready' });

    expect(
      fetchFunction.mock.calls.map(([url, init]) => [
        url,
        JSON.parse(init.body),
      ]),
    ).toEqual([
      ['/api/rpc', { method: 'session_store.status', params: {} }],
      [
        '/api/rpc',
        {
          method: 'session_store.snapshot_create',
          params: { reason: 'manual' },
        },
      ],
      [
        '/api/rpc',
        {
          method: 'session_store.incident_acknowledge',
          params: { incident_id: 'incident-1' },
        },
      ],
    ]);
  });

  it('normalizes server RPC errors', async () => {
    const fetchFunction = vi.fn().mockResolvedValue(
      jsonResponse(
        {
          ok: false,
          error: { code: 'active_run', message: 'session is busy' },
        },
        { status: 200 },
      ),
    );

    await expect(
      rpc('chat.stream', {}, { fetch: fetchFunction }),
    ).rejects.toMatchObject({
      name: 'ApiClientError',
      code: 'active_run',
      message: 'session is busy',
      method: 'chat.stream',
      status: 200,
    });
  });

  it('normalizes HTTP errors even when the body is an RPC error envelope', async () => {
    const fetchFunction = vi.fn().mockResolvedValue(
      jsonResponse(
        {
          ok: false,
          error: { code: 'domain_error', message: 'agent does not exist' },
        },
        { ok: false, status: 404 },
      ),
    );

    await expect(
      rpc('agent.update', {}, { fetch: fetchFunction }),
    ).rejects.toMatchObject({
      code: 'domain_error',
      message: 'agent does not exist',
      status: 404,
    });
  });

  it('uses a predictable fallback for non-RPC HTTP errors', async () => {
    const fetchFunction = vi
      .fn()
      .mockResolvedValue(jsonResponse({ detail: 'Not Found' }, { ok: false }));

    await expect(
      rpc('agent.list', {}, { fetch: fetchFunction }),
    ).rejects.toMatchObject({
      code: RPC_ERROR_HTTP,
      message: 'RPC request failed with HTTP 500',
      status: 500,
    });
  });

  it('normalizes network and malformed response failures', async () => {
    const networkFetch = vi.fn().mockRejectedValue(new Error('offline'));
    const malformedFetch = vi
      .fn()
      .mockResolvedValue(jsonResponse({ result: {} }));

    await expect(
      rpc('agent.list', {}, { fetch: networkFetch }),
    ).rejects.toMatchObject({
      code: RPC_ERROR_NETWORK,
    });
    await expect(
      rpc('agent.list', {}, { fetch: malformedFetch }),
    ).rejects.toMatchObject({
      code: RPC_ERROR_RESPONSE,
    });
  });

  it('forwards the Statistics window through the existing report RPC', async () => {
    const params = {
      since: '2026-06-01T00:00:00Z',
      until: '2026-06-07T12:00:00Z',
    };
    const fetchFunction = vi
      .fn()
      .mockResolvedValue(
        jsonResponse({ ok: true, result: { window: params } }),
      );
    await expect(
      getStatisticsReport(params, { fetch: fetchFunction }),
    ).resolves.toEqual({ window: params });
    expect(JSON.parse(fetchFunction.mock.calls[0][1].body)).toEqual({
      method: 'statistics.report',
      params,
    });
  });

  it('loads the logs catalog through log.list', async () => {
    const fetchFunction = vi.fn().mockResolvedValue(
      jsonResponse({
        ok: true,
        result: { files: ['2026-05-11'], default_file: '2026-05-11' },
      }),
    );

    await expect(listLogs({ fetch: fetchFunction })).resolves.toEqual({
      files: ['2026-05-11'],
      default_file: '2026-05-11',
    });

    expect(JSON.parse(fetchFunction.mock.calls[0][1].body)).toEqual({
      method: 'log.list',
      params: {},
    });
  });

  it('loads one log file through log.read', async () => {
    const fetchFunction = vi.fn().mockResolvedValue(
      jsonResponse({
        ok: true,
        result: {
          file: '2026-05-11',
          entries: [{ message: 'Ready' }],
        },
      }),
    );

    await expect(
      readLogFile('2026-05-11', { fetch: fetchFunction }),
    ).resolves.toEqual({
      file: '2026-05-11',
      entries: [{ message: 'Ready' }],
    });

    expect(JSON.parse(fetchFunction.mock.calls[0][1].body)).toEqual({
      method: 'log.read',
      params: { file: '2026-05-11' },
    });
  });

  it('rejects invalid log file names before sending log.read', async () => {
    expect(() => readLogFile('')).toThrow(
      expect.objectContaining({
        code: RPC_ERROR_INVALID_CLIENT_REQUEST,
        method: 'log.read',
      }),
    );
  });

  it('lists queued messages through chat.queue_list', async () => {
    const fetchFunction = vi.fn().mockResolvedValue(
      jsonResponse({
        ok: true,
        result: {
          items: [
            {
              id: 'queue-1',
              content: 'Queued message',
              created_at: '2026-05-22T01:00:00+00:00',
            },
          ],
        },
      }),
    );

    await expect(
      listQueue('agent-1', 'session-1', { fetch: fetchFunction }),
    ).resolves.toEqual({
      items: [
        {
          id: 'queue-1',
          content: 'Queued message',
          created_at: '2026-05-22T01:00:00+00:00',
        },
      ],
    });

    expect(JSON.parse(fetchFunction.mock.calls[0][1].body)).toEqual({
      method: 'chat.queue_list',
      params: { agent_id: 'agent-1', session_id: 'session-1' },
    });
  });

  it('removes queued messages through chat.queue_remove', async () => {
    const fetchFunction = vi
      .fn()
      .mockResolvedValue(jsonResponse({ ok: true, result: { ok: true } }));

    await expect(
      removeFromQueue('agent-1', 'session-1', 'queue-1', {
        fetch: fetchFunction,
      }),
    ).resolves.toEqual({ ok: true });

    expect(JSON.parse(fetchFunction.mock.calls[0][1].body)).toEqual({
      method: 'chat.queue_remove',
      params: {
        agent_id: 'agent-1',
        session_id: 'session-1',
        item_id: 'queue-1',
      },
    });
  });

  it('updates queued messages through chat.queue_update', async () => {
    const fetchFunction = vi
      .fn()
      .mockResolvedValue(jsonResponse({ ok: true, result: { ok: true } }));

    await expect(
      updateQueueItem('agent-1', 'session-1', 'queue-1', 'Updated content', {
        fetch: fetchFunction,
      }),
    ).resolves.toEqual({ ok: true });

    expect(JSON.parse(fetchFunction.mock.calls[0][1].body)).toEqual({
      method: 'chat.queue_update',
      params: {
        agent_id: 'agent-1',
        session_id: 'session-1',
        item_id: 'queue-1',
        content: 'Updated content',
      },
    });
  });
});

describe('inspectSubAgentWork()', () => {
  it('sends the durable Subagent work address through subagent.inspect', async () => {
    const fetchFunction = vi
      .fn()
      .mockResolvedValue(
        jsonResponse({ ok: true, result: { id: 'sub-work-one' } }),
      );
    const params = {
      id: 'sub-work-one',
      agent_id: 'worker@project-one',
      session_id: 'child-session',
    };

    await expect(
      inspectSubAgentWork(params, { fetch: fetchFunction }),
    ).resolves.toEqual({ id: 'sub-work-one' });

    expect(JSON.parse(fetchFunction.mock.calls[0][1].body)).toEqual({
      method: 'subagent.inspect',
      params,
    });
  });
});
