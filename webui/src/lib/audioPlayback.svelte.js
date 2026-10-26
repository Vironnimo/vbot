import { t } from './i18n.js';
import { createSpeechPlayback } from './speechPlayback.js';

const PLAY_EVENT = 'vbot-audio-play';

// A media resource independent of its controls. Ordinary players own one;
// App owns speech resources so changing Session never stops their audio.
export function createAudioPlayback({
  onPlay = () => {},
  onFinished = () => {},
} = {}) {
  const audio = document.createElement('audio');
  const state = $state({
    src: '',
    paused: true,
    loading: true,
    duration: 0,
    currentTime: 0,
    volume: 1,
    muted: false,
    rate: 1,
    error: '',
    streaming: false,
  });
  let attached;
  let attachedPlayback;
  let stream;
  let streamAttached = false;
  let streamSeek = null;
  let autoplayRequested = false;
  let autoplayAttempted = false;
  let generation = 0;
  let playPending = false;
  let destroyed = false;

  function announcePlay() {
    onPlay();
    document.dispatchEvent(new CustomEvent(PLAY_EVENT, { detail: audio }));
  }

  function syncTime() {
    if (streamAttached) return;
    state.duration =
      Number.isFinite(audio.duration) && audio.duration > 0
        ? audio.duration
        : 0;
    state.currentTime = Number.isFinite(audio.currentTime)
      ? audio.currentTime
      : 0;
  }

  function pause() {
    generation += 1;
    playPending = false;
    if (stream) stream.pause();
    else audio.pause();
    state.paused = true;
    state.loading = false;
  }

  function cancel() {
    autoplayAttempted = true;
    stream?.stop();
    stream = null;
    state.streaming = false;
    state.error = '';
    pause();
    onFinished();
  }

  async function play(automatic = false) {
    if (destroyed) return;
    state.error = '';
    if (stream) {
      void stream.play();
      return;
    }
    if (!state.src || playPending) return;
    const request = ++generation;
    playPending = true;
    try {
      if (audio.error) audio.load();
      if (streamAttached) {
        streamAttached = false;
        audio.currentTime = streamSeek ?? 0;
        streamSeek = null;
        syncTime();
      } else if (audio.ended) audio.currentTime = 0;
      await audio.play();
    } catch (failure) {
      if (request !== generation) return;
      state.paused = true;
      state.loading = false;
      if (
        failure?.name !== 'AbortError' &&
        !(automatic && failure?.name === 'NotAllowedError')
      ) {
        state.error = t('audio.playFailed');
        onFinished();
      }
    } finally {
      if (request === generation) playPending = false;
    }
  }

  function ready() {
    if (streamAttached) return;
    syncTime();
    state.loading = false;
    if (autoplayRequested && !autoplayAttempted) {
      autoplayAttempted = true;
      void play(true);
    }
  }

  function syncVolume() {
    state.volume = audio.volume;
    state.muted = audio.muted;
    stream?.setVolume(state.muted ? 0 : state.volume);
  }

  const listeners = {
    loadedmetadata: () => {
      if (streamAttached) return;
      syncTime();
      state.loading = false;
    },
    durationchange: syncTime,
    timeupdate: syncTime,
    canplay: ready,
    play: () => {
      state.paused = false;
      announcePlay();
    },
    playing: () => {
      if (streamAttached) return;
      state.paused = false;
      state.loading = false;
    },
    pause: () => {
      if (streamAttached) return;
      state.paused = true;
      state.loading = false;
    },
    ended: () => {
      if (streamAttached) return;
      state.paused = true;
      state.loading = false;
      syncTime();
      onFinished();
    },
    waiting: () => {
      if (!streamAttached) state.loading = true;
    },
    volumechange: syncVolume,
    ratechange: () => {
      state.rate = audio.playbackRate;
    },
    error: () => {
      if (state.streaming) return;
      pause();
      state.error = t('audio.loadFailed');
      onFinished();
    },
  };
  for (const [name, listener] of Object.entries(listeners))
    audio.addEventListener(name, listener);
  function otherPlayback(event) {
    if (event.detail !== audio) {
      autoplayAttempted = true;
      pause();
    }
  }
  document.addEventListener(PLAY_EVENT, otherPlayback);

  function update({
    src = '',
    playback = '',
    autoplay = false,
    cancelled = false,
  }) {
    if (destroyed) return;
    state.src = src;
    if (cancelled) {
      cancel();
      return;
    }
    if (src === attached && (!playback || playback === attachedPlayback))
      return;
    // Attach the durable artifact without restarting the transient audio.
    if (
      streamAttached &&
      (!playback || playback === attachedPlayback) &&
      (!attached || src === attached)
    ) {
      attached = src;
      if (src && stream && state.paused && state.currentTime === 0) {
        stream.stop();
        stream = null;
        state.streaming = false;
        state.loading = false;
      }
      if (src) {
        audio.src = src;
        audio.load();
      }
      return;
    }
    attached = src;
    attachedPlayback = playback;
    generation += 1;
    playPending = false;
    stream?.stop();
    stream = null;
    const transient = playback && !src;
    streamAttached = Boolean(transient);
    streamSeek = null;
    state.streaming = Boolean(transient);
    audio.pause();
    state.paused = true;
    state.loading = true;
    state.duration = 0;
    state.currentTime = 0;
    state.error = '';
    autoplayRequested = autoplay;
    autoplayAttempted = false;
    audio.preload = autoplayRequested ? 'auto' : 'metadata';
    if (src) {
      audio.src = src;
      audio.load();
    }
    audio.volume = state.volume;
    audio.muted = state.muted;
    audio.playbackRate = state.rate;
    if (!transient) return;
    state.rate = 1;
    audio.playbackRate = 1;
    autoplayAttempted = true;
    const owner = createSpeechPlayback({
      url: playback,
      onState(next) {
        if (stream !== owner) return;
        const wasPaused = state.paused;
        state.paused = next.paused;
        state.loading = next.loading;
        state.currentTime = next.currentTime;
        state.duration = next.duration;
        if (next.error || next.ended) {
          stream = null;
          state.streaming = false;
          if (next.error) state.error = t('audio.playFailed');
          onFinished();
        } else if (wasPaused && !state.paused) announcePlay();
      },
    });
    stream = owner;
    owner.setVolume(state.muted ? 0 : state.volume);
    state.loading = autoplayRequested;
    if (autoplayRequested) void owner.play();
  }

  return {
    audio,
    state,
    update,
    play,
    pause,
    cancel,
    toggle() {
      autoplayAttempted = true;
      if (!state.paused || playPending) pause();
      else void play();
    },
    seek(value) {
      if (!state.duration) return;
      const position = Math.max(0, Math.min(state.duration, Number(value)));
      if (streamAttached) {
        streamSeek = position;
        state.currentTime = position;
      } else {
        audio.currentTime = position;
        state.currentTime = audio.currentTime;
      }
    },
    setVolume(value) {
      audio.volume = Number(value);
      audio.muted = false;
      syncVolume();
    },
    toggleMute() {
      if (state.muted || state.volume === 0) {
        if (audio.volume === 0) audio.volume = 1;
        audio.muted = false;
      } else audio.muted = true;
      syncVolume();
    },
    setRate(value) {
      audio.playbackRate = Number(value);
      state.rate = Number(value);
    },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      generation += 1;
      playPending = false;
      document.removeEventListener(PLAY_EVENT, otherPlayback);
      for (const [name, listener] of Object.entries(listeners))
        audio.removeEventListener(name, listener);
      stream?.stop();
      stream = null;
      audio.pause();
      audio.removeAttribute('src');
      audio.load();
      audio.remove();
    },
  };
}
