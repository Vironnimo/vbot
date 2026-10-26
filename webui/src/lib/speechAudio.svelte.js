import { getContext, setContext } from 'svelte';
import { createAudioPlayback } from './audioPlayback.svelte.js';
import { isSpeechPlaybackUrl } from './api.js';
import { speechArtifactFromResult } from './chatTimelinePresentation/messages.js';
import { formatAgentAddress } from './agentAddress.js';

const CONTEXT_KEY = Symbol('speech-audio');
const ORIGIN_KEY = Symbol('speech-origin');
const MAX_IDLE = 128;
const MAX_SEEN = 2000;

const speechAudioKey = (runId, callId) => `${runId}:${callId}`;

// App-scoped speech playback and its pending list. The event feed admits each
// fresh speech call once; Chat players only attach controls to these resources.
export function createSpeechAudio({
  createPlayback = createAudioPlayback,
} = {}) {
  // eslint-disable-next-line svelte/prefer-svelte-reactivity -- resource ownership, not rendered state
  const entries = new Map();
  // eslint-disable-next-line svelte/prefer-svelte-reactivity -- event deduplication, not rendered state
  const seen = new Set();
  // eslint-disable-next-line svelte/prefer-svelte-reactivity -- event deduplication, not rendered state
  const cancelledRuns = new Set();
  let queue = [];
  let active = $state.raw(null);
  let pending = $state(0);
  let epoch = '';
  let destroyed = false;

  function prune() {
    for (const [key, entry] of entries) {
      if (entries.size <= MAX_IDLE) break;
      if (entry.views || entry === active || queue.includes(entry)) continue;
      entry.resource.destroy();
      entries.delete(key);
    }
  }

  // Paused by the user part of the way: a fresh speech takes over, and the
  // paused one stays resumable from its own player.
  function pausedPartway(entry) {
    const { paused, loading, currentTime } = entry.resource.state;
    return paused && !loading && currentTime > 0;
  }

  function advance() {
    if (destroyed || (active && !pausedPartway(active))) return;
    if (active && !queue.length) return;
    active = queue.shift() ?? null;
    pending = queue.length;
    if (active) void active.resource.play(true);
    prune();
  }

  function entryFor(key) {
    const existing = entries.get(key);
    if (existing) return existing;
    const entry = $state({
      key,
      views: 0,
      runId: '',
      agentId: '',
      sessionId: '',
      resource: null,
    });
    entry.resource = createPlayback({
      onPlay() {
        if (destroyed) return;
        // Manual replay can take over from another paused/playing source.
        queue = queue.filter((item) => item !== entry);
        pending = queue.length;
        active = entry;
      },
      onFinished() {
        // Keep a failed background player available for Retry/Stop instead
        // of hiding its error with the next announcement.
        if (active === entry && !entry.resource.state.error) {
          active = null;
          advance();
        }
      },
    });
    entries.set(key, entry);
    return entry;
  }

  function remember(key) {
    seen.add(key);
    if (seen.size > MAX_SEEN) seen.delete(seen.values().next().value);
  }

  function enqueue(entry) {
    if (seen.has(entry.key) || cancelledRuns.has(entry.runId)) return;
    remember(entry.key);
    queue.push(entry);
    pending = queue.length;
    advance();
  }

  function cancelRun(runId) {
    cancelledRuns.add(runId);
    if (cancelledRuns.size > MAX_SEEN)
      cancelledRuns.delete(cancelledRuns.values().next().value);
    queue = queue.filter((entry) => entry.runId !== runId);
    pending = queue.length;
    for (const entry of entries.values()) {
      if (entry.runId === runId) entry.resource.cancel();
    }
  }

  function stop() {
    queue = [];
    pending = 0;
    const playing = active;
    active = null;
    playing?.resource.cancel();
  }

  function reset() {
    stop();
    // Existing Chat controls may still reference a resource until their
    // server-generation owner unmounts. Cancel all before releasing them.
    for (const entry of entries.values()) entry.resource.destroy();
    entries.clear();
    seen.clear();
    cancelledRuns.clear();
  }

  return {
    get active() {
      return active;
    },
    get pending() {
      return pending;
    },
    // origin: the shown Session ({agentId, sessionId}) of the player, for a
    // speech replayed from history that no live event announced.
    acquire(key, origin = null) {
      const entry = entryFor(key);
      entry.views += 1;
      if (origin?.sessionId && !entry.sessionId) {
        entry.agentId = origin.agentId;
        entry.sessionId = origin.sessionId;
      }
      return entry.resource;
    },
    release(key) {
      const entry = entries.get(key);
      if (entry) entry.views = Math.max(0, entry.views - 1);
      prune();
    },
    renameAgent(oldAgentId, newAgentId) {
      for (const entry of entries.values()) {
        if (entry.agentId === oldAgentId) entry.agentId = newAgentId;
      }
    },
    handleServerEvent(event) {
      if (destroyed) return;
      if (event.type === 'connection_ready') {
        if (epoch && event.epoch && epoch !== event.epoch) reset();
        epoch = event.epoch ?? epoch;
        return;
      }
      const payload = event.payload ?? {};
      const runId = payload.run_id;
      if (!runId) return;
      // Only the user's cancellation silences a Run's speech; after a failure
      // or interruption its generated speech still plays to the end.
      if (event.type === 'run_cancelled') {
        cancelRun(runId);
        return;
      }
      const output = payload.output ?? {};
      const kind = payload.run_event_type;
      const callId =
        kind === 'speech_playback' ? output.tool_call_id : output.tool_call?.id;
      if (!callId || cancelledRuns.has(runId)) return;
      const key = speechAudioKey(runId, callId);
      const playback =
        kind === 'speech_playback' && isSpeechPlaybackUrl(output.url)
          ? output.url
          : '';
      const speechResult =
        kind === 'tool_call_result' &&
        output.tool_call?.name === 'generate_speech';
      const artifact = speechResult
        ? speechArtifactFromResult({ payload: output })
        : null;
      if (speechResult && !artifact) {
        queue = queue.filter((entry) => entry.key !== key);
        pending = queue.length;
        remember(key);
        entries.get(key)?.resource.cancel();
        return;
      }
      if (!playback && !artifact) return;
      const entry = entryFor(key);
      entry.runId = runId;
      entry.agentId = formatAgentAddress(payload.agent_id, payload.project_id);
      entry.sessionId = payload.session_id ?? '';
      entry.resource.update({
        src: artifact?.url || entry.resource.state.src,
        playback,
      });
      enqueue(entry);
    },
    stop,
    destroy() {
      destroyed = true;
      reset();
    },
  };
}

// The Session a Chat area shows, so its players can name their speech's origin.
export function provideSpeechOrigin(shownSession) {
  setContext(ORIGIN_KEY, shownSession);
}
export function speechOriginContext() {
  return getContext(ORIGIN_KEY) ?? null;
}

export function provideSpeechAudio(owner) {
  setContext(CONTEXT_KEY, owner);
  return owner;
}
export function speechAudioContext() {
  return getContext(CONTEXT_KEY);
}
