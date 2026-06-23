import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  ApiClientError,
  RPC_ERROR_HTTP,
  RPC_ERROR_INVALID_CLIENT_REQUEST,
  RPC_ERROR_NETWORK,
  RPC_ERROR_RESPONSE,
  SSE_ERROR_RESPONSE,
  RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
  RUN_EVENT_REASONING_DELTA,
  RUN_EVENT_TOOL_CALL_DELTA,
  RUN_EVENT_TOOL_CALL_STDERR,
  RUN_EVENT_TOOL_CALL_STDOUT,
  RUN_EVENT_TYPES,
  WEBSOCKET_ERROR_RESPONSE,
  addProject,
  cancelRun,
  cancelToolCall,
  clearModelOverride,
  createRpcEnvelope,
  listProjects,
  removeProject,
  deleteSession,
  renameSession,
  setProject,
  showProject,
  getTaskModelOptions,
  listTaskModelTargets,
  listQueue,
  listLogs,
  normalizeRpcError,
  readLogFile,
  removeFromQueue,
  rpc,
  transcribeSpeech,
  uploadAttachment,
  subscribeLogEvents,
  subscribeRunEvents,
  subscribeServerEvents,
  updateQueueItem,
  updateTaskModelSettings,
} from '../api.js';

afterEach(() => {
  vi.unstubAllGlobals();
});

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
      params: { project_id: 'demo' },
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

  it('clears a model override through project.clear_model_override', async () => {
    const fetchFunction = vi.fn().mockResolvedValue(
      jsonResponse({
        ok: true,
        result: { project: { project_id: 'demo' }, scan: { team: [] } },
      }),
    );

    await clearModelOverride('demo', 'builder', { fetch: fetchFunction });

    expect(JSON.parse(fetchFunction.mock.calls[0][1].body)).toEqual({
      method: 'project.clear_model_override',
      params: { project_id: 'demo', agent_id: 'builder' },
    });
  });

  it('rejects an empty project or agent id before sending clear_model_override', () => {
    expect(() => clearModelOverride('', 'builder')).toThrow(
      expect.objectContaining({
        code: RPC_ERROR_INVALID_CLIENT_REQUEST,
        method: 'project.clear_model_override',
      }),
    );
    expect(() => clearModelOverride('demo', '')).toThrow(
      expect.objectContaining({
        code: RPC_ERROR_INVALID_CLIENT_REQUEST,
        method: 'project.clear_model_override',
      }),
    );
  });
});

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

describe('transcribeSpeech()', () => {
  it('uploads audio and returns the transcription payload', async () => {
    const fetchFunction = vi
      .fn()
      .mockResolvedValue(
        jsonResponse({ text: 'hello world' }, { status: 200 }),
      );

    const result = await transcribeSpeech(
      new Blob(['abc'], { type: 'audio/webm' }),
      {
        fetch: fetchFunction,
      },
    );

    expect(result).toEqual({ text: 'hello world' });
    expect(fetchFunction.mock.calls[0][0]).toBe('/api/speech/transcribe');
    expect(fetchFunction.mock.calls[0][1].method).toBe('POST');
    expect(fetchFunction.mock.calls[0][1].body).toBeInstanceOf(FormData);
  });
});

describe('normalizeRpcError()', () => {
  it('turns unknown error shapes into ApiClientError', () => {
    const error = normalizeRpcError(null, {
      method: 'agent.list',
      status: 200,
    });

    expect(error).toBeInstanceOf(ApiClientError);
    expect(error).toMatchObject({
      code: 'rpc_error',
      message: 'RPC request failed',
    });
  });
});

describe('uploadAttachment()', () => {
  it('returns embedded text_content when the upload payload includes it', async () => {
    const fetchFunction = vi.fn().mockResolvedValue(
      jsonResponse(
        {
          attachment_id: 'attachment-text-1',
          filename: 'notes.txt',
          media_type: 'text/plain',
          size_bytes: 5,
          text_content: 'hello',
        },
        { status: 200 },
      ),
    );

    const file = new File(['hello'], 'notes.txt', { type: 'text/plain' });
    await expect(
      uploadAttachment(file, { fetch: fetchFunction }),
    ).resolves.toEqual({
      attachment_id: 'attachment-text-1',
      filename: 'notes.txt',
      media_type: 'text/plain',
      size_bytes: 5,
      text_content: 'hello',
    });
  });

  it('defaults text_content to null when the upload payload omits it', async () => {
    const fetchFunction = vi.fn().mockResolvedValue(
      jsonResponse(
        {
          attachment_id: 'attachment-image-1',
          filename: 'photo.jpg',
          media_type: 'image/jpeg',
          size_bytes: 64,
        },
        { status: 200 },
      ),
    );

    const file = new File(['binary'], 'photo.jpg', { type: 'image/jpeg' });
    await expect(
      uploadAttachment(file, { fetch: fetchFunction }),
    ).resolves.toEqual({
      attachment_id: 'attachment-image-1',
      filename: 'photo.jpg',
      media_type: 'image/jpeg',
      size_bytes: 64,
      text_content: null,
    });
  });
});

