import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  deferred,
  flush,
  liveFixture,
  RELAY_RESULT,
  relayFixture,
} from './liveVoice.support.js';

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
    [
      'a denied permission',
      { getUserMedia: vi.fn().mockRejectedValue({ name: 'NotAllowedError' }) },
      'microphone_denied',
    ],
    [
      'a missing device',
      { getUserMedia: vi.fn().mockRejectedValue({ name: 'NotFoundError' }) },
      'microphone_unavailable',
    ],
    // Insecure pages lack the microphone API.
    ['no microphone API', {}, 'microphone_unavailable'],
  ])('reports %s as %s', async (_label, mediaDevices, code) => {
    const f = liveFixture({ mediaDevices });
    await f.controller.start();
    expect(f.onNotice).toHaveBeenCalledExactlyOnceWith({
      code,
      severity: 'error',
    });
    expect(f.api.startLiveCall).not.toHaveBeenCalled();
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
      action: 'open',
      args: { view: 'chat' },
    });
    previous.handlers.onEvent({ type: 'closed', reason: 'replaced' });
    previous.handlers.onClose();
    await flush();
    expect(f.uiActions.open).not.toHaveBeenCalled();
    expect(f.state).toMatchObject({ phase: 'connecting', callId: 'call-2' });
    expect(f.onNotice).not.toHaveBeenCalled();
  });
});

