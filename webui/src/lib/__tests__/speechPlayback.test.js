import { afterEach, expect, it, vi } from 'vitest';
import { createSpeechPlayback } from '../speechPlayback.js';

afterEach(() => vi.useRealTimers());

function harness(readAudio) {
  vi.useFakeTimers();
  const nodes = [];
  const context = {
    currentTime: 0,
    destination: {},
    resume: vi.fn(async () => {}),
    suspend: vi.fn(async () => {}),
    close: vi.fn(async () => {}),
    createGain: () => ({ gain: { value: 1 }, connect() {}, disconnect() {} }),
    createBuffer: (_, count) => ({
      getChannelData: () => new Float32Array(count),
    }),
    createBufferSource: () => {
      const node = {
        connect() {},
        disconnect: vi.fn(),
        start: vi.fn(),
        stop: vi.fn(),
      };
      nodes.push(node);
      return node;
    },
  };
  const onState = vi.fn();
  const player = createSpeechPlayback({
    url: '/api/speech/playback/test',
    readAudio,
    onState,
    AudioContextClass: function () {
      return context;
    },
  });
  return { context, nodes, onState, player };
}
async function flush() {
  for (let n = 0; n < 20; n++) await Promise.resolve();
}

it('plays bounded audio before synthesis ends and drains only after a successful terminal', async () => {
  let finish;
  const h = harness(async function* () {
    yield { sampleRate: 16000, bytes: new Uint8Array(32000) };
    await new Promise((resolve) => {
      finish = resolve;
    });
  });
  await h.player.play();
  await flush();
  expect(h.nodes.length).toBe(10);
  expect(h.onState.mock.lastCall[0].paused).toBe(false);
  h.player.pause();
  expect(h.context.suspend).toHaveBeenCalledOnce();
  await h.player.play();
  finish();
  await flush();
  expect(h.onState.mock.calls.some(([state]) => state.ended)).toBe(false);
  h.context.currentTime = 2;
  h.nodes.forEach((node) => node.onended());
  await flush();
  expect(h.onState.mock.lastCall[0]).toMatchObject({
    ended: true,
    paused: true,
    loading: false,
  });
  expect(h.onState.mock.lastCall[0].currentTime).toBeCloseTo(1);
  expect(h.context.close).toHaveBeenCalledOnce();
});

it('bounds scheduled samples while paused and aborts transport and audio on teardown', async () => {
  let signal;
  let read = 0;
  const h = harness(async function* (_, options) {
    signal = options.signal;
    for (let index = 0; index < 20; index++) {
      read++;
      yield { sampleRate: 16000, bytes: new Uint8Array(32000) };
    }
  });
  await h.player.play();
  h.player.pause();
  await flush();
  expect(h.nodes.length).toBeLessThanOrEqual(21);
  expect(read).toBeLessThanOrEqual(3);
  h.player.stop();
  const reports = h.onState.mock.calls.length;
  await flush();
  expect(signal.aborted).toBe(true);
  expect(h.nodes.every((node) => node.stop.mock.calls.length === 1)).toBe(true);
  expect(h.context.close).toHaveBeenCalledOnce();
  expect(h.onState).toHaveBeenCalledTimes(reports);
});

it('stops buffered audio on a transport error instead of treating a partial response as complete', async () => {
  const error = new Error('test-owned stream failure');
  const h = harness(async function* () {
    yield { sampleRate: 16000, bytes: new Uint8Array(3200) };
    throw error;
  });
  await h.player.play();
  await flush();
  expect(h.onState.mock.lastCall[0]).toMatchObject({
    error,
    paused: true,
    loading: false,
  });
  expect(h.nodes[0].stop).toHaveBeenCalledOnce();
  expect(h.context.close).toHaveBeenCalledOnce();
});

it('joins a timely frame without another preroll and reflects suspended audio output', async () => {
  let next;
  const h = harness(async function* () {
    yield { sampleRate: 16000, bytes: new Uint8Array(3200) };
    await new Promise((resolve) => {
      next = resolve;
    });
    yield { sampleRate: 24000, bytes: new Uint8Array(4800) };
  });
  await h.player.play();
  await flush();
  h.context.currentTime = 0.11;
  next();
  await flush();
  expect(h.nodes[0].start.mock.calls[0][0]).toBe(0.02);
  expect(h.nodes[1].start.mock.calls[0][0]).toBeCloseTo(0.12);
  h.context.state = 'suspended';
  h.context.onstatechange();
  expect(h.onState.mock.lastCall[0].paused).toBe(true);
  h.context.state = 'running';
  h.context.onstatechange();
  expect(h.onState.mock.lastCall[0].paused).toBe(false);
  h.player.stop();
});
