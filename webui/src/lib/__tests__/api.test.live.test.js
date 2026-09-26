import { describe, expect, it, vi } from 'vitest';
import {
  LIVE_SOCKET_ERROR_RESPONSE,
  RPC_ERROR_INVALID_CLIENT_REQUEST,
  getLiveVoiceStatus,
  openLiveCallSocket,
  sendLiveUiResult,
  startLiveCall,
  stopLiveCall,
} from '../api.js';
import { jsonResponse, MockWebSocket } from './api.support.js';

function rpcFetch(result = {}) {
  return vi.fn().mockResolvedValue(jsonResponse({ ok: true, result }));
}

const sentEnvelope = (fetchFunction) =>
  JSON.parse(fetchFunction.mock.calls[0][1].body);

describe('Live voice RPC wrappers', () => {
  it('reads the Live voice status', async () => {
    const fetchFunction = rpcFetch({
      configured: true,
      usable: true,
      target: 'openai/gpt-live-1::api-key',
    });
    await expect(
      getLiveVoiceStatus({ fetch: fetchFunction }),
    ).resolves.toMatchObject({ configured: true });
    expect(sentEnvelope(fetchFunction)).toEqual({
      method: 'live.status',
      params: {},
    });
  });

  it('starts a call with the SDP offer and stops it by id', async () => {
    const started = rpcFetch({
      call_id: 'call-1',
      media: { type: 'webrtc', sdp: 'answer' },
    });
    await startLiveCall({ media: 'webrtc', sdp: 'offer' }, { fetch: started });
    expect(sentEnvelope(started)).toEqual({
      method: 'live.start',
      params: { media: 'webrtc', sdp: 'offer' },
    });

    const relayed = rpcFetch({ call_id: 'call-2', media: { type: 'relay' } });
    await startLiveCall({ media: 'relay', sdp: 'ignored' }, { fetch: relayed });
    expect(sentEnvelope(relayed)).toEqual({
      method: 'live.start',
      params: { media: 'relay' },
    });

    const stopped = rpcFetch({ stopping: true });
    await expect(stopLiveCall('call-1', { fetch: stopped })).resolves.toEqual({
      stopping: true,
    });
    expect(sentEnvelope(stopped)).toEqual({
      method: 'live.stop',
      params: { call_id: 'call-1' },
    });
  });

  it('names the wake phrases of Desktop Voice commands for either media', async () => {
    const wakePhrases = ['Okay Nabu', 'Hey Jarvis'];
    const started = rpcFetch({ call_id: 'call-1', media: { type: 'relay' } });
    await startLiveCall({ media: 'relay', wakePhrases }, { fetch: started });
    expect(sentEnvelope(started).params).toEqual({
      media: 'relay',
      wake_phrases: ['Okay Nabu', 'Hey Jarvis'],
    });

    const offered = rpcFetch({ call_id: 'call-2', media: { type: 'webrtc' } });
    await startLiveCall(
      { media: 'webrtc', sdp: 'offer', wakePhrases },
      { fetch: offered },
    );
    expect(sentEnvelope(offered).params).toEqual({
      media: 'webrtc',
      sdp: 'offer',
      wake_phrases: ['Okay Nabu', 'Hey Jarvis'],
    });

    // Without wake phrases the field is left out entirely.
    const plain = rpcFetch({ call_id: 'call-3', media: { type: 'relay' } });
    await startLiveCall({ media: 'relay', wakePhrases: [] }, { fetch: plain });
    expect(sentEnvelope(plain).params).toEqual({ media: 'relay' });
  });

  it('rejects wake phrases the server would refuse before sending', () => {
    for (const wakePhrases of [
      'Okay Nabu',
      [''],
      ['Okay Nabu', 3],
      Array.from({ length: 9 }, (_, index) => `Phrase ${index}`),
    ]) {
      expect(() => startLiveCall({ media: 'relay', wakePhrases })).toThrow(
        expect.objectContaining({ code: RPC_ERROR_INVALID_CLIENT_REQUEST }),
      );
    }
  });

  it('sends exactly one UI request outcome', async () => {
    const withResult = rpcFetch({ accepted: true });
    await sendLiveUiResult(
      'call-1',
      'request-1',
      { result: { applied: true } },
      { fetch: withResult },
    );
    expect(sentEnvelope(withResult)).toEqual({
      method: 'live.ui_result',
      params: {
        call_id: 'call-1',
        request_id: 'request-1',
        result: { applied: true },
      },
    });

    const withError = rpcFetch({ accepted: true });
    await sendLiveUiResult(
      'call-1',
      'request-2',
      { error: 'terminal_not_found' },
      { fetch: withError },
    );
    expect(sentEnvelope(withError).params).toEqual({
      call_id: 'call-1',
      request_id: 'request-2',
      error: 'terminal_not_found',
    });
  });

  it.each([
    [{}],
    [{ result: { applied: true }, error: 'operation_failed' }],
    [{ result: ['not', 'an', 'object'] }],
    [{ error: '' }],
  ])('rejects an ambiguous UI outcome %j before sending', (outcome) => {
    expect(() => sendLiveUiResult('call-1', 'request-1', outcome)).toThrow(
      expect.objectContaining({
        code: RPC_ERROR_INVALID_CLIENT_REQUEST,
        method: 'live.ui_result',
      }),
    );
  });

  it('rejects empty call ids and unknown media before sending', () => {
    for (const request of [
      { media: 'webrtc', sdp: '' },
      { media: 'webrtc' },
      { media: 'sip' },
      {},
      undefined,
    ]) {
      expect(() => startLiveCall(request)).toThrow(
        expect.objectContaining({ code: RPC_ERROR_INVALID_CLIENT_REQUEST }),
      );
    }
    expect(() => stopLiveCall('')).toThrow(
      expect.objectContaining({ code: RPC_ERROR_INVALID_CLIENT_REQUEST }),
    );
  });
});

