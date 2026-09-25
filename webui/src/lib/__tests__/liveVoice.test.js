import { afterEach, describe, expect, it, vi } from 'vitest';
import { createLiveVoice, createLiveVoiceState } from '../liveVoice.js';

class Events {
  constructor() {
    this.listeners = new Map();
  }
  addEventListener(name, callback) {
    this.listeners.set(name, [...(this.listeners.get(name) || []), callback]);
  }
  removeEventListener(name, callback) {
    this.listeners.set(
      name,
      (this.listeners.get(name) || []).filter((fn) => fn !== callback),
    );
  }
  emit(name, value = {}) {
    for (const callback of this.listeners.get(name) || []) callback(value);
  }
}

const flush = async () => {
  for (let index = 0; index < 20; index += 1) await Promise.resolve();
};

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

function liveFixture(overrides = {}) {
  const track = Object.assign(new Events(), {
    enabled: true,
    stop: vi.fn(),
    getCapabilities: vi.fn(() => ({ echoCancellation: [true, false] })),
    applyConstraints: vi.fn().mockResolvedValue(undefined),
  });
  const microphone = {
    getTracks: () => [track],
    getAudioTracks: () => [track],
  };
  const channel = Object.assign(new Events(), { close: vi.fn() });
  const peer = Object.assign(new Events(), {
    connectionState: 'new',
    iceGatheringState: 'complete',
    localDescription: { type: 'offer', sdp: 'offer-sdp' },
    addTrack: vi.fn(),
    createDataChannel: vi.fn(() => channel),
    createOffer: vi.fn().mockResolvedValue({ type: 'offer', sdp: 'offer' }),
    setLocalDescription: vi.fn().mockResolvedValue(undefined),
    setRemoteDescription: vi.fn().mockResolvedValue(undefined),
    close: vi.fn(),
  });
  const sockets = [];
  const api = {
    getLiveVoiceStatus: vi.fn().mockResolvedValue({
      configured: true,
      usable: true,
      target: 'openai/gpt-live-1::api-key',
    }),
    startLiveCall: vi.fn().mockResolvedValue({
      call_id: 'call-1',
      media: { type: 'webrtc', sdp: 'answer-sdp' },
    }),
    stopLiveCall: vi.fn().mockResolvedValue({ stopping: true }),
    sendLiveUiResult: vi.fn().mockResolvedValue({ accepted: true }),
    openLiveCallSocket: vi.fn((callId, handlers) => {
      const socket = { callId, handlers, close: vi.fn(), sendAudio: vi.fn() };
      sockets.push(socket);
      return socket;
    }),
  };
  const relay = {
    play: vi.fn(),
    clear: vi.fn(),
    close: vi.fn(),
    onFrame: null,
  };
  const createAudio = vi.fn(async ({ onFrame }) => {
    relay.onFrame = onFrame;
    return relay;
  });
  const mediaDevices = { getUserMedia: vi.fn().mockResolvedValue(microphone) };
  const audio = {
    srcObject: null,
    play: vi.fn().mockResolvedValue(undefined),
    pause: vi.fn(),
  };
  const uiActions = {
    context: vi.fn().mockResolvedValue({ view: 'chat', agents: [] }),
    open: vi.fn().mockResolvedValue(true),
    terminalView: vi.fn().mockResolvedValue({ visible_order: ['t1'] }),
  };
  const onNotice = vi.fn();
  const onActive = vi.fn();
  const state = createLiveVoiceState();
  const controller = createLiveVoice({
    state,
    api,
    mediaDevices,
    createPeer: () => peer,
    audio,
    createAudio,
    uiActions,
    onNotice,
    onActive,
    ...overrides,
  });
  const socket = () => sockets.at(-1);
  const frame = (value) => socket().handlers.onEvent(value);
  const goLive = async () => {
    await controller.start();
    frame({ type: 'state', phase: 'live' });
  };
  const request = (requestId, action, args) =>
    frame({ type: 'ui_request', request_id: requestId, action, args });
  return {
    api,
    audio,
    channel,
    controller,
    createAudio,
    relay,
    frame,
    goLive,
    mediaDevices,
    microphone,
    onActive,
    onNotice,
    peer,
    request,
    socket,
    sockets,
    state,
    track,
    uiActions,
  };
}

afterEach(() => vi.useRealTimers());

