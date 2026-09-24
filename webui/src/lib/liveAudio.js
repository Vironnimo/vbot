// Relay audio engine of a Live voice call: microphone capture to PCM frames
// and playback of relayed PCM, both through one AudioContext and the
// processors in liveAudioWorklet.js.
//
// Playback goes to the context destination, the device output mix, which
// the browser's echo cancellation of the microphone track uses as its
// reference. The microphone is never gated while the assistant speaks, so
// the user can always interrupt.

const WORKLET_URL = new URL('./liveAudioWorklet.js', import.meta.url);
// Must match liveAudioWorklet.js; that module cannot be imported here
// without bundling the worklet into the page.
const CAPTURE_PROCESSOR = 'vbot-live-capture';
const PLAYER_PROCESSOR = 'vbot-live-player';
const CAPTURE_FRAME_MS = 40;
// The relay audio format of the Live call protocol, both directions.
export const RELAY_AUDIO_FORMAT = Object.freeze({
  encoding: 'pcm16',
  sample_rate: 24000,
  channels: 1,
});
// A context that stays suspended this long after resume() is blocked.
const RESUME_TIMEOUT_MS = 2000;

// Whether a call's `media.audio` descriptor is the format this engine relays.
export function isRelayAudioFormat(audio) {
  return (
    audio?.encoding === RELAY_AUDIO_FORMAT.encoding &&
    audio?.sample_rate === RELAY_AUDIO_FORMAT.sample_rate &&
    audio?.channels === RELAY_AUDIO_FORMAT.channels
  );
}

export class RelayAudioError extends Error {
  constructor(code) {
    super(code);
    this.name = 'RelayAudioError';
    this.code = code;
  }
}

async function closeQuietly(context) {
  try {
    await context?.close?.();
  } catch {
    // Already closed.
  }
}

async function resumeContext(context, timeoutMs) {
  if (context.state === 'running') return;
  let timer;
  await Promise.race([
    Promise.resolve()
      .then(() => context.resume())
      .catch(() => {}),
    new Promise((resolve) => {
      timer = setTimeout(resolve, timeoutMs);
    }),
  ]);
  clearTimeout(timer);
  if (context.state !== 'running')
    throw new RelayAudioError('playback_blocked');
}

// Starts capture and playback for `microphone` (a MediaStream). `onFrame`
// receives each captured PCM frame (an ArrayBuffer in RELAY_AUDIO_FORMAT,
// 40 ms). `play` takes relayed PCM in the same format. Resolves to
// `{play(buffer), clear(), close()}`; rejects with a RelayAudioError coded
// `audio_unsupported` or `playback_blocked`.
export async function createRelayAudio({
  microphone,
  onFrame,
  AudioContextClass = globalThis.AudioContext,
  AudioWorkletNodeClass = globalThis.AudioWorkletNode,
  workletUrl = WORKLET_URL,
  resumeTimeoutMs = RESUME_TIMEOUT_MS,
}) {
  if (
    typeof AudioContextClass !== 'function' ||
    typeof AudioWorkletNodeClass !== 'function'
  ) {
    throw new RelayAudioError('audio_unsupported');
  }
  const sampleRate = RELAY_AUDIO_FORMAT.sample_rate;
  let context = null;
  try {
    let source;
    context = new AudioContextClass({ sampleRate, latencyHint: 'interactive' });
    try {
      source = context.createMediaStreamSource(microphone);
    } catch {
      // Some browsers cannot feed a microphone into a context at another
      // rate; the worklet resamples at the device rate instead.
      await closeQuietly(context);
      context = new AudioContextClass({ latencyHint: 'interactive' });
      source = context.createMediaStreamSource(microphone);
    }
    if (typeof context.audioWorklet?.addModule !== 'function') {
      throw new RelayAudioError('audio_unsupported');
    }
    await context.audioWorklet.addModule(String(workletUrl));
    const processorOptions = { sampleRate, frameMs: CAPTURE_FRAME_MS };
    const capture = new AudioWorkletNodeClass(context, CAPTURE_PROCESSOR, {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      outputChannelCount: [1],
      channelCount: 1,
      channelCountMode: 'explicit',
      processorOptions,
    });
    const player = new AudioWorkletNodeClass(context, PLAYER_PROCESSOR, {
      numberOfInputs: 0,
      numberOfOutputs: 1,
      outputChannelCount: [1],
      processorOptions,
    });
    capture.port.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) onFrame(event.data);
    };
    source.connect(capture);
    // The capture node outputs silence; its connection keeps it rendering.
    capture.connect(context.destination);
    player.connect(context.destination);
    await resumeContext(context, resumeTimeoutMs);

    const audioContext = context;
    let closed = false;
    return {
      play(buffer) {
        if (closed || !(buffer instanceof ArrayBuffer) || buffer.byteLength < 2)
          return;
        player.port.postMessage(buffer, [buffer]);
      },
      clear() {
        if (!closed) player.port.postMessage({ type: 'clear' });
      },
      close() {
        if (closed) return;
        closed = true;
        capture.port.onmessage = null;
        for (const node of [source, capture, player]) {
          try {
            node.disconnect();
          } catch {
            // Not connected.
          }
        }
        void closeQuietly(audioContext);
      },
    };
  } catch (error) {
    await closeQuietly(context);
    throw error instanceof RelayAudioError
      ? error
      : new RelayAudioError('audio_unsupported');
  }
}