describe('subscribeRunEvents()', () => {
  it('includes streaming delta events in the default SSE subscription list', () => {
    expect(RUN_EVENT_TYPES).toContain(RUN_EVENT_ASSISTANT_OUTPUT_DELTA);
    expect(RUN_EVENT_TYPES).toContain(RUN_EVENT_REASONING_DELTA);
    expect(RUN_EVENT_TYPES).toContain(RUN_EVENT_TOOL_CALL_DELTA);
    expect(RUN_EVENT_TYPES).toContain(RUN_EVENT_TOOL_CALL_STDOUT);
    expect(RUN_EVENT_TYPES).toContain(RUN_EVENT_TOOL_CALL_STDERR);
    expect(RUN_EVENT_TYPES).toContain('model_fallback_activated');
    expect(RUN_EVENT_TYPES).toContain('error_message_persisted');
    expect(RUN_EVENT_TYPES).toContain('compaction_completed');
    expect(RUN_EVENT_TYPES).toContain('subagent_session_started');
  });

  it('subscribes to named SSE run events and closes on terminal events', () => {
    const onEvent = vi.fn();
    const onError = vi.fn();
    const connection = subscribeRunEvents(
      '/api/runs/run-one/events',
      { onEvent, onError },
      { EventSource: MockEventSource, baseUrl: 'http://localhost:8420/' },
    );

    connection.source.emit('reasoning', {
      data: JSON.stringify({ payload: { text: 'thinking' } }),
    });
    connection.source.emit('run_completed', {
      data: JSON.stringify({ payload: { status: 'done' } }),
    });
    connection.close();

    expect(connection.source.url).toBe(
      'http://localhost:8420/api/runs/run-one/events',
    );
    expect(onEvent).toHaveBeenCalledWith({
      type: 'reasoning',
      data: { payload: { text: 'thinking' } },
      rawEvent: expect.any(Object),
    });
    expect(connection.source.closeCount).toBe(1);
    expect(onError).not.toHaveBeenCalled();
  });

  it('subscribes to delta SSE run events', () => {
    const onEvent = vi.fn();
    const connection = subscribeRunEvents(
      '/api/runs/run-one/events',
      { onEvent },
      { EventSource: MockEventSource },
    );

    connection.source.emit(RUN_EVENT_ASSISTANT_OUTPUT_DELTA, {
      data: JSON.stringify({ payload: { content_delta: 'hel' } }),
    });
    connection.source.emit(RUN_EVENT_REASONING_DELTA, {
      data: JSON.stringify({ payload: { reasoning_delta: 'think' } }),
    });
    connection.source.emit(RUN_EVENT_TOOL_CALL_DELTA, {
      data: JSON.stringify({
        payload: { tool_call_id: 'tool-one', name_delta: 'read' },
      }),
    });

    expect(onEvent).toHaveBeenCalledWith({
      type: RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
      data: { payload: { content_delta: 'hel' } },
      rawEvent: expect.any(Object),
    });
    expect(onEvent).toHaveBeenCalledWith({
      type: RUN_EVENT_REASONING_DELTA,
      data: { payload: { reasoning_delta: 'think' } },
      rawEvent: expect.any(Object),
    });
    expect(onEvent).toHaveBeenCalledWith({
      type: RUN_EVENT_TOOL_CALL_DELTA,
      data: {
        payload: { tool_call_id: 'tool-one', name_delta: 'read' },
      },
      rawEvent: expect.any(Object),
    });
  });

  it('subscribes to sub-agent session started SSE run events', () => {
    const onEvent = vi.fn();
    const connection = subscribeRunEvents(
      '/api/runs/run-subagent/events',
      { onEvent },
      { EventSource: MockEventSource },
    );

    connection.source.emit('subagent_session_started', {
      data: JSON.stringify({
        payload: {
          tool_call: { id: 'call-subagent', index: 0, name: 'subagent' },
          data: {
            agent_id: 'beta',
            session_id: 'child-session',
            status: 'running',
          },
        },
      }),
    });

    expect(onEvent).toHaveBeenCalledWith({
      type: 'subagent_session_started',
      data: {
        payload: {
          tool_call: { id: 'call-subagent', index: 0, name: 'subagent' },
          data: {
            agent_id: 'beta',
            session_id: 'child-session',
            status: 'running',
          },
        },
      },
      rawEvent: expect.any(Object),
    });
  });

  it('adds optional after_sequence query param to SSE subscriptions', () => {
    const connection = subscribeRunEvents(
      '/api/runs/run-one/events?mode=live',
      { onEvent: vi.fn() },
      {
        EventSource: MockEventSource,
        baseUrl: 'http://localhost:8420/',
        afterSequence: 12,
      },
    );

    expect(connection.source.url).toBe(
      'http://localhost:8420/api/runs/run-one/events?mode=live&after_sequence=12',
    );
  });

  it('reports malformed SSE JSON through the error handler', () => {
    const onError = vi.fn();
    const connection = subscribeRunEvents(
      '/events',
      { onError },
      { EventSource: MockEventSource },
    );

    connection.source.emit('reasoning', { data: 'not json' });

    expect(onError).toHaveBeenCalledWith(
      expect.objectContaining({ code: SSE_ERROR_RESPONSE }),
      expect.any(Object),
    );
  });
});

