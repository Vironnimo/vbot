import { describe, expect, it, vi } from 'vitest';

import {
  RELAY_AUDIO_FORMAT,
  createRelayAudio,
  isRelayAudioFormat,
} from '../liveAudio.js';
import {
  CAPTURE_PROCESSOR,
  PLAYER_PROCESSOR,
  createPcmFramer,
  createPlaybackQueue,
  createResampler,
  floatToPcm16,
  pcm16ToFloat,
} from '../liveAudioWorklet.js';

const ramp = (length, start = 0) =>
  Float32Array.from({ length }, (_value, index) => (start + index) / 1000);

describe('relay PCM helpers', () => {
  it('converts float samples to little-endian PCM16 and back', () => {
    const buffer = floatToPcm16(Float32Array.from([0, 1, -1, 2, -2, 0.5, NaN]));
    const view = new DataView(buffer);

    expect(buffer.byteLength).toBe(14);
    expect(view.getInt16(2, true)).toBe(32767);
    expect(view.getInt16(4, true)).toBe(-32768);
    expect(view.getInt16(6, true)).toBe(32767);
    expect(view.getInt16(8, true)).toBe(-32768);
    expect(view.getInt16(12, true)).toBe(0);
    expect(new Uint8Array(buffer, 2, 2)).toEqual(Uint8Array.from([0xff, 0x7f]));
    const decoded = pcm16ToFloat(buffer);
    expect(decoded[1]).toBeCloseTo(1, 4);
    expect(decoded[2]).toBe(-1);
    expect(decoded[5]).toBeCloseTo(0.5, 4);
    expect(pcm16ToFloat(new ArrayBuffer(3))).toHaveLength(1);
  });

  it('keeps the rate unchanged without resampling', () => {
    const samples = ramp(4);
    expect(createResampler(24000, 24000)(samples)).toBe(samples);
  });

  it.each([
    [48000, 24000],
    [24000, 48000],
    [44100, 24000],
  ])(
    'resamples a stream from %i to %i Hz seamlessly across chunks',
    (fromRate, toRate) => {
      const whole = createResampler(fromRate, toRate)(ramp(960));
      const chunked = createResampler(fromRate, toRate);
      const parts = [];
      for (let start = 0; start < 960; start += 128) {
        parts.push(...chunked(ramp(Math.min(128, 960 - start), start)));
      }

      expect(parts.length).toBe(whole.length);
      expect(parts.length).toBeGreaterThanOrEqual(
        Math.floor((960 * toRate) / fromRate) - 2,
      );
      parts.forEach((value, index) => {
        // A linear ramp stays linear: sample n sits at input n * fromRate/toRate.
        expect(value).toBeCloseTo((index * fromRate) / toRate / 1000, 5);
        expect(whole[index]).toBeCloseTo(value, 6);
      });
    },
  );

  it('frames samples into fixed-size PCM16 frames', () => {
    const frame = createPcmFramer(4);

    expect(frame(ramp(3))).toEqual([]);
    const frames = frame(ramp(6, 3));
    expect(frames).toHaveLength(2);
    expect(frames.every((buffer) => buffer.byteLength === 8)).toBe(true);
    expect(
      Array.from(pcm16ToFloat(frames[1]), (v) => Math.round(v * 1000)),
    ).toEqual([4, 5, 6, 7]);
    expect(frame(ramp(3, 9))).toHaveLength(1);
  });

  it('plays queued samples in order, drops the oldest beyond the limit and clears', () => {
    const queue = createPlaybackQueue(5);
    queue.push(Float32Array.from([1, 2, 3]));
    queue.push(Float32Array.from([4, 5, 6, 7]));
    expect(queue.size).toBe(5);

    const output = new Float32Array(4);
    expect(queue.read(output)).toBe(4);
    expect(Array.from(output)).toEqual([3, 4, 5, 6]);
    expect(queue.read(output)).toBe(1);
    expect(output[0]).toBe(7);
    expect(queue.read(output)).toBe(0);

    queue.push(Float32Array.from([8, 9]));
    queue.clear();
    expect(queue.size).toBe(0);
    expect(queue.read(output)).toBe(0);
  });
});

function audioFixture({ sourceFailsAt = null, state = 'running' } = {}) {
  const contexts = [];
  const nodes = [];
  class FakePort {
    constructor() {
      this.posted = [];
      this.onmessage = null;
    }
    postMessage(message, transfer) {
      this.posted.push({ message, transfer });
    }
  }
  class FakeContext {
    constructor(options) {
      this.options = options;
      this.state = 'suspended';
      this.destination = { name: 'destination' };
      this.audioWorklet = { addModule: vi.fn().mockResolvedValue(undefined) };
      this.resume = vi.fn(async () => {
        this.state = state;
      });
      this.close = vi.fn().mockResolvedValue(undefined);
      this.source = {
        connect: vi.fn(),
        disconnect: vi.fn(),
      };
      contexts.push(this);
    }
    createMediaStreamSource(stream) {
      if (sourceFailsAt === contexts.length) throw new Error('rate mismatch');
      this.stream = stream;
      return this.source;
    }
  }
  class FakeNode {
    constructor(context, name, options) {
      Object.assign(this, { context, name, options, port: new FakePort() });
      this.connect = vi.fn();
      this.disconnect = vi.fn();
      nodes.push(this);
    }
  }
  const node = (name) => nodes.find((candidate) => candidate.name === name);
  return { FakeContext, FakeNode, contexts, node };
}

