import { readSpeechPlayback } from './api.js';

// AudioPlayer's transient media resource. Keep only two seconds scheduled in
// WebAudio and one bounded transport frame, letting HTTP apply backpressure.
// The completed artifact owns seeking and replay; transient audio is never
// retained as a second full recording in the browser.
export function createSpeechPlayback({
  url,
  onState,
  readAudio = readSpeechPlayback,
  AudioContextClass = globalThis.AudioContext ?? globalThis.webkitAudioContext,
}) {
  const abort = new AbortController();
  const sources = new Set();
  let context;
  let gain;
  let timer;
  let started = false;
  let stopped = false;
  let finished = false;
  let paused = true;
  let received = 0;
  let completed = 0;
  let nextStart = 0;
  let volume = 1;
  let wake;
  let request = 0;
  let intentPlaying = false;

  function position() {
    let value = completed;
    for (const entry of sources)
      value += Math.max(
        0,
        Math.min(entry.duration, context.currentTime - entry.start),
      );
    return value;
  }

  function report(extra = {}) {
    if (!stopped)
      onState({
        paused,
        currentTime: position(),
        duration: finished ? received : 0,
        loading: !paused && sources.size === 0 && !finished,
        ...extra,
      });
  }

  function dispose() {
    if (stopped) return;
    stopped = true;
    abort.abort();
    clearInterval(timer);
    wake?.();
    for (const { node } of sources) {
      node.onended = null;
      node.stop();
      node.disconnect();
    }
    sources.clear();
    gain?.disconnect();
    if (context) context.onstatechange = null;
    void context?.close().catch(() => {});
  }

  function fail(error) {
    if (stopped) return;
    report({ error, paused: true, loading: false });
    dispose();
  }

  async function consume() {
    try {
      for await (const { sampleRate, bytes } of readAudio(url, {
        signal: abort.signal,
      })) {
        const pcm = new DataView(
          bytes.buffer,
          bytes.byteOffset,
          bytes.byteLength,
        );
        const blockSamples = Math.floor(sampleRate / 10);
        for (
          let offset = 0;
          offset < bytes.length / 2;
          offset += blockSamples
        ) {
          while (!stopped && nextStart - context.currentTime >= 2)
            await new Promise((resolve) => {
              wake = resolve;
            });
          if (stopped) return;
          const count = Math.min(blockSamples, bytes.length / 2 - offset);
          const buffer = context.createBuffer(1, count, sampleRate);
          const samples = buffer.getChannelData(0);
          for (let index = 0; index < count; index++)
            samples[index] = pcm.getInt16((offset + index) * 2, true) / 32768;
          const node = context.createBufferSource();
          node.buffer = buffer;
          node.connect(gain);
          // A timely next frame must join the preceding one exactly, even
          // when less than the initial preroll remains in the queue.
          const start = Math.max(
            nextStart,
            context.currentTime + (sources.size ? 0 : 0.02),
          );
          const entry = { node, start, duration: count / sampleRate };
          sources.add(entry);
          nextStart = start + entry.duration;
          received += entry.duration;
          node.onended = () => {
            if (stopped) return;
            sources.delete(entry);
            completed += entry.duration;
            node.disconnect();
            wake?.();
            report();
          };
          node.start(start);
          report();
        }
      }
      finished = true;
      while (!stopped && sources.size)
        await new Promise((resolve) => {
          wake = resolve;
        });
      if (!stopped) {
        paused = true;
        report({ ended: true, loading: false });
        dispose();
      }
    } catch (error) {
      fail(error);
    }
  }

  return {
    async play() {
      if (stopped) return;
      const generation = ++request;
      intentPlaying = true;
      try {
        if (!context) {
          context = new AudioContextClass({ latencyHint: 'interactive' });
          gain = context.createGain();
          gain.gain.value = volume;
          gain.connect(context.destination);
          context.onstatechange = () => {
            if (stopped || !started) return;
            if (context.state === 'closed') {
              fail(new Error('Speech audio output closed'));
              return;
            }
            paused = context.state !== 'running' || !intentPlaying;
            report();
          };
        }
        await context.resume();
        if (stopped || generation !== request) return;
        paused = false;
        report();
        if (!started) {
          started = true;
          timer = setInterval(() => report(), 100);
          void consume();
        }
      } catch (error) {
        if (error?.name === 'NotAllowedError') {
          paused = true;
          report({ loading: false });
        } else fail(error);
      }
    },
    pause() {
      if (stopped) return;
      request += 1;
      intentPlaying = false;
      paused = true;
      report({ loading: false });
      void context?.suspend().catch(fail);
    },
    setVolume(value) {
      volume = value;
      if (gain) gain.gain.value = value;
    },
    stop: dispose,
  };
}
