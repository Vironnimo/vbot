import { describe, expect, it, vi } from 'vitest';
import * as api from '../api.js';
import { jsonResponse } from './api.support.js';

const {
  ApiClientError,
  RPC_ERROR_HTTP,
  RPC_ERROR_INVALID_CLIENT_REQUEST,
  RPC_ERROR_NETWORK,
  RPC_ERROR_RESPONSE,
} = api;

const RESULT = { result: 'rpc-result-sentinel' };

function rpcFetch(result = RESULT) {
  return vi.fn().mockResolvedValue(jsonResponse({ ok: true, result }));
}

const sentEnvelopes = (fetchFunction) =>
  fetchFunction.mock.calls.map(([, init]) => JSON.parse(init.body));

const invalidRequest = (method) =>
  expect.objectContaining({
    code: RPC_ERROR_INVALID_CLIENT_REQUEST,
    ...(method === undefined ? {} : { method }),
  });

describe('rpc()', () => {
  it('posts the RPC envelope to the server and returns its result', async () => {
    const fetchFunction = rpcFetch({ agents: [] });

    await expect(
      api.rpc(
        'agent.list',
        { visible: true },
        { baseUrl: 'http://localhost:8420/', fetch: fetchFunction },
      ),
    ).resolves.toEqual({ agents: [] });

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
    expect(api.createRpcEnvelope('agent.list')).toEqual({
      method: 'agent.list',
      params: {},
    });
  });

  it.each([
    [
      'a server RPC error',
      jsonResponse(
        {
          ok: false,
          error: { code: 'active_run', message: 'session is busy' },
        },
        { status: 200 },
      ),
      { code: 'active_run', message: 'session is busy', status: 200 },
    ],
    [
      'an HTTP error carrying an RPC error envelope',
      jsonResponse(
        {
          ok: false,
          error: { code: 'domain_error', message: 'agent does not exist' },
        },
        { ok: false, status: 404 },
      ),
      { code: 'domain_error', message: 'agent does not exist', status: 404 },
    ],
    [
      'an HTTP error without an RPC envelope',
      jsonResponse({ detail: 'Not Found' }, { ok: false, status: 500 }),
      { code: RPC_ERROR_HTTP, status: 500 },
    ],
    [
      'a response without an ok flag',
      jsonResponse({ result: {} }),
      { code: RPC_ERROR_RESPONSE },
    ],
  ])('rejects %s as an ApiClientError', async (_label, response, error) => {
    await expect(
      api.rpc(
        'chat.stream',
        {},
        { fetch: vi.fn().mockResolvedValue(response) },
      ),
    ).rejects.toMatchObject({
      name: 'ApiClientError',
      method: 'chat.stream',
      ...error,
    });
  });

  it('rejects a failed request as a network error', async () => {
    await expect(
      api.rpc(
        'agent.list',
        {},
        { fetch: vi.fn().mockRejectedValue(new Error('offline')) },
      ),
    ).rejects.toMatchObject({ code: RPC_ERROR_NETWORK, method: 'agent.list' });
  });

  it('normalizes an unknown error shape into an ApiClientError', () => {
    const error = api.normalizeRpcError(null, {
      method: 'agent.list',
      status: 200,
    });

    expect(error).toBeInstanceOf(ApiClientError);
    expect(error).toMatchObject({
      code: 'rpc_error',
      method: 'agent.list',
      status: 200,
    });
  });
});