describe('subscribeServerEvents()', () => {
  it('subscribes to /ws messages and parses JSON events', () => {
    const onEvent = vi.fn();
    const connection = subscribeServerEvents(
      { onEvent },
      { WebSocket: MockWebSocket, baseUrl: 'https://localhost:8420/' },
    );

    connection.socket.emit('message', {
      data: JSON.stringify({ type: 'run_started' }),
    });
    connection.close(1000, 'done');
    connection.close(1000, 'done');

    // The /ws connect carries the per-window presence identity by default
    // (a minted connection_id + accessor type).
    expect(connection.socket.url).toContain('wss://localhost:8420/ws');
    expect(connection.socket.url).toContain('connection_id=');
    expect(connection.socket.url).toContain('accessor=browser');
    expect(onEvent).toHaveBeenCalledWith(
      { type: 'run_started' },
      expect.any(Object),
    );
    expect(connection.socket.closeCalls).toEqual([
      { code: 1000, reason: 'done' },
    ]);
  });

  it('sends explicit connection_id and accessor query params when provided', () => {
    const connection = subscribeServerEvents(
      { onEvent: vi.fn() },
      {
        WebSocket: MockWebSocket,
        baseUrl: 'https://localhost:8420/',
        connectionId: 'tab-xyz',
        accessor: 'desktop',
      },
    );

    expect(connection.socket.url).toContain('connection_id=tab-xyz');
    expect(connection.socket.url).toContain('accessor=desktop');

    connection.close();
  });

  it('reports malformed WebSocket messages through the error handler', () => {
    const onError = vi.fn();
    const connection = subscribeServerEvents(
      { onError },
      { WebSocket: MockWebSocket },
    );

    connection.socket.emit('message', { data: '{' });

    expect(onError).toHaveBeenCalledWith(
      expect.objectContaining({ code: WEBSOCKET_ERROR_RESPONSE }),
      expect.any(Object),
    );
  });

  it('includes after_sequence query param when afterSequence is greater than 0', () => {
    const connection = subscribeServerEvents(
      { onEvent: vi.fn() },
      {
        WebSocket: MockWebSocket,
        baseUrl: 'https://localhost:8420/',
        afterSequence: 5,
      },
    );

    expect(connection.socket.url).toContain('after_sequence=5');

    connection.close();
  });

  it('omits after_sequence query param when afterSequence is 0', () => {
    const connection = subscribeServerEvents(
      { onEvent: vi.fn() },
      {
        WebSocket: MockWebSocket,
        baseUrl: 'https://localhost:8420/',
        afterSequence: 0,
      },
    );

    expect(connection.socket.url).not.toContain('after_sequence');

    connection.close();
  });

  it('omits after_sequence query param when afterSequence is omitted', () => {
    const connection = subscribeServerEvents(
      { onEvent: vi.fn() },
      { WebSocket: MockWebSocket, baseUrl: 'https://localhost:8420/' },
    );

    expect(connection.socket.url).not.toContain('after_sequence');

    connection.close();
  });

  it('includes epoch query param when epoch is non-empty', () => {
    const connection = subscribeServerEvents(
      { onEvent: vi.fn() },
      {
        WebSocket: MockWebSocket,
        baseUrl: 'https://localhost:8420/',
        epoch: 'abc123',
      },
    );

    expect(connection.socket.url).toContain('epoch=abc123');

    connection.close();
  });

  it('combines epoch and after_sequence when both are non-empty', () => {
    const connection = subscribeServerEvents(
      { onEvent: vi.fn() },
      {
        WebSocket: MockWebSocket,
        baseUrl: 'https://localhost:8420/',
        afterSequence: 5,
        epoch: 'abc123',
      },
    );

    expect(connection.socket.url).toContain('after_sequence=5');
    expect(connection.socket.url).toContain('epoch=abc123');

    connection.close();
  });

  it('omits epoch query param when epoch is the empty string', () => {
    const connection = subscribeServerEvents(
      { onEvent: vi.fn() },
      {
        WebSocket: MockWebSocket,
        baseUrl: 'https://localhost:8420/',
        epoch: '',
      },
    );

    expect(connection.socket.url).not.toContain('epoch=');

    connection.close();
  });

  it('omits epoch query param when epoch is omitted', () => {
    const connection = subscribeServerEvents(
      { onEvent: vi.fn() },
      { WebSocket: MockWebSocket, baseUrl: 'https://localhost:8420/' },
    );

    expect(connection.socket.url).not.toContain('epoch=');

    connection.close();
  });
});