describe('relay audio engine', () => {
  const microphone = { id: 'microphone' };

  it('describes the relay format it supports', () => {
    expect(isRelayAudioFormat(RELAY_AUDIO_FORMAT)).toBe(true);
    expect(
      isRelayAudioFormat({
        encoding: 'pcm16',
        sample_rate: 16000,
        channels: 1,
      }),
    ).toBe(false);
    expect(isRelayAudioFormat(undefined)).toBe(false);
  });

  it('is unsupported without AudioWorklet support', async () => {
    await expect(
      createRelayAudio({
        microphone,
        onFrame: vi.fn(),
        AudioContextClass: class {},
        AudioWorkletNodeClass: undefined,
      }),
    ).rejects.toMatchObject({ code: 'audio_unsupported' });
  });

  it('captures microphone frames and plays relayed audio at 24 kHz', async () => {
    const f = audioFixture();
    const onFrame = vi.fn();

    const relay = await createRelayAudio({
      microphone,
      onFrame,
      AudioContextClass: f.FakeContext,
      AudioWorkletNodeClass: f.FakeNode,
      workletUrl: 'https://app.test/liveAudioWorklet.js',
    });

    const [context] = f.contexts;
    expect(context.options).toEqual({
      sampleRate: 24000,
      latencyHint: 'interactive',
    });
    expect(context.stream).toBe(microphone);
    expect(context.audioWorklet.addModule).toHaveBeenCalledWith(
      'https://app.test/liveAudioWorklet.js',
    );
    const capture = f.node(CAPTURE_PROCESSOR);
    const player = f.node(PLAYER_PROCESSOR);
    expect(capture.options.processorOptions).toEqual({
      sampleRate: 24000,
      frameMs: 40,
    });
    expect(context.source.connect).toHaveBeenCalledWith(capture);
    expect(capture.connect).toHaveBeenCalledWith(context.destination);
    expect(player.connect).toHaveBeenCalledWith(context.destination);

    const frame = new ArrayBuffer(1920);
    capture.port.onmessage({ data: frame });
    capture.port.onmessage({ data: 'noise' });
    expect(onFrame).toHaveBeenCalledExactlyOnceWith(frame);

    const speech = new ArrayBuffer(8);
    relay.play(speech);
    relay.play(new ArrayBuffer(1));
    relay.clear();
    expect(player.port.posted).toEqual([
      { message: speech, transfer: [speech] },
      { message: { type: 'clear' }, transfer: undefined },
    ]);

    relay.close();
    relay.close();
    relay.play(new ArrayBuffer(8));
    expect(player.port.posted).toHaveLength(2);
    expect(capture.port.onmessage).toBeNull();
    expect(capture.disconnect).toHaveBeenCalledOnce();
    expect(context.close).toHaveBeenCalledOnce();
  });

  it('falls back to the device rate when the microphone cannot join a 24 kHz context', async () => {
    const f = audioFixture({ sourceFailsAt: 1 });

    await createRelayAudio({
      microphone,
      onFrame: vi.fn(),
      AudioContextClass: f.FakeContext,
      AudioWorkletNodeClass: f.FakeNode,
    });

    expect(f.contexts).toHaveLength(2);
    expect(f.contexts[0].close).toHaveBeenCalledOnce();
    expect(f.contexts[1].options).toEqual({ latencyHint: 'interactive' });
    expect(f.node(CAPTURE_PROCESSOR).context).toBe(f.contexts[1]);
  });

  it('reports blocked playback when the context stays suspended', async () => {
    const f = audioFixture({ state: 'suspended' });

    await expect(
      createRelayAudio({
        microphone,
        onFrame: vi.fn(),
        AudioContextClass: f.FakeContext,
        AudioWorkletNodeClass: f.FakeNode,
        resumeTimeoutMs: 5,
      }),
    ).rejects.toMatchObject({ code: 'playback_blocked' });
    expect(f.contexts[0].close).toHaveBeenCalledOnce();
  });

  it('is unsupported when the worklet module cannot load', async () => {
    const f = audioFixture();
    const Context = class extends f.FakeContext {
      constructor(options) {
        super(options);
        this.audioWorklet.addModule.mockRejectedValue(new Error('blocked'));
      }
    };

    await expect(
      createRelayAudio({
        microphone,
        onFrame: vi.fn(),
        AudioContextClass: Context,
        AudioWorkletNodeClass: f.FakeNode,
      }),
    ).rejects.toMatchObject({ code: 'audio_unsupported' });
    expect(f.contexts[0].close).toHaveBeenCalledOnce();
  });
});