// Each wrapper's wire contract: the RPC method (the first word of the case
// label) and the params it sends, and the result it resolves to.
describe('RPC wrappers', () => {
  const cursor = {
    active_sort: 2460000,
    agent_id: 'alpha',
    session_id: 'session-35',
  };
  const statisticsWindow = {
    since: '2026-06-01T00:00:00Z',
    until: '2026-06-07T12:00:00Z',
  };
  const subAgentWork = {
    id: 'sub-work-one',
    agent_id: 'worker@project-one',
    session_id: 'child-session',
  };
  const wakePhrases = ['Okay Nabu', 'Hey Jarvis'];

  it.each([
    ['data_store.status', (o) => api.getDataStoreStatus(o), {}],
    ['recall.status', (o) => api.getRecallIndexStatus(o), {}],
    ['recall.rebuild_index', (o) => api.rebuildRecallIndex(o), {}],
    [
      'data_store.snapshot_create',
      (o) => api.createDataSnapshot('manual', o),
      { reason: 'manual' },
    ],
    [
      'data_store.incident_acknowledge',
      (o) => api.acknowledgeDataStoreIncident('incident-1', o),
      { incident_id: 'incident-1' },
    ],
    [
      'statistics.report',
      (o) => api.getStatisticsReport(statisticsWindow, o),
      statisticsWindow,
    ],
    ['log.list', (o) => api.listLogs(o), {}],
    [
      'log.read',
      (o) => api.readLogFile('2026-05-11', o),
      { file: '2026-05-11' },
    ],
    [
      'log.read',
      (o) => api.readOlderLogEntries('2026-05-11', 4096, o),
      { file: '2026-05-11', before: 4096 },
    ],
    [
      'chat.queue_list',
      (o) => api.listQueue('agent-1', 'session-1', o),
      { agent_id: 'agent-1', session_id: 'session-1' },
    ],
    [
      'chat.queue_remove',
      (o) => api.removeFromQueue('agent-1', 'session-1', 'queue-1', o),
      { agent_id: 'agent-1', session_id: 'session-1', item_id: 'queue-1' },
    ],
    [
      'chat.queue_update',
      (o) =>
        api.updateQueueItem('agent-1', 'session-1', 'queue-1', 'Updated', o),
      {
        agent_id: 'agent-1',
        session_id: 'session-1',
        item_id: 'queue-1',
        content: 'Updated',
      },
    ],
    [
      'subagent.inspect',
      (o) => api.inspectSubAgentWork(subAgentWork, o),
      subAgentWork,
    ],
    [
      'agent.rename',
      (o) => api.renameAgent('coder', 'researcher', o),
      { id: 'coder', new_id: 'researcher' },
    ],
    [
      'agent.reorder',
      (o) => api.reorderAgents(['writer', 'coder'], 3, o),
      { agent_ids: ['writer', 'coder'], expected_revision: 3 },
    ],
    [
      'memory.list',
      (o) => api.listAgentMemories('coder', o),
      { agent_id: 'coder' },
    ],
    [
      'memory.add',
      (o) => api.addAgentMemory('coder', 'agent', 'Keep tests focused.', o),
      { agent_id: 'coder', scope: 'agent', content: 'Keep tests focused.' },
    ],
    [
      'memory.replace',
      (o) => api.replaceAgentMemory('coder', 'user', 2, 'Deterministic.', o),
      {
        agent_id: 'coder',
        scope: 'user',
        entry_id: 2,
        content: 'Deterministic.',
      },
    ],
    [
      'memory.remove',
      (o) => api.removeAgentMemory('coder', 'agent', 2, o),
      { agent_id: 'coder', scope: 'agent', entry_id: 2 },
    ],
    [
      'project.add',
      (o) =>
        api.addProject(
          {
            cwd: 'C:/repos/demo',
            display_name: 'Demo',
            default_agent: 'builder',
            default_model: 'openai/gpt-5.2',
            auto_load: ['AGENTS.md'],
          },
          o,
        ),
      {
        cwd: 'C:/repos/demo',
        display_name: 'Demo',
        default_agent: 'builder',
        default_model: 'openai/gpt-5.2',
        auto_load: ['AGENTS.md'],
      },
    ],
    [
      'project.detect',
      (o) => api.detectProject('C:/repos/demo', o),
      { cwd: 'C:/repos/demo' },
    ],
    ['project.list', (o) => api.listProjects(o), {}],
    ['project.show', (o) => api.showProject('demo', o), { project_id: 'demo' }],
    [
      'project.set (id merged into the changes)',
      (o) =>
        api.setProject('demo', { display_name: 'Renamed', cwd: 'C:/x' }, o),
      { display_name: 'Renamed', cwd: 'C:/x', project_id: 'demo' },
    ],
    [
      'project.rm (identity files not copied by default)',
      (o) => api.removeProject('demo', o),
      { project_id: 'demo', copy_rooted_agent_identity_files: false },
    ],
    [
      'project.rm (Rooted-Agent copy choice)',
      (o) => api.removeProject('demo', true, o),
      { project_id: 'demo', copy_rooted_agent_identity_files: true },
    ],
    [
      'project.set_override (numeric 0 sent verbatim)',
      (o) => api.setOverride('demo', 'builder', 'temperature', 0, o),
      {
        project_id: 'demo',
        agent_id: 'builder',
        field: 'temperature',
        value: 0,
      },
    ],
    [
      'project.clear_override',
      (o) => api.clearOverride('demo', 'builder', 'model', o),
      { project_id: 'demo', agent_id: 'builder', field: 'model' },
    ],
    [
      'session.list',
      (o) =>
        api.listSessions(
          ['alpha', 'builder@vbot'],
          {
            limit: 20,
            cursor,
            includeSubagents: false,
            includeMemoryReflections: false,
            includeSkillReflections: true,
            includeCron: false,
            includeChannels: false,
            requiredSession: { agentId: 'alpha', sessionId: 'session-1' },
          },
          o,
        ),
      {
        agent_ids: ['alpha', 'builder@vbot'],
        limit: 20,
        cursor,
        include_subagents: false,
        include_memory_reflections: false,
        include_skill_reflections: true,
        include_cron: false,
        include_channels: false,
        required_session: { agent_id: 'alpha', session_id: 'session-1' },
      },
    ],
    [
      'session.activity_list',
      (o) => api.listSessionActivity(['alpha', 'builder@vbot'], o),
      { agent_ids: ['alpha', 'builder@vbot'] },
    ],
    [
      'session.activity_list (empty batch)',
      (o) => api.listSessionActivity([], o),
      { agent_ids: [] },
    ],
    [
      'session.get',
      (o) => api.getSession('builder@vbot', 'session-1', o),
      { agent_id: 'builder@vbot', session_id: 'session-1' },
    ],
    [
      'session.rename',
      (o) => api.renameSession('alpha', 'session-1', 'Release planning', o),
      { agent_id: 'alpha', session_id: 'session-1', title: 'Release planning' },
    ],
    [
      'session.rename (empty title clears it)',
      (o) => api.renameSession('alpha', 'session-1', '', o),
      { agent_id: 'alpha', session_id: 'session-1', title: '' },
    ],
    [
      'session.delete',
      (o) => api.deleteSession('alpha', 'session-1', o),
      { agent_id: 'alpha', session_id: 'session-1' },
    ],
    [
      'chat.cancel',
      (o) => api.cancelRun('run-1', { reason: 'user' }, o),
      { run_id: 'run-1', reason: 'user' },
    ],
    [
      'chat.cancel (no reason)',
      (o) => api.cancelRun('run-2', undefined, o),
      { run_id: 'run-2' },
    ],
    [
      'chat.cancel_tool_call',
      (o) =>
        api.cancelToolCall(
          { agentId: 'alpha', runId: 'run-1', toolCallId: 'call-1' },
          o,
        ),
      { agent_id: 'alpha', run_id: 'run-1', tool_call_id: 'call-1' },
    ],
    [
      'chat.cancel_tool_call (no Agent)',
      (o) => api.cancelToolCall({ runId: 'run-1', toolCallId: 'call-1' }, o),
      { run_id: 'run-1', tool_call_id: 'call-1' },
    ],
    [
      'chat.cancel_process',
      (o) =>
        api.cancelProcess(
          { agentId: 'builder@project-one', processId: 'process-1' },
          o,
        ),
      { agent_id: 'builder@project-one', process_id: 'process-1' },
    ],
    [
      'task_model.list_targets',
      (o) => api.listTaskModelTargets('speech_to_text', o),
      { task_type: 'speech_to_text' },
    ],
    [
      'task_model.update',
      (o) =>
        api.updateTaskModelSettings(
          { speech_to_text: { target: 'openai/whisper' } },
          o,
        ),
      { model_tasks: { speech_to_text: { target: 'openai/whisper' } } },
    ],
    [
      'task_model.update (based)',
      (o) =>
        api.updateTaskModelSettings(
          { speech_to_text: { target: 'openai/whisper' } },
          { ...o, base: { speech_to_text: { target: '' } } },
        ),
      {
        model_tasks: { speech_to_text: { target: 'openai/whisper' } },
        base: { model_tasks: { speech_to_text: { target: '' } } },
      },
    ],
    ['terminal.list', (o) => api.listTerminals(o), {}],
    [
      'terminal.start',
      (o) => api.startTerminal({ command: 'codex', args: ['--profile'] }, o),
      { command: 'codex', args: ['--profile'] },
    ],
    [
      'terminal.input',
      (o) => api.sendTerminalInput('term/one', 'hello\r', o),
      { terminal_id: 'term/one', data: 'hello\r' },
    ],
    [
      'terminal.resize',
      (o) => api.resizeTerminal('term/one', 100, 30, o),
      { terminal_id: 'term/one', columns: 100, rows: 30 },
    ],
    [
      'terminal.kill',
      (o) => api.killTerminal('term/one', o),
      { terminal_id: 'term/one' },
    ],
    ['live.status', (o) => api.getLiveVoiceStatus(o), {}],
    [
      'live.start (WebRTC offer)',
      (o) => api.startLiveCall({ media: 'webrtc', sdp: 'offer' }, o),
      { media: 'webrtc', sdp: 'offer' },
    ],
    [
      'live.start (relay drops the SDP)',
      (o) => api.startLiveCall({ media: 'relay', sdp: 'ignored' }, o),
      { media: 'relay' },
    ],
    [
      'live.start (relay with wake phrases)',
      (o) => api.startLiveCall({ media: 'relay', wakePhrases }, o),
      { media: 'relay', wake_phrases: wakePhrases },
    ],
    [
      'live.start (WebRTC with wake phrases)',
      (o) =>
        api.startLiveCall({ media: 'webrtc', sdp: 'offer', wakePhrases }, o),
      { media: 'webrtc', sdp: 'offer', wake_phrases: wakePhrases },
    ],
    [
      'live.start (empty wake phrases left out)',
      (o) => api.startLiveCall({ media: 'relay', wakePhrases: [] }, o),
      { media: 'relay' },
    ],
    ['live.stop', (o) => api.stopLiveCall('call-1', o), { call_id: 'call-1' }],
    [
      'live.ui_result (result)',
      (o) =>
        api.sendLiveUiResult(
          'call-1',
          'request-1',
          { result: { applied: true } },
          o,
        ),
      { call_id: 'call-1', request_id: 'request-1', result: { applied: true } },
    ],
    [
      'live.ui_result (error code)',
      (o) =>
        api.sendLiveUiResult(
          'call-1',
          'request-2',
          { error: 'terminal_not_found' },
          o,
        ),
      {
        call_id: 'call-1',
        request_id: 'request-2',
        error: 'terminal_not_found',
      },
    ],
    [
      'skill.install (source link)',
      (o) =>
        api.installSkill(
          { source: 'https://example.test/demo.skill', scope: 'global' },
          o,
        ),
      { source: 'https://example.test/demo.skill', scope: 'global' },
    ],
    [
      'task_model.local_setup_status',
      (o) => api.getLocalSetupStatus('local/granite-embedding-r2', o),
      { target: 'local/granite-embedding-r2' },
    ],
    [
      'task_model.local_setup_install',
      (o) => api.installLocalSetup('local/granite-embedding-r2', o),
      { target: 'local/granite-embedding-r2' },
    ],
    ['speech.local_memory_status', (o) => api.getLocalSpeechMemory(o), {}],
    [
      'speech.local_unload',
      (o) => api.unloadLocalSpeech('local/qwen3-asr', o),
      { target: 'local/qwen3-asr' },
    ],
    [
      'speech.prepare_transcription',
      (o) => api.prepareSpeechTranscription(o),
      {},
    ],
  ])('sends %s', async (label, call, params) => {
    const fetchFunction = rpcFetch();

    await expect(call({ fetch: fetchFunction })).resolves.toEqual(RESULT);

    expect(sentEnvelopes(fetchFunction)).toEqual([
      { method: label.split(' ')[0], params },
    ]);
  });

  // Wrappers check each required id with the same shared check; the empty Run
  // id stands for all of them. The other cases are distinct request rules.
  it.each([
    ['an empty RPC method', () => api.createRpcEnvelope('', {}), undefined],
    [
      'non-object RPC params',
      () => api.createRpcEnvelope('agent.list', []),
      'agent.list',
    ],
    [
      'an empty Run id',
      () => api.cancelRun('', { reason: 'user' }),
      'chat.cancel',
    ],
    [
      'a non-boolean Skill disabled flag',
      () => api.setSkillDisabled('review', 'yes'),
      'skill.set_disabled',
    ],
    [
      'a non-boolean Skill shared flag',
      () => api.shareSkill('main', 'review', 'yes'),
      'skill.share',
    ],
    [
      'an unknown Memory scope',
      () => api.addAgentMemory('coder', 'other', 'Fact'),
      'memory.add',
    ],
    [
      'a non-positive Memory entry id',
      () => api.removeAgentMemory('coder', 'agent', 0),
      'memory.remove',
    ],
    [
      'a Project without cwd',
      () => api.addProject({ display_name: 'Demo' }),
      'project.add',
    ],
    [
      'non-object Project changes',
      () => api.setProject('demo', null),
      'project.set',
    ],
    [
      'a blank Agent id in an activity batch',
      () => api.listSessionActivity(['alpha', '  ']),
      'session.activity_list',
    ],
    [
      'non-object task Model settings',
      () => api.updateTaskModelSettings([]),
      'task_model.update',
    ],
    [
      'task model options without task type',
      () => api.getTaskModelOptions('', 'target'),
      'task_model.options',
    ],
    [
      'an unknown Live media',
      () => api.startLiveCall({ media: 'sip' }),
      'live.start',
    ],
    ['a Live start without request', () => api.startLiveCall(), 'live.start'],
    [
      'a WebRTC Live start without SDP offer',
      () => api.startLiveCall({ media: 'webrtc', sdp: '' }),
      'live.start',
    ],
    [
      'wake phrases that are not a list',
      () => api.startLiveCall({ media: 'relay', wakePhrases: 'Okay Nabu' }),
      'live.start',
    ],
    [
      'an empty wake phrase',
      () => api.startLiveCall({ media: 'relay', wakePhrases: [''] }),
      'live.start',
    ],
    [
      'more wake phrases than the server accepts',
      () =>
        api.startLiveCall({
          media: 'relay',
          wakePhrases: Array.from(
            { length: 9 },
            (_, index) => `Phrase ${index}`,
          ),
        }),
      'live.start',
    ],
    ...[
      {},
      { result: { applied: true }, error: 'operation_failed' },
      { result: ['not', 'an', 'object'] },
      { error: '' },
    ].map((outcome) => [
      `the ambiguous Live UI outcome ${JSON.stringify(outcome)}`,
      () => api.sendLiveUiResult('call-1', 'request-1', outcome),
      'live.ui_result',
    ]),
  ])('rejects %s before sending', (_label, call, method) => {
    const fetchFunction = vi.fn();
    vi.stubGlobal('fetch', fetchFunction);

    expect(call).toThrow(invalidRequest(method));
    expect(fetchFunction).not.toHaveBeenCalled();
  });
});