describe('subscribeLogEvents()', () => {
  it('subscribes to the dedicated logs websocket with file query param', () => {
    const onEvent = vi.fn();
    const connection = subscribeLogEvents(
      '2026-05-11',
      { onEvent },
      { WebSocket: MockWebSocket, baseUrl: 'https://localhost:8420/' },
    );

    connection.socket.emit('message', {
      data: JSON.stringify({ type: 'append', file: '2026-05-11', entries: [] }),
    });

    expect(connection.socket.url).toBe(
      'wss://localhost:8420/ws/logs?file=2026-05-11',
    );
    expect(onEvent).toHaveBeenCalledWith(
      { type: 'append', file: '2026-05-11', entries: [] },
      expect.any(Object),
    );

    connection.close();
  });

  it('passes the explicit log cursor through to the logs websocket', () => {
    const connection = subscribeLogEvents(
      '2026-05-11',
      { onEvent: vi.fn() },
      {
        WebSocket: MockWebSocket,
        baseUrl: 'https://localhost:8420/',
        cursor: 'cursor-123',
      },
    );

    expect(connection.socket.url).toBe(
      'wss://localhost:8420/ws/logs?file=2026-05-11&cursor=cursor-123',
    );

    connection.close();
  });

  it('reports malformed log websocket messages through the error handler', () => {
    const onError = vi.fn();
    const connection = subscribeLogEvents(
      '2026-05-11',
      { onError },
      { WebSocket: MockWebSocket },
    );

    connection.socket.emit('message', { data: '{' });

    expect(onError).toHaveBeenCalledWith(
      expect.objectContaining({ code: WEBSOCKET_ERROR_RESPONSE }),
      expect.any(Object),
    );
  });

  it('rejects invalid log subscriptions before opening websocket', () => {
    expect(() =>
      subscribeLogEvents('', {}, { WebSocket: MockWebSocket }),
    ).toThrow(
      expect.objectContaining({ code: RPC_ERROR_INVALID_CLIENT_REQUEST }),
    );
  });
});

function jsonResponse(body, options = {}) {
  return {
    ok: options.ok ?? true,
    status: options.status ?? 500,
    json: vi.fn().mockResolvedValue(body),
  };
}

class MockEventSource {
  constructor(url) {
    this.url = url;
    this.closeCount = 0;
    this.listeners = new Map();
  }

  addEventListener(eventName, listener) {
    this.listeners.set(eventName, [
      ...(this.listeners.get(eventName) ?? []),
      listener,
    ]);
  }

  removeEventListener(eventName, listener) {
    this.listeners.set(
      eventName,
      (this.listeners.get(eventName) ?? []).filter(
        (storedListener) => storedListener !== listener,
      ),
    );
  }

  emit(eventName, event) {
    for (const listener of this.listeners.get(eventName) ?? []) {
      listener({ type: eventName, ...event });
    }
  }

  close() {
    this.closeCount += 1;
  }
}

class MockWebSocket {
  constructor(url) {
    this.url = url;
    this.closeCalls = [];
    this.listeners = new Map();
  }

  addEventListener(eventName, listener) {
    this.listeners.set(eventName, [
      ...(this.listeners.get(eventName) ?? []),
      listener,
    ]);
  }

  removeEventListener(eventName, listener) {
    this.listeners.set(
      eventName,
      (this.listeners.get(eventName) ?? []).filter(
        (storedListener) => storedListener !== listener,
      ),
    );
  }

  emit(eventName, event) {
    for (const listener of this.listeners.get(eventName) ?? []) {
      listener({ type: eventName, ...event });
    }
  }

  close(code, reason) {
    this.closeCalls.push({ code, reason });
  }
}