describe('Live voice relay media', () => {
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

    const speech = {
      generation: 1,
      start_samples: 0,
      buffer: new ArrayBuffer(8),
    };
    f.socket().handlers.onAudio(speech);
    expect(f.relay.play).toHaveBeenCalledExactlyOnceWith(speech);
    f.frame({ type: 'playback_clear', generation: 2 });
    expect(f.relay.clear).toHaveBeenCalledExactlyOnceWith(2);

    f.controller.stop();
    expect(f.relay.close).toHaveBeenCalledOnce();
    f.relay.onFrame(new ArrayBuffer(4));
    f.socket().handlers.onAudio(new ArrayBuffer(8));
    expect(f.socket().sendAudio).toHaveBeenCalledOnce();
    expect(f.relay.play).toHaveBeenCalledOnce();
    expect(f.onNotice).not.toHaveBeenCalled();
  });

  it('reports rendered prefixes and resynchronizes relay playback after socket loss', async () => {
    vi.useFakeTimers();
    const f = relayFixture();
    await f.goLive();
    f.socket().handlers.onOpen();
    expect(f.socket().sendJson).toHaveBeenCalledWith({
      type: 'playback',
      generation: 0,
      played_samples: 0,
      enabled: true,
    });
    expect(f.relay.setEnabled).toHaveBeenLastCalledWith(true);
    f.frame({ type: 'playback_clear', generation: 1 });
    f.relay.onPlayback({ generation: 1, played_samples: 128, enabled: true });
    expect(f.socket().sendJson).toHaveBeenLastCalledWith({
      type: 'playback',
      generation: 1,
      played_samples: 128,
      enabled: true,
    });
    f.socket().handlers.onClose({}, 'lost');
    expect(f.relay.setEnabled).toHaveBeenLastCalledWith(false);
    f.relay.onPlayback({
      generation: 1,
      played_samples: 256,
      enabled: false,
      cleared: true,
    });
    await vi.advanceTimersByTimeAsync(500);
    f.socket().handlers.onOpen();
    expect(f.socket().sendJson).toHaveBeenLastCalledWith({
      type: 'playback',
      generation: 1,
      played_samples: 256,
      enabled: true,
    });
    expect(f.relay.setEnabled).toHaveBeenLastCalledWith(true);
    expect(f.relay.report).toHaveBeenCalledTimes(2);
    f.frame({ type: 'playback_clear', generation: 2 });
    expect(f.relay.clear).toHaveBeenLastCalledWith(2);
    f.controller.destroy();
    f.relay.onPlayback({ generation: 2, played_samples: 128, enabled: true });
    expect(f.socket().sendJson).toHaveBeenLastCalledWith({
      type: 'playback',
      generation: 1,
      played_samples: 256,
      enabled: true,
    });
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

  it.each([
    [
      'playback_blocked',
      Object.assign(new Error('blocked'), { code: 'playback_blocked' }),
    ],
    // A failure without a code means the browser lacks relay audio.
    ['audio_unsupported', new Error('no audio worklet')],
  ])(
    'fails with %s before creating a call when relay audio cannot start',
    async (code, error) => {
      const f = relayFixture();
      f.createAudio.mockRejectedValue(error);

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

  it('closes after a timeout when no closed frame arrives', async () => {
    vi.useFakeTimers();
    const f = liveFixture();
    await f.goLive();
    f.controller.stop();
    await vi.advanceTimersByTimeAsync(11999);
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
    ['after it was live', true, null, { code: 'ended', severity: 'info' }],
    [
      'before it was live',
      false,
      null,
      { code: 'connection_failed', severity: 'error' },
    ],
    [
      'after the voice model hung up',
      true,
      'hung_up',
      { code: 'hung_up', severity: 'info' },
    ],
    ['after idling', true, 'idle', { code: 'idle', severity: 'info' }],
    [
      'at the provider limit',
      true,
      'expired',
      { code: 'expired', severity: 'info' },
    ],
  ])(
    'reports a call the server ended %s',
    async (_label, live, reason, notice) => {
      const f = liveFixture();
      await f.controller.start();
      if (live) f.frame({ type: 'state', phase: 'live' });
      f.frame({ type: 'closed', reason, usage: null });
      expect(f.onNotice).toHaveBeenCalledExactlyOnceWith(notice);
      expect(f.state.phase).toBe('off');
    },
  );

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
      { seq: 1, role: 'user', text: 'Open the chat', final: false },
      { seq: 2, role: 'assistant', text: 'Sure', final: false },
    ]);
    f.frame({
      type: 'caption',
      role: 'user',
      text: 'Open the chat.',
      final: true,
    });
    f.frame({ type: 'caption', role: 'assistant', text: '', final: true });
    expect(f.state.captions).toEqual([
      { seq: 1, role: 'user', text: 'Open the chat.', final: true },
      { seq: 2, role: 'assistant', text: 'Sure', final: true },
    ]);
  });

  it('bounds caption turns and their text', async () => {
    const f = liveFixture();
    await f.goLive();
    for (let index = 0; index < 45; index += 1)
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
    expect(f.state.captions).toHaveLength(40);
    expect(f.state.captions[0].text).toBe('turn 6');
    expect(f.state.captions.at(-1).text).toHaveLength(2000);
    expect(f.state.captions.at(-1).text.endsWith('end')).toBe(true);
  });

  it('keeps the operator actions with their links in order with the captions', async () => {
    const f = liveFixture();
    await f.goLive();
    f.frame({
      type: 'caption',
      role: 'user',
      text: 'Start Coder',
      final: true,
    });
    f.frame({
      type: 'action',
      tool: 'start_agent_session',
      ok: true,
      arguments: { agent: 'coder', task: 'Fix it' },
      result: 'Started a Session at Coder with the task: s1.',
      links: [
        {
          ref: 's1',
          kind: 'session',
          agent_id: 'coder',
          session_id: 'ses_1',
          label: 'Session at Coder',
        },
        { ref: 't1', kind: 'terminal', terminal_id: 'term_a', label: '' },
        { ref: 's2', kind: 'session', agent_id: 'coder' },
        { ref: 'x1', kind: 'browser', url: 'https://example.com' },
        'not a link',
      ],
    });
    f.frame({ type: 'action', ok: true });
    expect(f.state.actions).toEqual([
      {
        seq: 2,
        tool: 'start_agent_session',
        ok: true,
        arguments: { agent: 'coder', task: 'Fix it' },
        result: 'Started a Session at Coder with the task: s1.',
        links: [
          {
            ref: 's1',
            kind: 'session',
            label: 'Session at Coder',
            agent_id: 'coder',
            session_id: 'ses_1',
          },
          { ref: 't1', kind: 'terminal', label: 't1', terminal_id: 'term_a' },
        ],
      },
    ]);
    expect(f.state.captions[0].seq).toBe(1);

    // The record outlasts the call and starts over with the next one.
    f.frame({ type: 'closed', reason: 'hung_up', usage: null });
    expect(f.state.actions).toHaveLength(1);
    expect(f.state.captions).toHaveLength(1);
    await f.goLive();
    expect(f.state.actions).toEqual([]);
    expect(f.state.captions).toEqual([]);
  });

  it('names the Sessions of the call as the server creates them', async () => {
    const f = liveFixture();
    await f.goLive();
    const voice = { agent_id: 'live-voice', session_id: 'call-1' };
    f.frame({ type: 'sessions', voice, backend: null });
    expect(f.state.sessions).toEqual({ voice, backend: null });
    // The backend Session follows once its first request created it.
    const backend = { agent_id: 'live-backend', session_id: 'call-2' };
    f.frame({ type: 'sessions', voice, backend });
    expect(f.state.sessions).toEqual({ voice, backend });
    f.frame({ type: 'sessions', voice: { agent_id: 'live-voice' } });
    expect(f.state.sessions).toEqual({ voice: null, backend: null });

    // They stay after the call and start over with the next one.
    f.frame({ type: 'sessions', voice, backend });
    f.frame({ type: 'closed', reason: 'hung_up', usage: null });
    expect(f.state.sessions).toEqual({ voice, backend });
    await f.goLive();
    expect(f.state.sessions).toEqual({ voice: null, backend: null });
  });

  it('knows how long the call runs, when the provider ends it and when it idles out', async () => {
    let clock = 1_000_000;
    const f = liveFixture({ now: () => clock });
    await f.goLive();
    expect(f.state.liveSince).toBe(1_000_000);
    f.frame({ type: 'expiry', seconds: 3600 });
    f.frame({ type: 'idle', ends_in: 60 });
    expect(f.state).toMatchObject({
      expiresAt: 4_600_000,
      idleEndsAt: 1_060_000,
    });
    // A warning reaches the user also where the sidebar shows no caption.
    f.frame({ type: 'idle', ends_in: 50 });
    expect(f.onNotice).toHaveBeenCalledExactlyOnceWith({
      code: 'idle_warning',
      severity: 'warn',
    });
    // Keeping the call asks the server; its answer withdraws the warning.
    expect(f.controller.stay()).toBe(true);
    expect(f.socket().sendJson).toHaveBeenCalledWith({ type: 'stay' });
    f.frame({ type: 'idle', ends_in: null });
    expect(f.state.idleEndsAt).toBeNull();
    f.frame({ type: 'idle', ends_in: 'soon' });
    expect(f.state.idleEndsAt).toBeNull();

    clock += 5000;
    f.frame({ type: 'idle', ends_in: 30 });
    f.controller.stop();
    expect(f.state.idleEndsAt).toBeNull();
    expect(f.controller.stay()).toBe(false);
    f.frame({ type: 'closed', reason: 'closed', usage: null });
    expect(f.state).toMatchObject({
      liveSince: null,
      expiresAt: null,
      idleEndsAt: null,
    });
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

  it('gives a reopened socket a fresh reattach window', async () => {
    vi.useFakeTimers();
    const f = liveFixture();
    await f.goLive();
    f.socket().handlers.onClose({}, 'lost');
    await vi.advanceTimersByTimeAsync(500);
    f.socket().handlers.onOpen();
    await vi.advanceTimersByTimeAsync(9000);
    f.socket().handlers.onClose({}, 'lost');
    await vi.advanceTimersByTimeAsync(500);
    expect(f.api.openLiveCallSocket).toHaveBeenCalledTimes(3);
    expect(f.onNotice).not.toHaveBeenCalled();
    expect(f.state.phase).toBe('live');
  });

  it('replaces a socket that stays silent past the heartbeat', async () => {
    vi.useFakeTimers();
    const f = liveFixture();
    await f.goLive();
    const silent = f.socket();
    await vi.advanceTimersByTimeAsync(59000);
    expect(f.api.openLiveCallSocket).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(11000);
    expect(silent.close).toHaveBeenCalledOnce();
    expect(f.api.openLiveCallSocket).toHaveBeenCalledTimes(2);
    expect(f.onNotice).not.toHaveBeenCalled();
  });

  it.each([
    // A socket closed as ended is a call that ended.
    ['ended', { code: 'ended', severity: 'info' }, false],
    ['unknown_call', { code: 'connection_lost', severity: 'error' }, true],
    ['replaced', { code: 'connection_lost', severity: 'error' }, true],
  ])(
    'ends the call without reattaching when the socket closes as %s',
    async (outcome, notice, stopsCall) => {
      vi.useFakeTimers();
      const f = liveFixture();
      await f.goLive();
      f.socket().handlers.onClose({}, outcome);
      await vi.advanceTimersByTimeAsync(1000);
      expect(f.api.openLiveCallSocket).toHaveBeenCalledOnce();
      expect(f.onNotice).toHaveBeenCalledExactlyOnceWith(notice);
      expect(f.api.stopLiveCall.mock.calls).toEqual(
        stopsCall ? [['call-1']] : [],
      );
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

  it('silences the assistant without muting the microphone', async () => {
    const f = relayFixture();
    await f.goLive();
    f.controller.muteSpeaker();
    expect(f.state.speakerMuted).toBe(true);
    expect(f.relay.setEnabled).toHaveBeenLastCalledWith(false);
    f.socket().handlers.onAudio(new ArrayBuffer(4));
    // The worklet owns dropping muted frames and the final rendered count.
    expect(f.relay.play).toHaveBeenCalledOnce();
    expect(f.track.enabled).toBe(true);
    f.controller.muteSpeaker();
    expect(f.relay.setEnabled).toHaveBeenLastCalledWith(true);
    f.socket().handlers.onAudio(new ArrayBuffer(4));
    expect(f.relay.play).toHaveBeenCalledTimes(2);

    // WebRTC audio plays through the element, which is muted instead.
    const g = liveFixture();
    await g.goLive();
    g.peer.emit('track', { track: {}, streams: [{ id: 'remote' }] });
    g.controller.muteSpeaker(true);
    expect(g.audio.muted).toBe(true);
    g.controller.stop();
    expect(g.audio.muted).toBe(false);
    g.frame({ type: 'closed', reason: 'closed', usage: null });
    expect(g.state.speakerMuted).toBe(false);
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
