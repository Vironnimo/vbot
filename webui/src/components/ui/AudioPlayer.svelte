<script>
  import Button from './Button.svelte';
  import Banner from './Banner.svelte';
  import Dropdown from '../Dropdown.svelte';
  import { t } from '$lib/i18n.js';
  import { mountHold } from '$lib/mountHold.js';
  import { tooltip } from '$lib/tooltip.js';
  import { createSpeechPlayback } from '$lib/speechPlayback.js';

  let {
    src = '',
    playback = '',
    cancelled = false,
    ariaLabel = t('audio.label'),
    autoplay = false,
    class: className = '',
  } = $props();
  let audio;
  let root = $state();
  let paused = $state(true);
  let loading = $state(true);
  let duration = $state(0);
  let currentTime = $state(0);
  let volume = $state(1);
  let muted = $state(false);
  let rate = $state(1);
  let error = $state('');
  let streaming = $state(false);
  let stream;
  let streamAttached = false;
  // The automatic start is requested once per attached source: withdrawing
  // `autoplay` afterwards neither cancels a pending start nor stops playback.
  let autoplayRequested = false;
  let autoplayAttempted = false;
  let generation = 0;
  let playPending = false;
  const speeds = [0.75, 1, 1.25, 1.5, 2].map((value) => ({
    value,
    label: `${value}×`,
  }));
  let silent = $derived(muted || volume === 0);
  let progress = $derived(
    duration > 0 ? Math.min(100, (currentTime / duration) * 100) : 0,
  );
  let playLabel = $derived(paused ? t('audio.play') : t('audio.pause'));
  let muteLabel = $derived(silent ? t('audio.unmute') : t('audio.mute'));
  let volumePercent = $derived(Math.round((muted ? 0 : volume) * 100));
  // A host that unmounts content scrolled out of view (the Chat timeline)
  // keeps a playing player mounted, so playback survives scrolling away.
  const holdMounted = mountHold();

  $effect(() => {
    if (paused || !root) return undefined;
    return holdMounted(root);
  });

  $effect(() => {
    if (!cancelled) return;
    autoplayAttempted = true;
    stream?.stop();
    stream = null;
    streaming = false;
    pause();
  });

  function timeLabel(value) {
    const seconds = Math.floor(Number.isFinite(value) && value > 0 ? value : 0);
    const minutes = Math.floor(seconds / 60);
    return minutes >= 60
      ? `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`
      : `${minutes}:${String(seconds % 60).padStart(2, '0')}`;
  }

  // The component owns its media resource and pending playback, including a
  // source replacement. Artifact URLs themselves remain caller/server-owned.
  function ownMedia(node, source) {
    audio = node;
    let attached;
    let attachedPlayback;
    function replace({ src: next, playback: nextPlayback }) {
      // Re-rendering the caller with an equal source keeps the playback.
      if (
        next === attached &&
        (!nextPlayback || nextPlayback === attachedPlayback)
      )
        return;
      // The final artifact takes over download/replay without interrupting
      // queued PCM or automatically repeating what has already been heard.
      if (
        streamAttached &&
        (!nextPlayback || nextPlayback === attachedPlayback) &&
        (!attached || next === attached)
      ) {
        attached = next;
        // A blocked automatic start may still be waiting for a browser user
        // gesture. Once the artifact exists, that gesture should play it
        // directly instead of depending on an expiring transient URL.
        if (next && stream && paused && currentTime === 0) {
          stream.stop();
          stream = null;
          streaming = false;
          loading = false;
        }
        if (next) {
          node.src = next;
          node.load();
        }
        return;
      }
      attached = next;
      attachedPlayback = nextPlayback;
      generation += 1;
      playPending = false;
      stream?.stop();
      stream = null;
      // A newly mounted completed row uses its durable artifact. The stream
      // belongs only to a player that saw the in-progress generation.
      const transient = nextPlayback && !next;
      streamAttached = Boolean(transient);
      streaming = Boolean(transient);
      node.pause();
      paused = true;
      loading = true;
      duration = 0;
      currentTime = 0;
      error = '';
      autoplayRequested = autoplay;
      autoplayAttempted = false;
      node.preload = autoplayRequested ? 'auto' : 'metadata';
      if (next) {
        node.src = next;
        node.load();
      }
      node.volume = volume;
      node.muted = muted;
      node.playbackRate = rate;
      if (transient) {
        rate = 1;
        node.playbackRate = 1;
        autoplayAttempted = true;
        const owner = createSpeechPlayback({
          url: nextPlayback,
          onState(state) {
            if (stream !== owner) return;
            const wasPaused = paused;
            paused = state.paused;
            loading = state.loading;
            currentTime = state.currentTime;
            duration = state.duration;
            if (state.error || state.ended) {
              stream = null;
              streaming = false;
              if (state.error) error = t('audio.playFailed');
            } else if (wasPaused && !paused) {
              node.dispatchEvent(new Event('play'));
            }
          },
        });
        stream = owner;
        owner.setVolume(muted ? 0 : volume);
        loading = autoplayRequested;
        if (autoplayRequested) void owner.play();
      }
    }
    function otherPlayback(event) {
      if (
        event.target !== node &&
        event.target.matches?.('.audio-player audio')
      ) {
        autoplayAttempted = true;
        pause();
      }
    }
    document.addEventListener('play', otherPlayback, true);
    replace(source);
    return {
      update: replace,
      destroy() {
        document.removeEventListener('play', otherPlayback, true);
        generation += 1;
        playPending = false;
        stream?.stop();
        stream = null;
        node.pause();
        node.removeAttribute('src');
        node.load();
        audio = null;
      },
    };
  }

  function syncTime() {
    if (streamAttached) return;
    duration =
      Number.isFinite(audio.duration) && audio.duration > 0
        ? audio.duration
        : 0;
    currentTime = Number.isFinite(audio.currentTime) ? audio.currentTime : 0;
  }

  function pause() {
    generation += 1;
    playPending = false;
    if (stream) stream.pause();
    else audio?.pause();
    paused = true;
    loading = false;
  }

  async function play(automatic = false) {
    if (stream) {
      error = '';
      void stream.play();
      return;
    }
    if (!src) return;
    if (!audio || playPending) return;
    const media = audio;
    const request = ++generation;
    playPending = true;
    error = '';
    try {
      if (media.error) media.load();
      if (streamAttached) {
        streamAttached = false;
        media.currentTime = 0;
        syncTime();
      }
      if (media.ended) media.currentTime = 0;
      await media.play();
    } catch (failure) {
      if (request !== generation) return;
      paused = true;
      loading = false;
      // A blocked automatic start leaves an ordinary Play action available.
      if (
        failure?.name !== 'AbortError' &&
        !(automatic && failure?.name === 'NotAllowedError')
      ) {
        error = t('audio.playFailed');
      }
    } finally {
      if (request === generation) playPending = false;
    }
  }

  function togglePlayback() {
    autoplayAttempted = true;
    if (!paused || playPending) pause();
    else void play();
  }

  function ready() {
    if (streamAttached) return;
    syncTime();
    loading = false;
    if (autoplayRequested && !autoplayAttempted) {
      autoplayAttempted = true;
      void play(true);
    }
  }

  function seek(event) {
    if (!audio || !duration) return;
    audio.currentTime = Math.max(
      0,
      Math.min(duration, Number(event.currentTarget.value)),
    );
    currentTime = audio.currentTime;
  }

  function changeVolume(event) {
    audio.volume = Number(event.currentTarget.value);
    audio.muted = false;
    syncVolume();
  }

  function syncVolume() {
    volume = audio.volume;
    muted = audio.muted;
    stream?.setVolume(muted ? 0 : volume);
  }

  function toggleMute() {
    if (silent) {
      if (audio.volume === 0) audio.volume = 1;
      audio.muted = false;
    } else audio.muted = true;
    syncVolume();
  }

  function download() {
    const link = document.createElement('a');
    link.href = src;
    link.download = '';
    link.click();
  }
