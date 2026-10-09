// AudioWorklet module for relayed Live voice audio. It runs in the
// AudioWorkletGlobalScope, so it must stay self-contained: no imports. The
// pure helpers are exported for tests; the processors register only where
// the worklet globals exist.
//
// Relay audio is PCM16 little-endian mono at the relay rate (24 kHz). The
// capture processor resamples microphone input to that rate and posts
// fixed-size PCM frames (ArrayBuffers). The player processor queues PCM
// frames, resamples them to the context rate and reports the source samples
// actually rendered. Generations fence cleared or missing output.

export const CAPTURE_PROCESSOR = 'vbot-live-capture';
export const PLAYER_PROCESSOR = 'vbot-live-player';
// Queued playback beyond this is dropped oldest first. Relayed audio arrives
// faster than real time, so the queue holds most of one long answer.
export const PLAYBACK_LIMIT_SECONDS = 60;
// A worklet has fixed device/relay rates; reuse its two coefficient tables
// when a clear or underrun starts another stream.
const RESAMPLER_FILTERS = new Map();

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

// Streaming, band-limited resampling. A Blackman-windowed sinc removes
// frequencies above the destination's Nyquist limit before downsampling and
// suppresses images when upsampling. The short lookahead (1.34 ms at 24 kHz)
// stays across chunks. flush() zero-pads only a real playback underrun/end.
export function createResampler(fromRate, toRate) {
  if (!(fromRate > 0) || !(toRate > 0) || fromRate === toRate) {
    const passthrough = (input) => input;
    passthrough.flush = () => new Float32Array(0);
    return passthrough;
  }
  const step = fromRate / toRate;
  const radius = Math.ceil(32 * Math.max(1, step));
  const phases = 256;
  const cutoff = 0.47 * Math.min(1, 1 / step);
  const filterKey = `${fromRate}/${toRate}`;
  const kernels =
    RESAMPLER_FILTERS.get(filterKey) ??
    Array.from({ length: phases }, (_, phase) => {
      const kernel = new Float64Array(radius * 2 + 1);
      let sum = 0;
      for (let tap = 0; tap < kernel.length; tap += 1) {
        const distance = tap - radius - phase / phases;
        const x = 2 * Math.PI * cutoff * distance;
        const sinc = Math.abs(x) < 1e-10 ? 1 : Math.sin(x) / x;
        const angle = (Math.PI * distance) / radius;
        const window =
          Math.abs(distance) > radius
            ? 0
            : 0.42 + 0.5 * Math.cos(angle) + 0.08 * Math.cos(2 * angle);
        kernel[tap] = 2 * cutoff * sinc * window;
        sum += kernel[tap];
      }
      for (let tap = 0; tap < kernel.length; tap += 1) kernel[tap] /= sum;
      return kernel;
    });
  RESAMPLER_FILTERS.set(filterKey, kernels);
  let buffer = new Float32Array(0);
  let bufferStart = 0;
  let received = 0;
  let produced = 0;
  const render = (final) => {
    const end = final ? received : Math.max(0, received - radius);
    const count = Math.max(0, Math.ceil(end / step) - produced);
    const output = new Float32Array(count);
    let written = 0;
    while (written < count) {
      const position = produced * step;
      const center = Math.floor(position);
      const kernel = kernels[Math.floor((position - center) * phases)];
      let sample = 0;
      for (let tap = 0; tap < kernel.length; tap += 1) {
        const index = center - radius + tap - bufferStart;
        if (index >= 0 && index < buffer.length)
          sample += buffer[index] * kernel[tap];
      }
      output[written] = sample;
      written += 1;
      produced += 1;
    }
    const discard = Math.max(
      0,
      Math.min(
        buffer.length,
        Math.floor(produced * step) - radius - bufferStart,
      ),
    );
    buffer = buffer.slice(discard);
    bufferStart += discard;
    return output;
  };
  const resample = (input) => {
    if (!input.length) return new Float32Array(0);
    const next = new Float32Array(buffer.length + input.length);
    next.set(buffer);
    next.set(input, buffer.length);
    buffer = next;
    received += input.length;
    return render(false);
  };
  resample.flush = () => render(true);
  return resample;
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
    push(samples, metadata = null) {
      if (samples.length === 0) return;
      chunks.push({ samples, metadata });
      size += samples.length;
      while (size > limitSamples && chunks.length > 0) {
        const available = chunks[0].samples.length - head;
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
    read(output, onRead = () => {}) {
      let written = 0;
      while (written < output.length && chunks.length > 0) {
        const { samples: chunk, metadata } = chunks[0];
        const count = Math.min(output.length - written, chunk.length - head);
        output.set(chunk.subarray(head, head + count), written);
        written += count;
        head += count;
        size -= count;
        onRead(count, metadata);
        if (head === chunk.length) {
          chunks.shift();
          head = 0;
        }
      }
      return written;
    },
  };
}

// The player's pure state machine is also its test seam. A report describes
// only the contiguous rendered prefix of a generation, in relay samples.
// Rendering precedes physical output by the browser/device output latency;
// this is a renderer acknowledgment, not a measurement at the loudspeaker.
// Missing/overflowed output stops that generation instead of pretending its
// later samples were heard. Only a server clear starts a new generation.
export function createRelayPlayer({ relayRate, outputRate, onPlayback }) {
  const limit = Math.round(outputRate * PLAYBACK_LIMIT_SECONDS);
  const queue = createPlaybackQueue(limit);
  const ratio = relayRate / outputRate;
  let generation = 0;
  let enabled = true;
  let fenced = true;
  let received = 0;
  let played = 0;
  let sinceReport = 0;
  let segment = null;
  const report = (cleared = false) => {
    onPlayback({
      generation,
      played_samples: played,
      enabled,
      ...(cleared && generation > 0 ? { cleared: true } : {}),
    });
    sinceReport = 0;
  };
  const discard = () => {
    queue.clear();
    segment = null;
    fenced = true;
  };
  const enqueue = (samples) => {
    if (queue.size + samples.length > limit) {
      discard();
      report(true);
      return;
    }
    queue.push(samples, segment);
  };
  return {
    receive(data) {
      if (data?.type === 'clear') {
        if (!Number.isInteger(data.generation) || data.generation <= generation)
          return;
        discard();
        report(true);
        generation = data.generation;
        played = 0;
        received = 0;
        fenced = false;
        report();
      } else if (data?.type === 'enabled') {
        const next = data.enabled === true;
        if (enabled !== next) {
          enabled = next;
          if (!enabled) discard();
          report(!enabled);
        }
      } else if (data?.type === 'report') {
        report();
      } else if (data?.type === 'audio') {
        if (!enabled || fenced || data.generation !== generation) return;
        if (data.start_samples !== received) {
          discard();
          report(true);
          return;
        }
        if (!segment || segment.final) {
          segment = {
            start: received,
            received: 0,
            rendered: 0,
            final: false,
            resample: createResampler(relayRate, outputRate),
          };
        }
        const samples = pcm16ToFloat(data.buffer);
        received += samples.length;
        segment.received += samples.length;
        enqueue(segment.resample(samples));
      }
    },
    read(output) {
      if (queue.size < output.length && segment && !segment.final) {
        segment.final = true;
        enqueue(segment.resample.flush());
      }
      const written = queue.read(output, (count, part) => {
        part.rendered += count;
        played =
          part.start +
          Math.min(part.received, Math.floor(part.rendered * ratio));
      });
      output.fill(0, written);
      sinceReport += written;
      if (written && (queue.size === 0 || sinceReport >= outputRate / 10))
        report();
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
      this.player = createRelayPlayer({
        relayRate: options?.processorOptions?.sampleRate,
        outputRate: contextRate,
        onPlayback: (report) => this.port.postMessage(report),
      });
      this.port.onmessage = (event) => this.player.receive(event.data);
    }

    process(_inputs, outputs) {
      const channels = outputs[0] ?? [];
      const first = channels[0];
      if (!first) return true;
      this.player.read(first);
      for (let index = 1; index < channels.length; index += 1) {
        channels[index].set(first);
      }
      return true;
    }
  }

  register(CAPTURE_PROCESSOR, CaptureProcessor);
  register(PLAYER_PROCESSOR, PlayerProcessor);
}
