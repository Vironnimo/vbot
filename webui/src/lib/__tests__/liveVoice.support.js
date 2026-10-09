import { vi } from 'vitest';

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

export const flush = async () => {
  for (let index = 0; index < 20; index += 1) await Promise.resolve();
};

export function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

export const RELAY_STATUS = {
  configured: true,
  usable: true,
  target: 'xai/grok-voice-think-fast-2.0::subscription',
  media: 'relay',
};

export const RELAY_RESULT = {
  call_id: 'call-1',
  media: {
    type: 'relay',
    audio: { encoding: 'pcm16', sample_rate: 24000, channels: 1 },
  },
};

/**
 * A Live voice controller over fakes of the server API, the browser media and
 * the App UI actions. The server offers WebRTC media unless a test says
 * otherwise; `overrides` replace controller options.
 */
export function liveFixture(overrides = {}) {
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
      const socket = {
        callId,
        handlers,
        close: vi.fn(),
        sendAudio: vi.fn(),
        sendJson: vi.fn(() => true),
      };
      sockets.push(socket);
      return socket;
    }),
  };
  const relay = {
    play: vi.fn(),
    clear: vi.fn(),
    setEnabled: vi.fn(),
    report: vi.fn(),
    close: vi.fn(),
    onFrame: null,
  };
  const createAudio = vi.fn(async ({ onFrame, onPlayback }) => {
    relay.onFrame = onFrame;
    relay.onPlayback = onPlayback;
    return relay;
  });
  const mediaDevices = { getUserMedia: vi.fn().mockResolvedValue(microphone) };
  const audio = {
    srcObject: null,
    play: vi.fn().mockResolvedValue(undefined),
    pause: vi.fn(),
  };
  const uiActions = {
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

/** A fixture whose Live voice binding uses relay media. */
export function relayFixture(overrides = {}) {
  const f = liveFixture(overrides);
  f.api.getLiveVoiceStatus.mockResolvedValue(RELAY_STATUS);
  f.api.startLiveCall.mockResolvedValue(RELAY_RESULT);
  return f;
}
