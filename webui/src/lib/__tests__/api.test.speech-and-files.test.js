import { describe, expect, it, vi } from 'vitest';
import {
  ApiClientError,
  RPC_ERROR_HTTP,
  RPC_ERROR_RESPONSE,
  getLocalSpeechMemory,
  unloadLocalSpeech,
  normalizeRpcError,
  transcribeSpeech,
  previewSpeech,
  uploadAttachment,
} from '../api.js';
import { jsonResponse } from './api.support.js';

describe('transcribeSpeech()', () => {
  it('reports streamed phases before returning the final transcript, across chunk boundaries', async () => {
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
    const result = await transcribeSpeech(new Blob(['audio']), {
      fetch: fetchFunction,
      onProgress,
    });
    expect(result).toEqual({ text: 'Grüße' });
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
      '{"type":"error","detail":"test-owned sentinel","status":409}\n',
      RPC_ERROR_HTTP,
    ],
    ['{"type":"progress","phase":"loading"}\n', RPC_ERROR_RESPONSE],
  ])('rejects failed or interrupted speech streams', async (body, code) => {
    const fetchFunction = vi.fn().mockResolvedValue(
      new Response(body, {
        headers: { 'Content-Type': 'application/x-ndjson' },
      }),
    );
    await expect(
      transcribeSpeech(new Blob(['audio']), {
        fetch: fetchFunction,
        onProgress: vi.fn(),
      }),
    ).rejects.toMatchObject({ code });
  });

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

describe('previewSpeech()', () => {
  it('streams synthesis progress and returns a server-owned audio artifact', async () => {
    const onProgress = vi.fn();
    const fetch = vi
      .fn()
      .mockResolvedValue(
        new Response(
          '{"type":"progress","phase":"loading","elapsed_seconds":12}\n' +
            '{"type":"result","result":{"url":"/api/speech/artifacts/aud_test"}}\n',
        ),
      );
    const result = await previewSpeech('test-owned text', {
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
      '{"type":"result","result":{"url":"https://external.example/audio"}}\n',
      RPC_ERROR_RESPONSE,
    ],
    ['{"type":"progress","phase":"loading"}\n', RPC_ERROR_RESPONSE],
    [
      '{"type":"error","detail":"test-owned error","status":502}\n',
      RPC_ERROR_HTTP,
    ],
  ])(
    'rejects invalid results and incomplete synthesis streams',
    async (body, code) => {
      await expect(
        previewSpeech('hello', {
          fetch: vi.fn().mockResolvedValue(new Response(body)),
        }),
      ).rejects.toMatchObject({ code });
    },
  );
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
  it('returns attachment metadata without text content', async () => {
    const fetchFunction = vi.fn().mockResolvedValue(
      jsonResponse(
        {
          attachment_id: 'attachment-text-1',
          filename: 'notes.txt',
          media_type: 'text/plain',
          size_bytes: 5,
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
    });
  });
});

describe('local speech memory', () => {
  it('reads memory without parameters and unloads only the exact target', async () => {
    const snapshot = { models: [] };
    const fetch = vi
      .fn()
      .mockImplementation(
        async () =>
          new Response(JSON.stringify({ ok: true, result: snapshot })),
      );
    const options = { fetch, baseUrl: 'http://speech.test' };
    expect(await getLocalSpeechMemory(options)).toEqual(snapshot);
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
      method: 'speech.local_memory_status',
      params: {},
    });
    expect(await unloadLocalSpeech('local/qwen3-asr', options)).toEqual(
      snapshot,
    );
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({
      method: 'speech.local_unload',
      params: { target: 'local/qwen3-asr' },
    });
    expect(() => unloadLocalSpeech('', options)).toThrow();
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});