describe('HTTP media transfers', () => {
  it('uploads a client Skill archive as multipart form data', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(
        jsonResponse({ operation: 'installed', name: 'demo' }),
      );
    const signal = new AbortController().signal;

    const result = await api.installSkillArchive(
      new File(['archive-sentinel'], 'demo.skill'),
      { scope: 'agent:main', path: 'skills/demo', dry_run: false },
      { fetch, signal },
    );

    const [url, request] = fetch.mock.calls[0];
    expect(url).toBe(
      '/api/skills/install?scope=agent%3Amain&path=skills%2Fdemo&dry_run=false',
    );
    expect(request.body).toBeInstanceOf(FormData);
    expect(await request.body.get('file').text()).toBe('archive-sentinel');
    expect(request.signal).toBe(signal);
    expect(result.operation).toBe('installed');
  });

  it.each([
    [
      'an upload failure with its detail',
      jsonResponse(
        { detail: 'upload-error-sentinel' },
        { ok: false, status: 413 },
      ),
      { code: RPC_ERROR_HTTP, message: 'upload-error-sentinel', status: 413 },
    ],
    [
      'a success without operation',
      jsonResponse({}),
      { code: RPC_ERROR_RESPONSE },
    ],
  ])(
    'rejects %s from a Skill archive upload',
    async (_label, response, error) => {
      await expect(
        api.installSkillArchive(
          new Blob(['archive']),
          { scope: 'global' },
          { fetch: vi.fn().mockResolvedValue(response) },
        ),
      ).rejects.toMatchObject(error);
    },
  );

  it('uploads an attachment and returns its metadata', async () => {
    const metadata = {
      attachment_id: 'attachment-text-1',
      filename: 'notes.txt',
      media_type: 'text/plain',
      size_bytes: 5,
    };
    const fetchFunction = vi
      .fn()
      .mockResolvedValue(jsonResponse(metadata, { status: 200 }));

    await expect(
      api.uploadAttachment(
        new File(['hello'], 'notes.txt', { type: 'text/plain' }),
        { fetch: fetchFunction },
      ),
    ).resolves.toEqual(metadata);
  });

  it('uploads recorded audio and returns the transcription', async () => {
    const fetchFunction = vi
      .fn()
      .mockResolvedValue(
        jsonResponse({ text: 'hello world' }, { status: 200 }),
      );

    await expect(
      api.transcribeSpeech(new Blob(['abc'], { type: 'audio/webm' }), {
        fetch: fetchFunction,
      }),
    ).resolves.toEqual({ text: 'hello world' });

    const [url, request] = fetchFunction.mock.calls[0];
    expect(url).toBe('/api/speech/transcribe');
    expect(request.method).toBe('POST');
    expect(request.body).toBeInstanceOf(FormData);
  });

  it('reports streamed transcription phases across chunk boundaries before the transcript', async () => {
    const events = [
      { type: 'progress', phase: 'downloading', elapsed_seconds: 1 },
      { type: 'progress', phase: 'loading', elapsed_seconds: 2 },
      { type: 'progress', phase: 'transcribing', elapsed_seconds: 3 },
      { type: 'result', result: { text: 'Grüße' } },
    ];
    const bytes = new TextEncoder().encode(
      events.map((item) => JSON.stringify(item)).join('\n') + '\n',
    );
    const response = new Response(
      new ReadableStream({
        start(controller) {
          for (let index = 0; index < bytes.length; index += 3)
            controller.enqueue(bytes.slice(index, index + 3));
          controller.close();
        },
      }),
      { headers: { 'Content-Type': 'application/x-ndjson' } },
    );
    const onProgress = vi.fn();
    const fetchFunction = vi.fn().mockResolvedValue(response);

    await expect(
      api.transcribeSpeech(new Blob(['audio']), {
        fetch: fetchFunction,
        onProgress,
      }),
    ).resolves.toEqual({ text: 'Grüße' });

    expect(onProgress.mock.calls.map(([event]) => event.phase)).toEqual([
      'downloading',
      'loading',
      'transcribing',
    ]);
    expect(fetchFunction.mock.calls[0][1].headers.Accept).toBe(
      'application/x-ndjson',
    );
  });

  it.each([
    [
      'an error event',
      '{"type":"error","detail":"test-owned sentinel","status":409}\n',
      RPC_ERROR_HTTP,
    ],
    [
      'a stream that ends without result',
      '{"type":"progress","phase":"loading"}\n',
      RPC_ERROR_RESPONSE,
    ],
  ])('rejects a transcription stream with %s', async (_label, body, code) => {
    const fetchFunction = vi.fn().mockResolvedValue(
      new Response(body, {
        headers: { 'Content-Type': 'application/x-ndjson' },
      }),
    );

    await expect(
      api.transcribeSpeech(new Blob(['audio']), {
        fetch: fetchFunction,
        onProgress: vi.fn(),
      }),
    ).rejects.toMatchObject({ code });
  });

  it('streams synthesis progress and returns the server-owned audio artifact', async () => {
    const onProgress = vi.fn();
    const fetch = vi
      .fn()
      .mockResolvedValue(
        new Response(
          '{"type":"progress","phase":"loading","elapsed_seconds":12}\n' +
            '{"type":"result","result":{"url":"/api/speech/artifacts/aud_test"}}\n',
        ),
      );

    const result = await api.previewSpeech('test-owned text', {
      fetch,
      onProgress,
      baseUrl: 'http://localhost:9000',
    });

    expect(fetch.mock.calls[0][0]).toBe(
      'http://localhost:9000/api/speech/synthesize',
    );
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
      text: 'test-owned text',
    });
    expect(onProgress).toHaveBeenCalledExactlyOnceWith({
      type: 'progress',
      phase: 'loading',
      elapsed_seconds: 12,
    });
    expect(result.url).toBe('/api/speech/artifacts/aud_test');
  });

  it.each([
    [
      'an audio URL outside the server artifacts',
      '{"type":"result","result":{"url":"https://external.example/audio"}}\n',
      RPC_ERROR_RESPONSE,
    ],
    [
      'a stream that ends without result',
      '{"type":"progress","phase":"loading"}\n',
      RPC_ERROR_RESPONSE,
    ],
    [
      'an error event',
      '{"type":"error","detail":"test-owned error","status":502}\n',
      RPC_ERROR_HTTP,
    ],
  ])('rejects a synthesis stream with %s', async (_label, body, code) => {
    await expect(
      api.previewSpeech('hello', {
        fetch: vi.fn().mockResolvedValue(new Response(body)),
      }),
    ).rejects.toMatchObject({ code });
  });
});

describe('getServedWebuiBuild()', () => {
  it.each([
    ['the served build id', new Response('{"build_id":"b1"}'), 'b1'],
    [
      'null for the page served for an unknown path',
      new Response('<html>'),
      null,
    ],
    ['null for a missing file', new Response('', { status: 404 }), null],
  ])('reads %s past the browser cache', async (_label, response, expected) => {
    const fetchFunction = vi.fn().mockResolvedValue(response);

    await expect(
      api.getServedWebuiBuild({ fetch: fetchFunction }),
    ).resolves.toBe(expected);
    expect(fetchFunction).toHaveBeenCalledWith(
      '/build.json',
      expect.objectContaining({ cache: 'no-store' }),
    );
  });
});
