// AudioWorklet module for relayed Live voice audio. It runs in the
// AudioWorkletGlobalScope, so it must stay self-contained: no imports. The
// pure helpers are exported for tests; the processors register only where
// the worklet globals exist.
//
// Relay audio is PCM16 little-endian mono at the relay rate (24 kHz). The
// capture processor resamples microphone input to that rate and posts
// fixed-size PCM frames (ArrayBuffers). The player processor queues PCM
// frames, resamples them to the context rate and plays them; `{type:
// 'clear'}` drops everything queued at once (barge-in).

export const CAPTURE_PROCESSOR = 'vbot-live-capture';
export const PLAYER_PROCESSOR = 'vbot-live-player';
// Queued playback beyond this is dropped oldest first. Relayed audio arrives
// faster than real time, so the queue holds most of one long answer.
export const PLAYBACK_LIMIT_SECONDS = 60;

// Float samples in [-1, 1] to PCM16 little-endian bytes.
export function floatToPcm16(samples) {
  const buffer = new ArrayBuffer(samples.length * 2);
  const view = new DataView(buffer);
  for (let index = 0; index < samples.length; index += 1) {
    const sample = Math.max(-1, Math.min(1, samples[index] || 0));
    view.setInt16(
      index * 2,
      sample < 0 ? Math.round(sample * 0x8000) : Math.round(sample * 0x7fff),
      true,
    );
  }
  return buffer;
}

// PCM16 little-endian bytes to float samples; a trailing odd byte is ignored.
export function pcm16ToFloat(buffer) {
  const view = new DataView(buffer);
  const samples = new Float32Array(Math.floor(buffer.byteLength / 2));
  for (let index = 0; index < samples.length; index += 1) {
    samples[index] = view.getInt16(index * 2, true) / 0x8000;
  }
  return samples;
}

// A streaming linear resampler. It keeps its read position and the last
// input sample between chunks, so consecutive chunks resample seamlessly.
export function createResampler(fromRate, toRate) {
  if (!(fromRate > 0) || !(toRate > 0) || fromRate === toRate) {
    return (input) => input;
  }
  const step = fromRate / toRate;
  let position = 0;
  let carry = null;
  return (input) => {
    if (input.length === 0) return new Float32Array(0);
    let buffer = input;
    if (carry !== null) {
      buffer = new Float32Array(input.length + 1);
      buffer[0] = carry;
      buffer.set(input, 1);
    }
    const last = buffer.length - 1;
    const output = new Float32Array(
      Math.max(0, Math.ceil((last - position) / step)) + 1,
    );
    let written = 0;
    while (position < last) {
      const index = Math.floor(position);
      const fraction = position - index;
      output[written] =
        buffer[index] + (buffer[index + 1] - buffer[index]) * fraction;
      written += 1;
      position += step;
    }
    position -= last;
    carry = buffer[last];
    return output.subarray(0, written);
  };
}

// Collects float samples into PCM16 frames of `frameSamples` samples each.
export function createPcmFramer(frameSamples) {
  const pending = new Float32Array(frameSamples);
  let filled = 0;
  return (samples) => {
    const frames = [];
    let offset = 0;
    while (offset < samples.length) {
      const count = Math.min(frameSamples - filled, samples.length - offset);
      pending.set(samples.subarray(offset, offset + count), filled);
      filled += count;
      offset += count;
      if (filled === frameSamples) {
        frames.push(floatToPcm16(pending));
        filled = 0;
      }
    }
    return frames;
  };
}

// A bounded FIFO of float samples; the oldest samples go first on overflow.
export function createPlaybackQueue(limitSamples) {
  let chunks = [];
  let head = 0;
  let size = 0;
  return {
    get size() {
      return size;
    },
    push(samples) {
      if (samples.length === 0) return;
      chunks.push(samples);
      size += samples.length;
      while (size > limitSamples && chunks.length > 0) {
        const available = chunks[0].length - head;
        const excess = size - limitSamples;
        if (available <= excess) {
          chunks.shift();
          head = 0;
          size -= available;
        } else {
          head += excess;
          size -= excess;
        }
      }
    },
    clear() {
      chunks = [];
      head = 0;
      size = 0;
    },
    // Fills `output` from the queue; returns how many samples were written.
    read(output) {
      let written = 0;
      while (written < output.length && chunks.length > 0) {
        const chunk = chunks[0];
        const count = Math.min(output.length - written, chunk.length - head);
        output.set(chunk.subarray(head, head + count), written);
        written += count;
        head += count;
        size -= count;
        if (head === chunk.length) {
          chunks.shift();
          head = 0;
        }
      }
      return written;
    },
  };
}

// Globals of the AudioWorkletGlobalScope; `sampleRate` is the context rate.
const {
  AudioWorkletProcessor: WorkletProcessor,
  registerProcessor: register,
  sampleRate: contextRate,
} = globalThis;

if (typeof register === 'function' && typeof WorkletProcessor === 'function') {
  class CaptureProcessor extends WorkletProcessor {
    constructor(options) {
      super();
      const relayRate = options?.processorOptions?.sampleRate;
      const frameMs = options?.processorOptions?.frameMs;
      this.resample = createResampler(contextRate, relayRate);
      this.frame = createPcmFramer(Math.round((relayRate * frameMs) / 1000));
    }

    process(inputs) {
      const samples = inputs[0]?.[0];
      if (samples) {
        for (const frame of this.frame(this.resample(samples))) {
          this.port.postMessage(frame, [frame]);
        }
      }
      return true;
    }
  }

  class PlayerProcessor extends WorkletProcessor {
    constructor(options) {
      super();
      this.relayRate = options?.processorOptions?.sampleRate;
      this.resample = createResampler(this.relayRate, contextRate);
      this.queue = createPlaybackQueue(
        Math.round(contextRate * PLAYBACK_LIMIT_SECONDS),
      );
      this.port.onmessage = (event) => {
        const data = event.data;
        if (data instanceof ArrayBuffer) {
          this.queue.push(this.resample(pcm16ToFloat(data)));
        } else if (data?.type === 'clear') {
          this.queue.clear();
          this.resample = createResampler(this.relayRate, contextRate);
        }
      };
    }

    process(_inputs, outputs) {
      const channels = outputs[0] ?? [];
      const first = channels[0];
      if (!first) return true;
      const written = this.queue.read(first);
      first.fill(0, written);
      for (let index = 1; index < channels.length; index += 1) {
        channels[index].set(first);
      }
      return true;
    }
  }

  register(CAPTURE_PROCESSOR, CaptureProcessor);
  register(PLAYER_PROCESSOR, PlayerProcessor);
}