describe('openLiveCallSocket()', () => {
  it('opens the encoded owner socket and delivers object frames', () => {
    const onEvent = vi.fn();
    const onError = vi.fn();
    const onClose = vi.fn();
    const connection = openLiveCallSocket(
      'rtc/one',
      { onEvent, onError, onClose },
      { WebSocket: MockWebSocket, baseUrl: 'https://localhost:8420/' },
    );
    expect(connection.socket.url).toBe(
      'wss://localhost:8420/ws/live/rtc%2Fone',
    );

    connection.socket.emit('message', {
      data: JSON.stringify({ type: 'state', phase: 'live' }),
    });
    connection.socket.emit('message', { data: '{' });
    connection.socket.emit('message', { data: '[1]' });
    expect(onEvent).toHaveBeenCalledExactlyOnceWith(
      { type: 'state', phase: 'live' },
      expect.any(Object),
    );
    expect(onError).toHaveBeenCalledTimes(2);
    expect(onError).toHaveBeenCalledWith(
      expect.objectContaining({ code: LIVE_SOCKET_ERROR_RESPONSE }),
      expect.any(Object),
    );

    connection.close();
    connection.close();
    expect(connection.socket.closeCalls).toHaveLength(1);
    connection.socket.emit('close', {});
    expect(onClose).not.toHaveBeenCalled();
  });

  it('receives relay audio as binary frames and sends audio only while open', () => {
    class AudioSocket extends MockWebSocket {
      static OPEN = 1;
      constructor(url) {
        super(url);
        this.readyState = 0;
        this.sent = [];
      }
      send(data) {
        this.sent.push(data);
      }
    }
    const onEvent = vi.fn();
    const onAudio = vi.fn();
    const connection = openLiveCallSocket(
      'call-1',
      { onEvent, onAudio },
      { WebSocket: AudioSocket, baseUrl: 'https://localhost:8420/' },
    );
    expect(connection.socket.binaryType).toBe('arraybuffer');

    const speech = new ArrayBuffer(4);
    connection.socket.emit('message', { data: speech });
    expect(onAudio).toHaveBeenCalledExactlyOnceWith(speech, expect.any(Object));
    expect(onEvent).not.toHaveBeenCalled();

    const frame = new ArrayBuffer(2);
    expect(connection.sendAudio(frame)).toBe(false);
    connection.socket.readyState = AudioSocket.OPEN;
    expect(connection.sendAudio(frame)).toBe(true);
    connection.close();
    expect(connection.sendAudio(frame)).toBe(false);
    expect(connection.socket.sent).toEqual([frame]);
  });

  it.each([
    [1000, 'ended'],
    [1008, 'unknown_call'],
    [1013, 'lagged'],
    [4000, 'replaced'],
    [1006, 'lost'],
  ])('reports server close code %i as %s', (code, outcome) => {
    const onClose = vi.fn();
    const connection = openLiveCallSocket(
      'call-1',
      { onClose },
      { WebSocket: MockWebSocket, baseUrl: 'https://localhost:8420/' },
    );
    connection.socket.emit('close', { code });
    expect(onClose).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({ code }),
      outcome,
    );
  });
});