describe('Live voice startup', () => {
  it.each([
    [{ configured: false, usable: false, target: null }, 'not_configured'],
    [{ configured: true, usable: false, target: 'x' }, 'not_usable'],
  ])(
    'checks the Live voice binding before asking for the microphone: %j',
    async (status, code) => {
      const f = liveFixture();
      f.api.getLiveVoiceStatus.mockResolvedValue(status);
      await f.controller.start();
      expect(f.mediaDevices.getUserMedia).not.toHaveBeenCalled();
      expect(f.api.startLiveCall).not.toHaveBeenCalled();
      expect(f.onNotice).toHaveBeenCalledExactlyOnceWith({
        code,
        severity: 'error',
      });
      expect(f.state.phase).toBe('off');
      expect(f.state.error).toBe(code);
    },
  );

  it('offers voice-processed microphone audio with the provider data channel and attaches the call socket', async () => {
    const f = liveFixture();
    await f.controller.start();
    expect(f.mediaDevices.getUserMedia).toHaveBeenCalledWith({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    expect(f.peer.addTrack).toHaveBeenCalledWith(f.track, f.microphone);
    expect(f.peer.createDataChannel).toHaveBeenCalledWith('oai-events');
    expect(f.peer.createDataChannel.mock.invocationCallOrder[0]).toBeLessThan(
      f.peer.createOffer.mock.invocationCallOrder[0],
    );
    expect(f.api.startLiveCall).toHaveBeenCalledWith({
      media: 'webrtc',
      sdp: 'offer-sdp',
      wakePhrases: [],
    });
    expect(f.api.openLiveCallSocket).toHaveBeenCalledWith(
      'call-1',
      expect.any(Object),
    );
    expect(f.peer.setRemoteDescription).toHaveBeenCalledWith({
      type: 'answer',
      sdp: 'answer-sdp',
    });
    expect(f.api.openLiveCallSocket.mock.invocationCallOrder[0]).toBeLessThan(
      f.peer.setRemoteDescription.mock.invocationCallOrder[0],
    );
    expect(f.state).toMatchObject({ phase: 'connecting', callId: 'call-1' });
    expect(f.onActive).not.toHaveBeenCalled();

    f.frame({ type: 'state', phase: 'live' });
    expect(f.state.phase).toBe('live');
    expect(f.controller.active()).toBe(true);
    expect(f.onActive).toHaveBeenCalledExactlyOnceWith(true);
    expect(f.onNotice).not.toHaveBeenCalled();
  });

  it('reports a rejected start and releases media without a call to stop', async () => {
    const f = liveFixture();
    f.api.startLiveCall.mockResolvedValue({ error: 'access_denied' });
    await f.controller.start();
    expect(f.onNotice).toHaveBeenCalledExactlyOnceWith({
      code: 'access_denied',
      severity: 'error',
    });
    expect(f.track.stop).toHaveBeenCalledOnce();
    expect(f.peer.close).toHaveBeenCalledOnce();
    expect(f.api.openLiveCallSocket).not.toHaveBeenCalled();
    expect(f.api.stopLiveCall).not.toHaveBeenCalled();
    expect(f.state.phase).toBe('off');
  });

  it.each([
    [{ name: 'NotAllowedError' }, 'microphone_denied'],
    [{ name: 'NotFoundError' }, 'microphone_unavailable'],
  ])('maps a microphone failure %j to %s', async (microphoneError, code) => {
    const f = liveFixture();
    f.mediaDevices.getUserMedia.mockRejectedValue(microphoneError);
    await f.controller.start();
    expect(f.onNotice).toHaveBeenCalledWith({ code, severity: 'error' });
    expect(f.api.startLiveCall).not.toHaveBeenCalled();
  });

  it('needs a microphone API, which insecure pages lack', async () => {
    const f = liveFixture({ mediaDevices: {} });
    await f.controller.start();
    expect(f.onNotice).toHaveBeenCalledWith({
      code: 'microphone_unavailable',
      severity: 'error',
    });
  });

  it('offers once ICE gathering completes', async () => {
    const f = liveFixture();
    f.peer.iceGatheringState = 'gathering';
    const started = f.controller.start();
    await flush();
    expect(f.api.startLiveCall).not.toHaveBeenCalled();
    f.peer.iceGatheringState = 'complete';
    f.peer.emit('icegatheringstatechange');
    await started;
    expect(f.api.startLiveCall).toHaveBeenCalledOnce();
  });

  it('offers the gathered candidates when ICE gathering does not finish in time', async () => {
    vi.useFakeTimers();
    const f = liveFixture();
    f.peer.iceGatheringState = 'gathering';
    void f.controller.start();
    await vi.advanceTimersByTimeAsync(4999);
    expect(f.api.startLiveCall).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(f.api.startLiveCall).toHaveBeenCalledWith({
      media: 'webrtc',
      sdp: 'offer-sdp',
      wakePhrases: [],
    });
    expect(f.onNotice).not.toHaveBeenCalled();
  });

  it('fails a call that never goes live and stops it on the server', async () => {
    vi.useFakeTimers();
    const f = liveFixture();
    await f.controller.start();
    await vi.advanceTimersByTimeAsync(44999);
    expect(f.onNotice).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    await flush();
    expect(f.onNotice).toHaveBeenCalledExactlyOnceWith({
      code: 'connection_timeout',
      severity: 'error',
    });
    expect(f.api.stopLiveCall).toHaveBeenCalledWith('call-1');
    expect(f.socket().close).toHaveBeenCalledOnce();
    expect(f.peer.close).toHaveBeenCalledOnce();
    expect(f.track.stop).toHaveBeenCalledOnce();
    expect(f.state.phase).toBe('off');
  });
});

describe('Live voice generation guards', () => {
  it('discards a microphone grant that arrives after Stop', async () => {
    const f = liveFixture();
    const grant = deferred();
    f.mediaDevices.getUserMedia.mockReturnValue(grant.promise);
    const started = f.controller.start();
    await flush();
    f.controller.stop();
    expect(f.state.phase).toBe('off');
    grant.resolve(f.microphone);
    await started;
    expect(f.track.stop).toHaveBeenCalledOnce();
    expect(f.peer.addTrack).not.toHaveBeenCalled();
    expect(f.api.startLiveCall).not.toHaveBeenCalled();
    expect(f.onNotice).not.toHaveBeenCalled();
  });

  it('stops a call the server created after the user already pressed Stop', async () => {
    const f = liveFixture();
    const created = deferred();
    f.api.startLiveCall.mockReturnValue(created.promise);
    const started = f.controller.start();
    await flush();
    f.controller.stop();
    expect(f.track.stop).toHaveBeenCalledOnce();
    created.resolve({
      call_id: 'late-call',
      media: { type: 'webrtc', sdp: 'answer' },
    });
    await started;
    await flush();
    expect(f.api.stopLiveCall).toHaveBeenCalledExactlyOnceWith('late-call');
    expect(f.api.openLiveCallSocket).not.toHaveBeenCalled();
    expect(f.peer.setRemoteDescription).not.toHaveBeenCalled();
    expect(f.onNotice).not.toHaveBeenCalled();
  });

  it('ignores frames from the socket of a previous call', async () => {
    const f = liveFixture();
    await f.goLive();
    const previous = f.socket();
    f.controller.stop();
    f.frame({ type: 'closed', reason: 'stopped', usage: null });
    f.api.startLiveCall.mockResolvedValue({
      call_id: 'call-2',
      media: { type: 'webrtc', sdp: 'answer' },
    });
    await f.controller.start();
    expect(f.socket().callId).toBe('call-2');

    previous.handlers.onEvent({
      type: 'ui_request',
      request_id: 'old',
      action: 'context',
    });
    previous.handlers.onEvent({ type: 'closed', reason: 'replaced' });
    previous.handlers.onClose();
    await flush();
    expect(f.uiActions.context).not.toHaveBeenCalled();
    expect(f.state).toMatchObject({ phase: 'connecting', callId: 'call-2' });
    expect(f.onNotice).not.toHaveBeenCalled();
  });
});

describe('Live voice relay media', () => {
  const RELAY_RESULT = {
    call_id: 'call-1',
    media: {
      type: 'relay',
      audio: { encoding: 'pcm16', sample_rate: 24000, channels: 1 },
    },
  };

  function relayFixture(overrides = {}) {
    const f = liveFixture(overrides);
    f.api.getLiveVoiceStatus.mockResolvedValue({
      configured: true,
      usable: true,
      target: 'xai/grok-voice-think-fast-2.0::subscription',
      media: 'relay',
    });
    f.api.startLiveCall.mockResolvedValue(RELAY_RESULT);
    return f;
  }

  it('relays audio over the call socket instead of a peer connection', async () => {
    const f = relayFixture();
    await f.controller.start();

    expect(f.mediaDevices.getUserMedia).toHaveBeenCalledWith({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    expect(f.createAudio).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({ microphone: f.microphone }),
    );
    expect(f.createAudio.mock.invocationCallOrder[0]).toBeLessThan(
      f.api.startLiveCall.mock.invocationCallOrder[0],
    );
    expect(f.api.startLiveCall).toHaveBeenCalledExactlyOnceWith({
      media: 'relay',
      wakePhrases: [],
    });
    expect(f.peer.createOffer).not.toHaveBeenCalled();
    expect(f.track.applyConstraints).not.toHaveBeenCalled();

    const early = new ArrayBuffer(4);
    f.relay.onFrame(early);
    expect(f.socket().sendAudio).not.toHaveBeenCalled();
    f.frame({ type: 'state', phase: 'live' });
    const frame = new ArrayBuffer(4);
    f.relay.onFrame(frame);
    expect(f.socket().sendAudio).toHaveBeenCalledExactlyOnceWith(frame);

    const speech = new ArrayBuffer(8);
    f.socket().handlers.onAudio(speech);
    expect(f.relay.play).toHaveBeenCalledExactlyOnceWith(speech);
    f.frame({ type: 'playback_clear' });
    expect(f.relay.clear).toHaveBeenCalledOnce();

    f.controller.stop();
    expect(f.relay.close).toHaveBeenCalledOnce();
    f.relay.onFrame(new ArrayBuffer(4));
    f.socket().handlers.onAudio(new ArrayBuffer(8));
    expect(f.socket().sendAudio).toHaveBeenCalledOnce();
    expect(f.relay.play).toHaveBeenCalledOnce();
    expect(f.onNotice).not.toHaveBeenCalled();
  });

  it('cancels echo of all device output where the microphone supports it', async () => {
    const f = relayFixture();
    f.track.getCapabilities.mockReturnValue({
      echoCancellation: [true, false, 'all'],
    });
    f.track.applyConstraints.mockRejectedValue(new Error('overconstrained'));

    await f.controller.start();

    expect(f.track.applyConstraints).toHaveBeenCalledExactlyOnceWith({
      echoCancellation: 'all',
      noiseSuppression: true,
      autoGainControl: true,
    });
    expect(f.api.startLiveCall).toHaveBeenCalledOnce();
    expect(f.onNotice).not.toHaveBeenCalled();
  });

  it.each(['audio_unsupported', 'playback_blocked'])(
    'fails with %s before creating a call when relay audio cannot start',
    async (code) => {
      const f = relayFixture();
      f.createAudio.mockRejectedValue(Object.assign(new Error(code), { code }));

      await f.controller.start();

      expect(f.onNotice).toHaveBeenCalledExactlyOnceWith({
        code,
        severity: 'error',
      });
      expect(f.api.startLiveCall).not.toHaveBeenCalled();
      expect(f.track.stop).toHaveBeenCalledOnce();
      expect(f.state.phase).toBe('off');
    },
  );

  it('releases relay audio created after the user pressed Stop', async () => {
    const f = relayFixture();
    const created = deferred();
    f.createAudio.mockReturnValue(created.promise);
    const started = f.controller.start();
    await flush();
    f.controller.stop();
    created.resolve(f.relay);
    await started;

    expect(f.relay.close).toHaveBeenCalledOnce();
    expect(f.api.startLiveCall).not.toHaveBeenCalled();
  });

  it('fails a relay call whose media does not match the relay format', async () => {
    const f = relayFixture();
    f.api.startLiveCall.mockResolvedValue({
      call_id: 'call-1',
      media: { type: 'relay', audio: { encoding: 'opus' } },
    });

    await f.controller.start();

    expect(f.onNotice).toHaveBeenCalledExactlyOnceWith({
      code: 'connection_failed',
      severity: 'error',
    });
    expect(f.api.stopLiveCall).toHaveBeenCalledWith('call-1');
    expect(f.relay.close).toHaveBeenCalledOnce();
  });

  it('retries once with fresh status when the server needs the other media', async () => {
    const f = liveFixture();
    f.api.getLiveVoiceStatus
      .mockResolvedValueOnce({
        configured: true,
        usable: true,
        media: 'webrtc',
      })
      .mockResolvedValueOnce({
        configured: true,
        usable: true,
        media: 'relay',
      });
    f.api.startLiveCall
      .mockResolvedValueOnce({ error: 'media_mismatch' })
      .mockResolvedValueOnce(RELAY_RESULT);

    await f.controller.start();

    expect(f.api.startLiveCall.mock.calls).toEqual([
      [{ media: 'webrtc', sdp: 'offer-sdp', wakePhrases: [] }],
      [{ media: 'relay', wakePhrases: [] }],
    ]);
    expect(f.peer.close).toHaveBeenCalledOnce();
    expect(f.track.stop).not.toHaveBeenCalled();
    expect(f.createAudio).toHaveBeenCalledOnce();
    expect(f.socket().callId).toBe('call-1');
    expect(f.onNotice).not.toHaveBeenCalled();
  });

  it('reports a media mismatch that persists after the retry', async () => {
    const f = relayFixture();
    f.api.startLiveCall.mockResolvedValue({ error: 'media_mismatch' });

    await f.controller.start();

    expect(f.api.startLiveCall).toHaveBeenCalledTimes(2);
    expect(f.api.getLiveVoiceStatus).toHaveBeenCalledTimes(2);
    expect(f.relay.close).toHaveBeenCalledTimes(2);
    expect(f.onNotice).toHaveBeenCalledExactlyOnceWith({
      code: 'media_mismatch',
      severity: 'error',
    });
    expect(f.state.phase).toBe('off');
  });
});

describe('stopping Live voice', () => {
  it('releases the microphone at once and closes the call after its closed frame', async () => {
    const f = liveFixture();
    await f.goLive();
    f.controller.stop();
    expect(f.state.phase).toBe('closing');
    expect(f.track.enabled).toBe(false);
    expect(f.track.stop).toHaveBeenCalledOnce();
    await flush();
    expect(f.api.stopLiveCall).toHaveBeenCalledExactlyOnceWith('call-1');
    expect(f.socket().close).not.toHaveBeenCalled();
    expect(f.peer.close).not.toHaveBeenCalled();

    f.frame({
      type: 'closed',
      reason: 'stopped',
      usage: { audio_duration_ms: 1200 },
    });
    expect(f.socket().close).toHaveBeenCalledOnce();
    expect(f.peer.close).toHaveBeenCalledOnce();
    expect(f.channel.close).toHaveBeenCalledOnce();
    expect(f.state).toMatchObject({
      phase: 'off',
      callId: null,
      usage: { audio_duration_ms: 1200 },
    });
    expect(f.onActive).toHaveBeenLastCalledWith(false);
    expect(f.onNotice).not.toHaveBeenCalled();
  });

  it('closes after a short timeout when no closed frame arrives', async () => {
    vi.useFakeTimers();
    const f = liveFixture();
    await f.goLive();
    f.controller.stop();
    await vi.advanceTimersByTimeAsync(4999);
    expect(f.socket().close).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(f.socket().close).toHaveBeenCalledOnce();
    expect(f.state.phase).toBe('off');
    expect(f.onNotice).not.toHaveBeenCalled();
  });

  it.each([
    ['the server no longer holds the call', { stopping: false }],
    ['the stop request fails', new Error('offline')],
  ])('finishes at once when %s', async (_label, outcome) => {
    const f = liveFixture();
    if (outcome instanceof Error) f.api.stopLiveCall.mockRejectedValue(outcome);
    else f.api.stopLiveCall.mockResolvedValue(outcome);
    await f.goLive();
    f.controller.stop();
    await flush();
    expect(f.state.phase).toBe('off');
    expect(f.socket().close).toHaveBeenCalledOnce();
    expect(f.onNotice).not.toHaveBeenCalled();
  });

  it('stops the server call when the controller is destroyed', async () => {
    const f = liveFixture();
    await f.goLive();
    f.controller.destroy();
    await flush();
    expect(f.api.stopLiveCall).toHaveBeenCalledWith('call-1');
    expect(f.socket().close).toHaveBeenCalledOnce();
    expect(f.state.phase).toBe('off');
  });
});

describe('Live call frames', () => {
  it('announces a takeover by another window and releases media', async () => {
    const f = liveFixture();
    await f.goLive();
    f.frame({ type: 'state', phase: 'closing' });
    expect(f.track.stop).toHaveBeenCalledOnce();
    f.frame({ type: 'closed', reason: 'replaced', usage: null });
    expect(f.onNotice).toHaveBeenCalledExactlyOnceWith({
      code: 'replaced',
      severity: 'info',
    });
    expect(f.api.stopLiveCall).not.toHaveBeenCalled();
    expect(f.state).toMatchObject({ phase: 'off', closeReason: 'replaced' });
  });

  it.each([
    ['after it was live', true, { code: 'ended', severity: 'info' }],
    [
      'before it was live',
      false,
      { code: 'connection_failed', severity: 'error' },
    ],
  ])('reports a call the server ended %s', async (_label, live, notice) => {
    const f = liveFixture();
    await f.controller.start();
    if (live) f.frame({ type: 'state', phase: 'live' });
    f.frame({ type: 'closed', reason: null, usage: null });
    expect(f.onNotice).toHaveBeenCalledExactlyOnceWith(notice);
    expect(f.state.phase).toBe('off');
  });

  it('reports a failed call once', async () => {
    const f = liveFixture();
    await f.goLive();
    f.frame({ type: 'state', phase: 'failed' });
    f.frame({ type: 'closed', reason: 'error', usage: null });
    expect(f.onNotice).toHaveBeenCalledExactlyOnceWith({
      code: 'call_failed',
      severity: 'error',
    });
  });

  it('reports a fatal error once, ends the call and waits for its closed frame', async () => {
    const f = liveFixture();
    await f.goLive();
    f.frame({ type: 'error', code: 'control_failed', fatal: true });
    expect(f.onNotice).toHaveBeenCalledExactlyOnceWith({
      code: 'control_failed',
      severity: 'error',
    });
    expect(f.state.phase).toBe('closing');
    expect(f.track.stop).toHaveBeenCalledOnce();
    await flush();
    expect(f.api.stopLiveCall).toHaveBeenCalledWith('call-1');
    f.frame({ type: 'error', code: 'provider_error', fatal: true });
    f.frame({ type: 'closed', reason: 'error', usage: null });
    expect(f.onNotice).toHaveBeenCalledOnce();
    expect(f.state).toMatchObject({ phase: 'off', error: 'control_failed' });
  });

  it('keeps the call after a non-fatal error', async () => {
    const f = liveFixture();
    await f.goLive();
    f.frame({ type: 'error', code: 'announcement_failed', fatal: false });
    expect(f.onNotice).toHaveBeenCalledExactlyOnceWith({
      code: 'announcement_failed',
      severity: 'warn',
    });
    expect(f.state.phase).toBe('live');
  });

  it('replaces open caption turns with each snapshot and closes them when final', async () => {
    const f = liveFixture();
    await f.goLive();
    f.frame({ type: 'caption', role: 'user', text: 'Open the', final: false });
    f.frame({
      type: 'caption',
      role: 'user',
      text: 'Open the chat',
      final: false,
    });
    f.frame({ type: 'caption', role: 'assistant', text: 'Sure', final: false });
    expect(f.state.captions).toEqual([
      { role: 'user', text: 'Open the chat', final: false },
      { role: 'assistant', text: 'Sure', final: false },
    ]);
    f.frame({
      type: 'caption',
      role: 'user',
      text: 'Open the chat.',
      final: true,
    });
    f.frame({ type: 'caption', role: 'assistant', text: '', final: true });
    expect(f.state.captions).toEqual([
      { role: 'user', text: 'Open the chat.', final: true },
      { role: 'assistant', text: 'Sure', final: true },
    ]);
  });

  it('bounds caption turns and their text', async () => {
    const f = liveFixture();
    await f.goLive();
    for (let index = 0; index < 25; index += 1)
      f.frame({
        type: 'caption',
        role: 'assistant',
        text: `turn ${index}`,
        final: true,
      });
    f.frame({
      type: 'caption',
      role: 'user',
      text: `${'x'.repeat(3000)}end`,
      final: false,
    });
    expect(f.state.captions).toHaveLength(20);
    expect(f.state.captions[0].text).toBe('turn 6');
    expect(f.state.captions.at(-1).text).toHaveLength(2000);
    expect(f.state.captions.at(-1).text.endsWith('end')).toBe(true);
  });

  it('tracks busy activity and ignores unknown frames', async () => {
    const f = liveFixture();
    await f.goLive();
    f.frame({ type: 'activity', busy: true, label: 'working' });
    expect(f.state).toMatchObject({ busy: true, activityLabel: 'working' });
    f.frame({ type: 'future_frame', value: 1 });
    f.frame({ type: 'activity', busy: false, label: null });
    expect(f.state).toMatchObject({
      phase: 'live',
      busy: false,
      activityLabel: null,
    });
    expect(f.onNotice).not.toHaveBeenCalled();
  });
});

describe('Live voice UI requests', () => {
  it('returns the App context and answers each request once', async () => {
    const f = liveFixture();
    await f.goLive();
    f.request('r1', 'context');
    f.request('r1', 'context');
    await flush();
    expect(f.uiActions.context).toHaveBeenCalledOnce();
    expect(f.api.sendLiveUiResult).toHaveBeenCalledExactlyOnceWith(
      'call-1',
      'r1',
      { result: { view: 'chat', agents: [] } },
    );
  });

  it('opens an exact Chat Session and reports whether navigation applied', async () => {
    const f = liveFixture();
    await f.goLive();
    f.request('r1', 'open', {
      view: 'chat',
      agent_id: 'joel@vbot',
      session_id: 's1',
    });
    await flush();
    expect(f.uiActions.open).toHaveBeenCalledWith(
      { view: 'chat', agent_id: 'joel@vbot', session_id: 's1' },
      { isCurrent: expect.any(Function) },
    );
    expect(f.api.sendLiveUiResult).toHaveBeenLastCalledWith('call-1', 'r1', {
      result: { applied: true },
    });
    f.uiActions.open.mockResolvedValue(false);
    f.request('r2', 'open', { view: 'terminals', agent_id: null });
    await flush();
    expect(f.uiActions.open).toHaveBeenLastCalledWith(
      { view: 'terminals' },
      expect.any(Object),
    );
    expect(f.api.sendLiveUiResult).toHaveBeenLastCalledWith('call-1', 'r2', {
      result: { applied: false },
    });
  });

  it.each([
    ['open', { view: 'settings' }, 'invalid_view'],
    [
      'open',
      { view: 'terminals', agent_id: 'joel', session_id: 's1' },
      'invalid_arguments',
    ],
    ['open', { view: 'chat', agent_id: 'joel' }, 'invalid_arguments'],
    ['terminal_view', { op: 'close', terminal_id: 't1' }, 'invalid_arguments'],
    ['terminal_view', { op: 'show' }, 'invalid_arguments'],
    ['terminal_view', { op: 'show_group' }, 'invalid_arguments'],
    ['context', ['unexpected'], 'invalid_arguments'],
    ['send_message', {}, 'unsupported_action'],
  ])(
    'answers an invalid %s request %j with %s without running it',
    async (action, args, code) => {
      const f = liveFixture();
      await f.goLive();
      f.request('bad', action, args);
      await flush();
      expect(f.uiActions.context).not.toHaveBeenCalled();
      expect(f.uiActions.open).not.toHaveBeenCalled();
      expect(f.uiActions.terminalView).not.toHaveBeenCalled();
      expect(f.api.sendLiveUiResult).toHaveBeenCalledExactlyOnceWith(
        'call-1',
        'bad',
        { error: code },
      );
      expect(f.onNotice).not.toHaveBeenCalled();
    },
  );

  it('runs Terminal layout requests with only their target fields', async () => {
    const f = liveFixture();
    await f.goLive();
    f.request('r1', 'terminal_view', {
      op: 'maximize',
      terminal_id: 't1',
      group_id: 'ignored',
    });
    f.request('r2', 'terminal_view', { op: 'show_group', group_id: 'g1' });
    f.request('r3', 'terminal_view', { op: 'refresh' });
    await flush();
    expect(
      f.uiActions.terminalView.mock.calls.map(([target]) => target),
    ).toEqual([
      { op: 'maximize', terminal_id: 't1' },
      { op: 'show_group', group_id: 'g1' },
      { op: 'refresh' },
    ]);
    expect(f.api.sendLiveUiResult).toHaveBeenCalledWith('call-1', 'r1', {
      result: { visible_order: ['t1'] },
    });
  });

  it.each([
    [new Error('terminal_not_found'), 'terminal_not_found'],
    [
      Object.assign(new Error('Navigation was cancelled'), {
        code: 'navigation_not_applied',
      }),
      'navigation_not_applied',
    ],
    [new Error('Cannot read properties of undefined'), 'operation_failed'],
  ])(
    'answers a failed UI action with its error code: %s',
    async (error, code) => {
      const f = liveFixture();
      await f.goLive();
      f.uiActions.terminalView.mockRejectedValue(error);
      f.request('r1', 'terminal_view', { op: 'show', terminal_id: 't9' });
      await flush();
      expect(f.api.sendLiveUiResult).toHaveBeenCalledExactlyOnceWith(
        'call-1',
        'r1',
        { error: code },
      );
      expect(f.onNotice).toHaveBeenCalledExactlyOnceWith({
        code: 'ui_action_failed',
        severity: 'warn',
      });
    },
  );

  it('drops the result of a request whose call has stopped', async () => {
    const f = liveFixture();
    await f.goLive();
    const pending = deferred();
    f.uiActions.context.mockReturnValue(pending.promise);
    f.request('r1', 'context');
    await flush();
    const [guard] = f.uiActions.context.mock.calls[0];
    expect(guard.isCurrent()).toBe(true);
    f.controller.stop();
    expect(guard.isCurrent()).toBe(false);
    f.frame({ type: 'closed', reason: 'stopped', usage: null });
    pending.resolve({ view: 'chat' });
    await flush();
    expect(f.api.sendLiveUiResult).not.toHaveBeenCalled();
  });

  it('declines requests that arrive while the call is closing', async () => {
    const f = liveFixture();
    await f.goLive();
    f.controller.stop();
    f.request('r1', 'context');
    await flush();
    expect(f.uiActions.context).not.toHaveBeenCalled();
    expect(f.api.sendLiveUiResult).toHaveBeenCalledExactlyOnceWith(
      'call-1',
      'r1',
      { error: 'call_closing' },
    );
  });
});

describe('Live voice media and connection failures', () => {
  it('ends the call when the microphone disappears', async () => {
    const f = liveFixture();
    await f.goLive();
    f.track.emit('ended');
    await flush();
    expect(f.onNotice).toHaveBeenCalledExactlyOnceWith({
      code: 'microphone_unavailable',
      severity: 'error',
    });
    expect(f.api.stopLiveCall).toHaveBeenCalledWith('call-1');
    expect(f.state.phase).toBe('off');
  });

  it('tolerates a brief peer disconnect but ends a lasting one', async () => {
    vi.useFakeTimers();
    const f = liveFixture();
    await f.goLive();
    const setConnection = (connectionState) => {
      f.peer.connectionState = connectionState;
      f.peer.emit('connectionstatechange');
    };
    setConnection('disconnected');
    await vi.advanceTimersByTimeAsync(4000);
    setConnection('connected');
    await vi.advanceTimersByTimeAsync(5000);
    expect(f.onNotice).not.toHaveBeenCalled();
    setConnection('disconnected');
    await vi.advanceTimersByTimeAsync(5000);
    expect(f.onNotice).toHaveBeenCalledExactlyOnceWith({
      code: 'connection_lost',
      severity: 'error',
    });
  });

  it('ends the call at once when the peer connection fails', async () => {
    const f = liveFixture();
    await f.goLive();
    f.peer.connectionState = 'failed';
    f.peer.emit('connectionstatechange');
    expect(f.onNotice).toHaveBeenCalledExactlyOnceWith({
      code: 'connection_lost',
      severity: 'error',
    });
    expect(f.state.phase).toBe('off');
  });

  it('plays provider audio and ends the call when playback is blocked', async () => {
    const f = liveFixture();
    await f.goLive();
    const stream = { id: 'remote' };
    f.peer.emit('track', { track: {}, streams: [stream] });
    expect(f.audio.srcObject).toBe(stream);
    expect(f.audio.play).toHaveBeenCalledOnce();
    await flush();
    expect(f.onNotice).not.toHaveBeenCalled();

    f.audio.play.mockRejectedValue(
      Object.assign(new Error('blocked'), { name: 'NotAllowedError' }),
    );
    f.peer.emit('track', { track: {}, streams: [stream] });
    await flush();
    expect(f.onNotice).toHaveBeenCalledExactlyOnceWith({
      code: 'playback_blocked',
      severity: 'error',
    });
    expect(f.audio.srcObject).toBeNull();
    expect(f.audio.pause).toHaveBeenCalled();
  });

  it.each(['lost', 'lagged'])(
    'reattaches a %s call socket within the grace window',
    async (outcome) => {
      vi.useFakeTimers();
      const f = liveFixture();
      await f.goLive();
      f.socket().handlers.onClose({}, outcome);
      await vi.advanceTimersByTimeAsync(500);
      expect(f.api.openLiveCallSocket).toHaveBeenCalledTimes(2);
      f.frame({ type: 'activity', busy: true, label: null });
      expect(f.state.busy).toBe(true);
      expect(f.onNotice).not.toHaveBeenCalled();
    },
  );

  it('treats a socket closed as ended like a call that ended', async () => {
    vi.useFakeTimers();
    const f = liveFixture();
    await f.goLive();
    f.socket().handlers.onClose({}, 'ended');
    await vi.advanceTimersByTimeAsync(1000);
    expect(f.api.openLiveCallSocket).toHaveBeenCalledOnce();
    expect(f.api.stopLiveCall).not.toHaveBeenCalled();
    expect(f.onNotice).toHaveBeenCalledExactlyOnceWith({
      code: 'ended',
      severity: 'info',
    });
    expect(f.state.phase).toBe('off');
  });

  it.each(['unknown_call', 'replaced'])(
    'ends the call without reattaching when the socket closes as %s',
    async (outcome) => {
      vi.useFakeTimers();
      const f = liveFixture();
      await f.goLive();
      f.socket().handlers.onClose({}, outcome);
      await vi.advanceTimersByTimeAsync(1000);
      expect(f.api.openLiveCallSocket).toHaveBeenCalledOnce();
      expect(f.onNotice).toHaveBeenCalledExactlyOnceWith({
        code: 'connection_lost',
        severity: 'error',
      });
      expect(f.api.stopLiveCall).toHaveBeenCalledWith('call-1');
      expect(f.state.phase).toBe('off');
    },
  );

  it('ends the call when the socket cannot be reattached in time', async () => {
    vi.useFakeTimers();
    const f = liveFixture();
    await f.goLive();
    for (
      let attempt = 0;
      attempt < 20 && !f.onNotice.mock.calls.length;
      attempt += 1
    ) {
      f.socket().handlers.onClose();
      await vi.advanceTimersByTimeAsync(500);
    }
    await flush();
    expect(f.api.openLiveCallSocket).toHaveBeenCalledTimes(17);
    expect(f.onNotice).toHaveBeenCalledExactlyOnceWith({
      code: 'connection_lost',
      severity: 'error',
    });
    expect(f.api.stopLiveCall).toHaveBeenCalledWith('call-1');
    expect(f.state.phase).toBe('off');
  });

  it('mutes the microphone track, including before it is granted', async () => {
    const f = liveFixture();
    const grant = deferred();
    f.mediaDevices.getUserMedia.mockReturnValue(grant.promise);
    const started = f.controller.start();
    await flush();
    f.controller.mute();
    expect(f.state.muted).toBe(true);
    grant.resolve(f.microphone);
    await started;
    expect(f.track.enabled).toBe(false);
    f.controller.mute();
    expect(f.state.muted).toBe(false);
    expect(f.track.enabled).toBe(true);
  });
});

describe('Live voice microphone access check', () => {
  function accessFixture(check) {
    const events = [];
    const checkMicrophoneAccess = vi.fn(async () => {
      events.push('check');
      return check();
    });
    const f = liveFixture({ checkMicrophoneAccess });
    f.mediaDevices.getUserMedia.mockImplementation(async () => {
      events.push('microphone');
      return f.microphone;
    });
    return { ...f, checkMicrophoneAccess, events };
  }

  it('checks access after the Live voice binding and before the microphone', async () => {
    const f = accessFixture(() => null);
    await f.goLive();
    expect(f.events).toEqual(['check', 'microphone']);
    expect(f.api.getLiveVoiceStatus.mock.invocationCallOrder[0]).toBeLessThan(
      f.checkMicrophoneAccess.mock.invocationCallOrder[0],
    );

    const unbound = accessFixture(() => null);
    unbound.api.getLiveVoiceStatus.mockResolvedValue({
      configured: false,
      usable: false,
      target: null,
    });
    await unbound.controller.start();
    expect(unbound.checkMicrophoneAccess).not.toHaveBeenCalled();
  });

  it('ends the start with the code the check reports, without the microphone', async () => {
    const f = accessFixture(() => 'desktop_restart_required');
    await f.controller.start();
    expect(f.mediaDevices.getUserMedia).not.toHaveBeenCalled();
    expect(f.onNotice).toHaveBeenCalledExactlyOnceWith({
      code: 'desktop_restart_required',
      severity: 'error',
    });
    expect(f.state.phase).toBe('off');
  });

  it('never blocks the call on a failing check', async () => {
    const f = accessFixture(() => {
      throw new Error('bridge busy');
    });
    await f.goLive();
    expect(f.state.phase).toBe('live');
    expect(f.onNotice).not.toHaveBeenCalled();
  });

  it('opens no microphone when Stop comes during the check', async () => {
    const answer = deferred();
    const f = accessFixture(() => answer.promise);
    const started = f.controller.start();
    await flush();
    f.controller.stop();
    answer.resolve(null);
    await started;
    expect(f.mediaDevices.getUserMedia).not.toHaveBeenCalled();
    expect(f.onNotice).not.toHaveBeenCalled();
  });
});

describe('Live voice wake phrases', () => {
  it('names the current wake phrases with each start request', async () => {
    let phrases = ['Okay Nabu'];
    const f = liveFixture({ wakePhrases: () => phrases });
    await f.goLive();
    expect(f.api.startLiveCall).toHaveBeenCalledExactlyOnceWith({
      media: 'webrtc',
      sdp: 'offer-sdp',
      wakePhrases: ['Okay Nabu'],
    });
    f.controller.stop();
    f.frame({ type: 'closed', reason: 'stopped', usage: null });

    phrases = ['Hey Jarvis'];
    f.api.getLiveVoiceStatus.mockResolvedValue({
      configured: true,
      usable: true,
      media: 'relay',
    });
    f.api.startLiveCall.mockResolvedValue({
      call_id: 'call-2',
      media: {
        type: 'relay',
        audio: { encoding: 'pcm16', sample_rate: 24000, channels: 1 },
      },
    });
    await f.controller.start();
    expect(f.api.startLiveCall).toHaveBeenLastCalledWith({
      media: 'relay',
      wakePhrases: ['Hey Jarvis'],
    });
  });

  it('starts without wake phrases when they cannot be read', async () => {
    const f = liveFixture({
      wakePhrases: () => {
        throw new Error('no status');
      },
    });
    await f.goLive();
    expect(f.api.startLiveCall).toHaveBeenCalledExactlyOnceWith({
      media: 'webrtc',
      sdp: 'offer-sdp',
      wakePhrases: [],
    });
    expect(f.state.phase).toBe('live');
  });
});

describe('holding a Live voice call', () => {
  const RELAY_STATUS = { configured: true, usable: true, media: 'relay' };
  const RELAY_RESULT = {
    call_id: 'call-1',
    media: {
      type: 'relay',
      audio: { encoding: 'pcm16', sample_rate: 24000, channels: 1 },
    },
  };

  async function relayCall() {
    const f = liveFixture();
    f.api.getLiveVoiceStatus.mockResolvedValue(RELAY_STATUS);
    f.api.startLiveCall.mockResolvedValue(RELAY_RESULT);
    await f.goLive();
    return f;
  }

  async function webrtcCall() {
    const f = liveFixture();
    await f.goLive();
    f.peer.emit('track', { track: {}, streams: [{ id: 'remote' }] });
    await flush();
    return f;
  }

  it('takes no hold without a running call or a reason', async () => {
    const f = liveFixture();
    expect(f.controller.hold('wakeword')).toBe(false);
    expect(f.controller.held()).toBe(false);

    await f.goLive();
    expect(f.controller.hold('')).toBe(false);
    expect(f.controller.hold(null)).toBe(false);
    expect(f.state.held).toBe(false);

    f.controller.stop();
    expect(f.controller.hold('wakeword')).toBe(false);
  });

  it('silences the microphone independently of the mute', async () => {
    const f = await webrtcCall();

    expect(f.controller.hold('wakeword')).toBe(true);
    expect(f.state.held).toBe(true);
    expect(f.controller.held('wakeword')).toBe(true);
    expect(f.track.enabled).toBe(false);

    // Unmuting during a hold keeps the microphone off.
    f.controller.mute(true);
    f.controller.mute(false);
    expect(f.state.muted).toBe(false);
    expect(f.track.enabled).toBe(false);

    // Muting during a hold keeps the mute after the release.
    f.controller.mute(true);
    f.controller.release('wakeword');
    expect(f.state.held).toBe(false);
    expect(f.track.enabled).toBe(false);
    f.controller.mute(false);
    expect(f.track.enabled).toBe(true);
  });

  it('counts holds per reason and releases only after the last one', async () => {
    const f = await webrtcCall();

    f.controller.hold('wakeword');
    f.controller.hold('wakeword');
    f.controller.hold('calibration');
    f.controller.release('wakeword');
    f.controller.release('calibration');
    expect(f.state.held).toBe(true);
    expect(f.controller.held('calibration')).toBe(false);
    expect(f.track.enabled).toBe(false);

    f.controller.release('wakeword');
    expect(f.state.held).toBe(false);
    expect(f.controller.held()).toBe(false);
    expect(f.track.enabled).toBe(true);

    // A release without a hold changes nothing.
    f.controller.release('wakeword');
    expect(f.track.enabled).toBe(true);
  });

  it('mutes the WebRTC audio element while held', async () => {
    const f = await webrtcCall();
    expect(f.audio.muted).toBeUndefined();

    f.controller.hold('wakeword');
    expect(f.audio.muted).toBe(true);
    // Audio that starts playing during a hold stays silent.
    f.peer.emit('track', { track: {}, streams: [{ id: 'remote-2' }] });
    expect(f.audio.muted).toBe(true);

    f.controller.release('wakeword');
    expect(f.audio.muted).toBe(false);
  });

  it('drops queued and arriving relay audio while held', async () => {
    const f = await relayCall();

    f.controller.hold('wakeword');
    expect(f.relay.clear).toHaveBeenCalledOnce();
    f.socket().handlers.onAudio(new ArrayBuffer(8));
    expect(f.relay.play).not.toHaveBeenCalled();
    // The disabled microphone track makes the relay send silence.
    expect(f.track.enabled).toBe(false);

    f.controller.release('wakeword');
    const speech = new ArrayBuffer(8);
    f.socket().handlers.onAudio(speech);
    expect(f.relay.play).toHaveBeenCalledExactlyOnceWith(speech);
    expect(f.track.enabled).toBe(true);
  });

  it('ends every hold with the call and restores the audio element', async () => {
    const f = await webrtcCall();
    f.controller.hold('wakeword');
    f.controller.hold('calibration');

    f.controller.stop();
    expect(f.audio.muted).toBe(false);
    f.frame({ type: 'closed', reason: 'stopped', usage: null });
    expect(f.state.held).toBe(false);
    expect(f.controller.held()).toBe(false);

    await f.goLive();
    expect(f.state.held).toBe(false);
    expect(f.track.enabled).toBe(true);
  });
});