</script>

<div
  bind:this={root}
  class={`audio-player ${className}`}
  role="group"
  aria-label={ariaLabel}
>
  <audio
    use:ownMedia={{ src, playback }}
    aria-hidden="true"
    onloadedmetadata={() => {
      if (streamAttached) return;
      syncTime();
      loading = false;
    }}
    ondurationchange={syncTime}
    ontimeupdate={syncTime}
    oncanplay={ready}
    onplay={() => {
      paused = false;
    }}
    onplaying={() => {
      if (streamAttached) return;
      paused = false;
      loading = false;
    }}
    onpause={() => {
      if (streamAttached) return;
      paused = true;
      loading = false;
    }}
    onended={() => {
      if (streamAttached) return;
      paused = true;
      loading = false;
      syncTime();
    }}
    onwaiting={() => {
      if (streamAttached) return;
      loading = true;
    }}
    onvolumechange={syncVolume}
    onratechange={() => {
      rate = audio.playbackRate;
    }}
    onerror={() => {
      if (streaming) return;
      pause();
      error = t('audio.loadFailed');
    }}
  ></audio>
  <div class="audio-player__controls">
    <Button
      variant="primary"
      icon
      ariaLabel={playLabel}
      tooltip={playLabel}
      onClick={togglePlayback}
      disabled={!streaming && !src}
    >
      <svg
        width="16"
        height="16"
        viewBox="0 0 24 24"
        fill="currentColor"
        aria-hidden="true"
      >
        {#if paused}<path d="m8 5 11 7-11 7z" />{:else}<path
            d="M6 5h4v14H6zm8 0h4v14h-4z"
          />{/if}
      </svg>
    </Button>
    <div class="audio-player__timeline">
      <input
        class="audio-player__range"
        type="range"
        min="0"
        max={duration || 1}
        step="0.1"
        value={Math.min(currentTime, duration)}
        disabled={streaming || !duration || Boolean(error)}
        aria-label={t('audio.seek')}
        aria-valuetext={t('audio.position', {
          current: timeLabel(currentTime),
          duration: timeLabel(duration),
        })}
        style={`--range-progress: ${progress}%`}
        oninput={seek}
      />
      <div class="audio-player__time">
        <span>{timeLabel(currentTime)}</span>
        {#if loading && !error}<span role="status">{t('audio.loading')}</span
          >{/if}
        <span>{duration ? timeLabel(duration) : '–:––'}</span>
      </div>
    </div>
    <div class="audio-player__options">
      <Dropdown
        value={rate}
        options={speeds}
        ariaLabel={t('audio.speed')}
        triggerTooltip={t('audio.speed')}
        triggerClass="audio-player__speed"
        disabled={streaming}
        onValueChange={(value) => {
          audio.playbackRate = Number(value);
          rate = Number(value);
        }}
      />
      <div class="audio-player__volume">
        <Button
          variant="tertiary"
          icon
          ariaLabel={muteLabel}
          tooltip={muteLabel}
          onClick={toggleMute}
        >
          <svg
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.7"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
          >
            <path d="M11 5 6 9H3v6h3l5 4z" />
            {#if silent}<path d="m16 9 6 6m0-6-6 6" />{:else}<path
                d="M15 8a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14"
              />{/if}
          </svg>
        </Button>
        <input
          class="audio-player__range"
          type="range"
          min="0"
          max="1"
          step="0.05"
          value={muted ? 0 : volume}
          aria-label={t('audio.volume')}
          aria-valuetext={`${volumePercent}%`}
          use:tooltip={{
            rows: [{ label: t('audio.volume'), value: `${volumePercent}%` }],
          }}
          style={`--range-progress: ${muted ? 0 : volume * 100}%`}
          oninput={changeVolume}
        />
      </div>
      <Button
        variant="tertiary"
        icon
        ariaLabel={t('audio.download')}
        tooltip={t('audio.download')}
        onClick={download}
        disabled={!src}
      >
        <svg
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.7"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"
          ><path d="M12 3v12m-4-4 4 4 4-4M5 16v4h14v-4" /></svg
        >
      </Button>
    </div>
  </div>
  {#if error}<Banner variant="warn" role="alert">{error}</Banner>{/if}
</div>

<style>
  .audio-player {
    container-type: inline-size;
    width: 100%;
    max-width: 600px;
    min-width: 0;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    background: var(--surface);
    color: var(--text-hi);
  }
  audio {
    display: none;
  }
  .audio-player__controls {
    display: flex;
    align-items: center;
    gap: var(--space-md);
    padding: 10px 12px;
  }
  .audio-player__timeline {
    flex: 1;
    min-width: 0;
  }
  .audio-player__time {
    display: flex;
    justify-content: space-between;
    gap: var(--space-xs);
    color: var(--text-med);
    font-size: var(--fs-label-sm);
    font-variant-numeric: tabular-nums;
    line-height: 1.4;
  }
  .audio-player__options,
  .audio-player__volume {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
  }
  .audio-player__options {
    flex-shrink: 0;
  }
  .audio-player__volume {
    margin-inline: var(--space-xs);
  }
  .audio-player__volume input {
    width: 48px;
  }
  .audio-player__options :global(.audio-player__speed) {
    flex-shrink: 0;
    min-width: 66px;
  }
  .audio-player__options :global(.audio-player__speed .dropdown-trigger) {
    min-height: 30px;
    padding: 4px 8px;
    background: transparent;
    border-color: transparent;
    font-size: var(--fs-label-sm);
    font-variant-numeric: tabular-nums;
  }
  .audio-player__options :global(.audio-player__speed .dropdown-trigger:hover) {
    background: var(--surface-3);
  }
  .audio-player__range {
    display: block;
    appearance: none;
    width: 100%;
    height: 20px;
    margin: 0;
    padding: 0;
    border: 0;
    border-radius: var(--r-sm);
    background: transparent;
    cursor: pointer;
    /* Position and volume are values, not selections: the filled part uses
       the neutral "on" tone like the shared toggle track. */
    --track-fill: linear-gradient(
      to right,
      var(--text-med) var(--range-progress),
      var(--border-2) var(--range-progress)
    );
  }
  .audio-player__range::-webkit-slider-runnable-track {
    height: 3px;
    border-radius: var(--r-sm);
    background: var(--track-fill);
  }
  .audio-player__range::-moz-range-track {
    height: 3px;
    border-radius: var(--r-sm);
    background: var(--track-fill);
  }
  .audio-player__range::-webkit-slider-thumb {
    appearance: none;
    width: 10px;
    height: 10px;
    margin-top: -3.5px;
    border: 0;
    border-radius: 50%;
    background: var(--text-hi);
  }
  .audio-player__range::-moz-range-thumb {
    width: 10px;
    height: 10px;
    border: 0;
    border-radius: 50%;
    background: var(--text-hi);
  }
  .audio-player__range:focus-visible {
    outline: 1px solid var(--accent);
    outline-offset: 2px;
    box-shadow: var(--focus-ring);
  }
  .audio-player__range:disabled {
    opacity: 0.45;
    cursor: default;
  }
  .audio-player :global(.banner) {
    margin: 0 8px 8px;
  }
  @container (max-width: 440px) {
    .audio-player__controls {
      flex-wrap: wrap;
      gap: var(--space-sm);
    }
    .audio-player__timeline {
      flex-basis: calc(100% - 48px);
    }
    .audio-player__options {
      width: 100%;
      justify-content: flex-end;
      border-top: 1px solid var(--border);
      padding-top: var(--space-xs);
    }
  }
  @media (max-width: 640px), (pointer: coarse) {
    .audio-player__controls :global(.btn-icon),
    .audio-player__options :global(.audio-player__speed .dropdown-trigger) {
      min-width: 40px;
      min-height: 40px;
    }
    .audio-player__range {
      height: 40px;
    }
    .audio-player__timeline {
      flex-basis: calc(100% - 56px);
    }
  }
  @container (max-width: 280px) {
    .audio-player__options {
      flex-wrap: wrap;
      justify-content: space-between;
    }
    .audio-player__volume {
      order: 1;
      width: 100%;
      margin: 0;
    }
    .audio-player__volume input {
      flex: 1;
      width: auto;
      min-width: 0;
    }
  }
</style>
