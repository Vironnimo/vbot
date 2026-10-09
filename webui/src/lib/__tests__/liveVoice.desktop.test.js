import { describe, expect, it, vi } from 'vitest';

import {
  deferred,
  flush,
  liveFixture,
  RELAY_RESULT,
  RELAY_STATUS,
  relayFixture,
} from './liveVoice.support.js';

// The hooks the Desktop integration uses: the microphone access check, the
// wake phrases named with each call and holds while a wake phrase is handled.

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
    f.api.getLiveVoiceStatus.mockResolvedValue(RELAY_STATUS);
    f.api.startLiveCall.mockResolvedValue(RELAY_RESULT);
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
  async function relayCall() {
    const f = relayFixture();
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
    expect(f.relay.setEnabled).toHaveBeenLastCalledWith(false);
    f.socket().handlers.onAudio(new ArrayBuffer(8));
    expect(f.relay.play).toHaveBeenCalledOnce();
    // The disabled microphone track makes the relay send silence.
    expect(f.track.enabled).toBe(false);

    f.controller.release('wakeword');
    expect(f.relay.setEnabled).toHaveBeenLastCalledWith(true);
    const speech = new ArrayBuffer(8);
    f.socket().handlers.onAudio(speech);
    expect(f.relay.play).toHaveBeenLastCalledWith(speech);
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
