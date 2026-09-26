import * as defaultApi from './api.js';
import { createRelayAudio, isRelayAudioFormat } from './liveAudio.js';
import { isPlainObject } from './values.js';

// Live voice accessor controller. The server owns the provider call, delegated
// reasoning and every data operation; this page owns only the microphone and
// speaker media, UI requests the server sends over the call's owner socket,
// and the displayed call state. Media is either WebRTC directly to the
// provider or PCM relayed through the server on the owner socket; the bound
// target decides (`live.status` -> `media`).

const STARTUP_TIMEOUT_MS = 45000;
// Non-trickle ICE: wait for complete gathering, then offer whatever candidates
// exist. The provider answers with its own reachable candidates, so a slow
// local interface must not block the call.
const ICE_GATHERING_TIMEOUT_MS = 5000;
// Stop keeps the socket and peer only to receive the final `closed` frame.
const CLOSE_TIMEOUT_MS = 5000;
const PEER_DISCONNECT_GRACE_MS = 5000;
// The server keeps a call through a short owner-socket loss; reattach within it.
const SOCKET_REATTACH_WINDOW_MS = 8000;
const SOCKET_REATTACH_DELAY_MS = 500;
const CAPTION_LIMIT = 20;
const CAPTION_TEXT_LIMIT = 2000;
const OPEN_VIEWS = new Set(['chat', 'terminals', 'agents', 'projects']);
// The ids that name one item of a view in an open request.
const OPEN_TARGET_IDS = {
  chat: ['agent_id', 'session_id'],
  agents: ['agent_id'],
  projects: ['project_id'],
};
const TERMINAL_VIEW_OPS = new Set([
  'context',
  'refresh',
  'show',
  'maximize',
  'restore',
  'show_group',
]);
const ERROR_CODE_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;
const MICROPHONE_CONSTRAINTS = Object.freeze({
  audio: {
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
  },
});
const MEDIA_WEBRTC = 'webrtc';
const MEDIA_RELAY = 'relay';

class LiveVoiceFailure extends Error {
  constructor(code) {
    super(code);
    this.name = 'LiveVoiceFailure';
    this.code = code;
  }
}

const failure = (code) => new LiveVoiceFailure(code);
const isText = (value) => typeof value === 'string' && value.length > 0;

// UI owners report failures as `{code}` errors or plain `Error('<code>')`.
function uiErrorCode(error) {
  for (const candidate of [error?.code, error?.message]) {
    if (typeof candidate === 'string' && ERROR_CODE_PATTERN.test(candidate))
      return candidate;
  }
  return 'operation_failed';
}

function stopTracks(stream) {
  for (const track of stream?.getTracks?.() ?? []) {
    track.enabled = false;
    track.stop();
  }
}

function boundCaption(text) {
  return text.length > CAPTION_TEXT_LIMIT
    ? text.slice(-CAPTION_TEXT_LIMIT)
    : text;
}

const mediaKind = (status) =>
  status?.media === MEDIA_RELAY ? MEDIA_RELAY : MEDIA_WEBRTC;

function checkStatus(status) {
  if (status?.configured !== true) throw failure('not_configured');
  if (status.usable !== true) throw failure('not_usable');
}

// Relay playback is not a WebRTC track, so its echo is cancelled against the
// device output where the browser supports that ("all", Chrome 141+). Best
// effort: the regular echo cancellation stays when it is unavailable.
async function cancelEchoOfAllOutput(microphone) {
  for (const track of microphone.getAudioTracks?.() ?? []) {
    const supported = track.getCapabilities?.()?.echoCancellation;
    if (!Array.isArray(supported) || !supported.includes('all')) continue;
    try {
      await track.applyConstraints({
        ...MICROPHONE_CONSTRAINTS.audio,
        echoCancellation: 'all',
      });
    } catch {
      // Keep the default echo cancellation.
    }
  }
}

function waitForIceGathering(peer) {
  if (peer.iceGatheringState === 'complete') return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      clearTimeout(timer);
      peer.removeEventListener('icegatheringstatechange', check);
      resolve();
    };
    const check = () => {
      if (peer.iceGatheringState === 'complete') done();
    };
    const timer = setTimeout(done, ICE_GATHERING_TIMEOUT_MS);
    peer.addEventListener('icegatheringstatechange', check);
  });
}

export function createLiveVoiceState() {
  return {
    // off | connecting | live | closing
    phase: 'off',
    callId: null,
    muted: false,
    busy: false,
    activityLabel: null,
    // Bounded transient captions: {role: 'user'|'assistant', text, final}.
    captions: [],
    // Last error code that ended a call; kept until the next start.
    error: '',
    // The call is held (microphone and assistant audio off) for something
    // else that uses the microphone, independent of the user's mute.
    held: false,
    closeReason: null,
    usage: null,
  };
}

// `uiActions` execute server UI requests: `context(guard)`,
// `open({view, agent_id?, session_id?}, guard)` (false when not applied) and
// `terminalView({op, terminal_id?, group_id?}, guard)`. `guard.isCurrent()`
// turns false once the requesting call stops. `onNotice({code, severity})`
// reports errors ('error'/'warn') and call endings the user did not request
// ('info'). An optional `checkMicrophoneAccess()` runs before the microphone
// opens and resolves a notice code that ends the start, or null to continue.
// `wakePhrases()` returns the wake phrases that address other Agents during
// the call; they are sent with each start request.
export function createLiveVoice({
  state,
  api = defaultApi,
  mediaDevices = globalThis.navigator?.mediaDevices,
  checkMicrophoneAccess = null,
  wakePhrases = () => [],
  createPeer = () => new RTCPeerConnection(),
  audio = null,
  createAudio = createRelayAudio,
  uiActions = {},
  onActive = () => {},
  onNotice = () => {},
  now = () => Date.now(),
}) {
  // The current call attempt. Its identity is the generation token: every
  // asynchronous step re-checks it, so Stop or a newer call silences late work.
  let current = null;

  const isCurrent = (call) => call !== null && call === current;
  const notify = (code, severity) => onNotice({ code, severity });

  function schedule(call, callback, delay) {
    const timer = setTimeout(() => {
      call.timers.delete(timer);
      if (isCurrent(call)) callback();
    }, delay);
    call.timers.add(timer);
    return timer;
  }

  function cancel(call, timer) {
    if (!timer) return;
    clearTimeout(timer);
    call.timers.delete(timer);
  }

  function armStartupTimer(call) {
    cancel(call, call.startupTimer);
    call.startupTimer = schedule(
      call,
      () => fail(call, 'connection_timeout'),
      STARTUP_TIMEOUT_MS,
    );
  }

  function releaseRelayAudio(call) {
    const relay = call.relay;
    call.relay = null;
    relay?.close();
  }

  function releaseMedia(call) {
    stopTracks(call.microphone);
    call.microphone = null;
    releaseRelayAudio(call);
    if (call.playing && audio) {
      audio.pause?.();
      audio.srcObject = null;
    }
    call.playing = false;
    if (call.outputMuted && audio) audio.muted = false;
    call.outputMuted = false;
  }

  const isHeld = (call) => call.holds.size > 0;

  // The microphone sends only while neither the user nor a hold mutes it.
  function applyMicrophone(call) {
    const enabled = !state.muted && !isHeld(call);
    for (const track of call.microphone?.getAudioTracks?.() ?? [])
      track.enabled = enabled;
  }

  // WebRTC plays through the audio element; relay audio is dropped on arrival
  // while held (see attachSocket).
  function applyOutput(call) {
    const muted = isHeld(call);
    if (!audio || call.outputMuted === muted) return;
    call.outputMuted = muted;
    audio.muted = muted;
  }

  function releasePeer(call) {
    const { channel, peer } = call;
    call.channel = null;
    call.peer = null;
    channel?.close();
    peer?.close();
  }

  function finish(call) {
    if (!isCurrent(call)) return;
    current = null;
    for (const timer of call.timers) clearTimeout(timer);
    call.timers.clear();
    releaseMedia(call);
    releasePeer(call);
    const { socket } = call;
    call.socket = null;
    socket?.close();
    call.holds.clear();
    Object.assign(state, {
      phase: 'off',
      callId: null,
      muted: false,
      held: false,
      busy: false,
      activityLabel: null,
    });
    if (call.announcedActive) onActive(false);
  }

  // Ends the server call once. The server answers `stopping: false` for a call
  // it no longer holds; then no `closed` frame will follow.
  function requestStop(call) {
    if (call.stopRequested || !call.callId) return;
    call.stopRequested = true;
    const callId = call.callId;
    Promise.resolve()
      .then(() => api.stopLiveCall(callId))
      .then(
        (result) => {
          if (isCurrent(call) && result?.stopping !== true) finish(call);
        },
        () => {
          if (isCurrent(call)) finish(call);
        },
      );
  }

  function fail(call, code) {
    if (!isCurrent(call)) return;
    state.error = code;
    notify(code, 'error');
    requestStop(call);
    finish(call);
  }

  // Microphone and speaker stop at once; socket and peer wait for `closed`,
  // which a lost socket or the close timeout stands in for.
  function beginClosing(call) {
    if (call.closing) return;
    call.closing = true;
    state.phase = 'closing';
    state.busy = false;
    releaseMedia(call);
    cancel(call, call.startupTimer);
    cancel(call, call.disconnectTimer);
    schedule(call, () => applyClosed(call, {}), CLOSE_TIMEOUT_MS);
  }

  function playbackFailed(call, error) {
    if (!isCurrent(call) || call.closing || error?.name === 'AbortError')
      return;
    fail(call, 'playback_blocked');
  }

  function wirePeer(call, peer) {
    peer.addEventListener('track', (event) => {
      if (!isCurrent(call) || call.closing || !audio) return;
      audio.srcObject = event.streams?.[0] ?? new MediaStream([event.track]);
      call.playing = true;
      applyOutput(call);
      let playback;
      try {
        playback = audio.play();
      } catch (error) {
        playbackFailed(call, error);
        return;
      }
      playback?.catch?.((error) => playbackFailed(call, error));
    });
    peer.addEventListener('connectionstatechange', () => {
      if (!isCurrent(call) || call.closing) return;
      const connectionState = peer.connectionState;
      if (connectionState === 'connected') {
        cancel(call, call.disconnectTimer);
        call.disconnectTimer = null;
      } else if (connectionState === 'failed' || connectionState === 'closed') {
        fail(call, call.reachedLive ? 'connection_lost' : 'connection_failed');
      } else if (connectionState === 'disconnected' && !call.disconnectTimer) {
        call.disconnectTimer = schedule(
          call,
          () => fail(call, 'connection_lost'),
          PEER_DISCONNECT_GRACE_MS,
        );
      }
    });
  }

  function attachSocket(call) {
    let connection = null;
    const owns = () => isCurrent(call) && call.socket === connection;
    try {
      connection = api.openLiveCallSocket(call.callId, {
        onEvent: (frame) => {
          if (!owns()) return;
          call.socketLostAt = null;
          handleFrame(frame, call);
        },
        onAudio: (pcm) => {
          if (owns() && !call.closing && !isHeld(call)) call.relay?.play(pcm);
        },
        onClose: (_event, outcome) => {
          if (owns()) socketLost(call, outcome);
        },
      });
    } catch {
      socketLost(call);
      return;
    }
    call.socket = connection;
  }

  // Only a lagging or dropped socket may reattach. The server closes it for
  // good once the call ended, when it no longer knows the call, and when a
  // newer owner socket replaced this one.
  function socketLost(call, outcome = 'lost') {
    call.socket = null;
    if (call.closing || outcome === 'ended') {
      applyClosed(call, {});
      return;
    }
    if (outcome === 'unknown_call' || outcome === 'replaced') {
      fail(call, call.reachedLive ? 'connection_lost' : 'connection_failed');
      return;
    }
    call.socketLostAt ??= now();
    if (now() - call.socketLostAt >= SOCKET_REATTACH_WINDOW_MS) {
      fail(call, call.reachedLive ? 'connection_lost' : 'connection_failed');
      return;
    }
    schedule(call, () => attachSocket(call), SOCKET_REATTACH_DELAY_MS);
  }

  function applyPhase(call, phase) {
    if (phase === 'live') {
      if (call.closing || call.reachedLive) return;
      call.reachedLive = true;
      cancel(call, call.startupTimer);
      state.phase = 'live';
      call.announcedActive = true;
      onActive(true);
    } else if (phase === 'failed') {
      call.failed = true;
      beginClosing(call);
    } else if (phase === 'closing' || phase === 'closed') {
      beginClosing(call);
    }
  }

  // Each caption frame carries the role's whole current turn so far, so it
  // replaces the open turn's text; a final frame also closes that turn.
  function applyCaption(frame) {
    const { role, text } = frame;
    if ((role !== 'user' && role !== 'assistant') || typeof text !== 'string')
      return;
    const final = frame.final === true;
    const captions = state.captions;
    const index = captions.findLastIndex(
      (entry) => entry.role === role && !entry.final,
    );
    let next;
    if (index >= 0) {
      next = captions.toSpliced(index, 1, {
        role,
        text: boundCaption(text || captions[index].text),
        final,
      });
    } else {
      if (!text) return;
      next = [...captions, { role, text: boundCaption(text), final }];
    }
    state.captions = next.slice(-CAPTION_LIMIT);
  }

  function applyError(call, frame) {
    if (call.stopRequested) return;
    const code = isText(frame.code) ? frame.code : 'provider_error';
    if (frame.fatal !== true) {
      notify(code, 'warn');
      return;
    }
    if (call.errorReported) return;
    call.errorReported = true;
    state.error = code;
    notify(code, 'error');
    beginClosing(call);
    requestStop(call);
  }

  function applyClosed(call, frame) {
    state.usage = isPlainObject(frame.usage) ? frame.usage : null;
    state.closeReason = isText(frame.reason) ? frame.reason : null;
    if (!call.stopRequested && !call.errorReported) {
      if (state.closeReason === 'replaced') notify('replaced', 'info');
      else if (call.failed || !call.reachedLive) {
        const code = call.failed ? 'call_failed' : 'connection_failed';
        state.error = code;
        notify(code, 'error');
      } else notify('ended', 'info');
    }
    finish(call);
  }

  function uiAction(name) {
    const action = uiActions[name];
    if (typeof action !== 'function') throw failure('unsupported_action');
    return action;
  }

  // An open request shows a view alone or exactly one item of it: a Chat
  // Session (agent_id and session_id), an Agent page, or a Project page.
  function openTarget(args) {
    const { view } = args;
    const ids = {
      agent_id: args.agent_id ?? undefined,
      session_id: args.session_id ?? undefined,
      project_id: args.project_id ?? undefined,
    };
    const given = Object.keys(ids).filter((key) => ids[key] !== undefined);
    if (!given.length) return { view };
    const expected = OPEN_TARGET_IDS[view] ?? [];
    if (
      given.length !== expected.length ||
      !expected.every((key) => isText(ids[key]))
    )
      throw failure('invalid_arguments');
    return Object.fromEntries([
      ['view', view],
      ...expected.map((key) => [key, ids[key]]),
    ]);
  }

  // Validates one UI request and returns the operation that executes it.
  function uiOperation(actionName, rawArgs, guard) {
    const args = rawArgs ?? {};
    if (!isPlainObject(args)) throw failure('invalid_arguments');
    if (actionName === 'context') {
      const context = uiAction('context');
      return () => context(guard);
    }
    if (actionName === 'open') {
      if (!OPEN_VIEWS.has(args.view)) throw failure('invalid_view');
      const target = openTarget(args);
      const open = uiAction('open');
      return async () => ({ applied: (await open(target, guard)) !== false });
    }
    if (actionName === 'terminal_view') {
      const { op } = args;
      if (!TERMINAL_VIEW_OPS.has(op)) throw failure('invalid_arguments');
      const target = { op };
      if (op === 'show' || op === 'maximize') {
        if (!isText(args.terminal_id)) throw failure('invalid_arguments');
        target.terminal_id = args.terminal_id;
      } else if (op === 'show_group') {
        if (!isText(args.group_id)) throw failure('invalid_arguments');
        target.group_id = args.group_id;
      }
      const terminalView = uiAction('terminalView');
      return () => terminalView(target, guard);
    }
    throw failure('unsupported_action');
  }

  function handleUiRequest(call, frame) {
    const requestId = frame.request_id;
    if (!isText(requestId) || call.requests.has(requestId)) return;
    call.requests.add(requestId);
    const answer = (outcome) => {
      // A stopped call has no server-side request left to answer.
      if (!isCurrent(call)) return;
      // Best effort: an unanswered request times out on the server, which
      // tells the voice Model the action's outcome is unknown.
      Promise.resolve()
        .then(() => api.sendLiveUiResult(call.callId, requestId, outcome))
        .catch(() => {});
    };
    if (call.closing) {
      answer({ error: 'call_closing' });
      return;
    }
    let operation;
    try {
      operation = uiOperation(frame.action, frame.args, {
        isCurrent: () => isCurrent(call) && !call.closing,
      });
    } catch (error) {
      answer({ error: uiErrorCode(error) });
      return;
    }
    Promise.resolve()
      .then(operation)
      .then(
        (result) => answer({ result: isPlainObject(result) ? result : {} }),
        (error) => {
          if (!isCurrent(call)) return;
          answer({ error: uiErrorCode(error) });
          if (!call.closing) notify('ui_action_failed', 'warn');
        },
      );
  }

  function handleFrame(frame, call = current) {
    if (!isCurrent(call) || !isPlainObject(frame)) return;
    switch (frame.type) {
      case 'state':
        applyPhase(call, frame.phase);
        break;
      case 'caption':
        applyCaption(frame);
        break;
      case 'activity':
        state.busy = frame.busy === true && !call.closing;
        state.activityLabel = isText(frame.label) ? frame.label : null;
        break;
      case 'playback_clear':
        // The user talks over the assistant: drop its queued speech at once.
        call.relay?.clear();
        break;
      case 'error':
        applyError(call, frame);
        break;
      case 'closed':
        applyClosed(call, frame);
        break;
      case 'ui_request':
        handleUiRequest(call, frame);
        break;
      default:
        // Frames added by newer servers are ignored.
        break;
    }
  }

  async function openMicrophone() {
    if (typeof mediaDevices?.getUserMedia !== 'function')
      throw failure('microphone_unavailable');
    try {
      return await mediaDevices.getUserMedia(MICROPHONE_CONSTRAINTS);
    } catch (error) {
      throw failure(
        error?.name === 'NotAllowedError' || error?.name === 'SecurityError'
          ? 'microphone_denied'
          : 'microphone_unavailable',
      );
    }
  }

  // WebRTC media: the offer goes to the server, the provider's answer comes
  // back with the call.
  async function requestWebrtcCall(call) {
    const peer = createPeer();
    call.peer = peer;
    wirePeer(call, peer);
    for (const track of call.microphone.getAudioTracks()) {
      peer.addTrack(track, call.microphone);
    }
    // The provider expects this channel in the offer. Its events duplicate
    // what the server already receives, so the page ignores them.
    call.channel = peer.createDataChannel('oai-events');
    const offer = await peer.createOffer();
    if (!isCurrent(call)) return null;
    await peer.setLocalDescription(offer);
    if (!isCurrent(call)) return null;
    await waitForIceGathering(peer);
    if (!isCurrent(call)) return null;
    return api.startLiveCall({
      media: MEDIA_WEBRTC,
      sdp: peer.localDescription.sdp,
      wakePhrases: currentWakePhrases(),
    });
  }

  async function connectWebrtc(call, media) {
    if (media?.type !== MEDIA_WEBRTC || !isText(media.sdp))
      throw failure('connection_failed');
    await call.peer.setRemoteDescription({ type: 'answer', sdp: media.sdp });
  }

  // Relay media: capture and playback run before the call exists, so an
  // unsupported browser or blocked audio fails without a provider call.
  async function requestRelayCall(call) {
    await cancelEchoOfAllOutput(call.microphone);
    if (!isCurrent(call)) return null;
    let relay;
    try {
      relay = await createAudio({
        microphone: call.microphone,
        onFrame: (pcm) => sendAudio(call, pcm),
      });
    } catch (error) {
      throw failure(isText(error?.code) ? error.code : 'audio_unsupported');
    }
    if (!isCurrent(call)) {
      relay.close();
      return null;
    }
    call.relay = relay;
    return api.startLiveCall({
      media: MEDIA_RELAY,
      wakePhrases: currentWakePhrases(),
    });
  }

  function connectRelay(_call, media) {
    if (media?.type !== MEDIA_RELAY || !isRelayAudioFormat(media.audio))
      throw failure('connection_failed');
  }

  // Microphone audio flows only while the call is live; the server drops
  // earlier audio anyway.
  function sendAudio(call, pcm) {
    if (!isCurrent(call) || !call.reachedLive || call.closing) return;
    call.socket?.sendAudio?.(pcm);
  }

  function currentWakePhrases() {
    try {
      const phrases = wakePhrases();
      return Array.isArray(phrases) ? phrases : [];
    } catch {
      return [];
    }
  }

  // An access check that fails does not block the call.
  async function microphoneAccessNotice() {
    if (typeof checkMicrophoneAccess !== 'function') return null;
    try {
      return (await checkMicrophoneAccess()) || null;
    } catch {
      return null;
    }
  }

  function abandonLateCall(result) {
    if (!isText(result?.call_id)) return;
    Promise.resolve()
      .then(() => api.stopLiveCall(result.call_id))
      .catch(() => {});
  }

  async function start() {
    if (current) return;
    const call = {
      callId: null,
      microphone: null,
      peer: null,
      channel: null,
      socket: null,
      relay: null,
      timers: new Set(),
      requests: new Set(),
      startupTimer: null,
      disconnectTimer: null,
      socketLostAt: null,
      closing: false,
      stopRequested: false,
      errorReported: false,
      failed: false,
      reachedLive: false,
      announcedActive: false,
      playing: false,
      outputMuted: false,
      // Hold count per reason; see hold().
      holds: new Map(),
    };
    current = call;
    Object.assign(state, createLiveVoiceState(), { phase: 'connecting' });
    armStartupTimer(call);
    try {
      let status = await api.getLiveVoiceStatus();
      if (!isCurrent(call)) return;
      checkStatus(status);

      const blocked = await microphoneAccessNotice();
      if (!isCurrent(call)) return;
      if (blocked) throw failure(blocked);
      const microphone = await openMicrophone();
      if (!isCurrent(call)) {
        stopTracks(microphone);
        return;
      }
      call.microphone = microphone;
      // The permission prompt is user time; bound only the connection from here.
      armStartupTimer(call);
      applyMicrophone(call);
      for (const track of microphone.getAudioTracks()) {
        track.addEventListener('ended', () => {
          if (isCurrent(call) && !call.closing)
            fail(call, 'microphone_unavailable');
        });
      }

      // The binding may change between status and start; the server then
      // answers `media_mismatch` and the page retries once with fresh status.
      for (let attempt = 1; ; attempt += 1) {
        const media = mediaKind(status);
        const result = await (media === MEDIA_RELAY
          ? requestRelayCall(call)
          : requestWebrtcCall(call));
        if (!isCurrent(call)) {
          abandonLateCall(result);
          return;
        }
        if (result?.error === 'media_mismatch' && attempt === 1) {
          releasePeer(call);
          releaseRelayAudio(call);
          status = await api.getLiveVoiceStatus();
          if (!isCurrent(call)) return;
          checkStatus(status);
          continue;
        }
        if (result?.error)
          throw failure(isText(result.error) ? result.error : 'provider_error');
        if (!isText(result?.call_id)) throw failure('connection_failed');
        call.callId = result.call_id;
        state.callId = call.callId;
        // Attach immediately: the server buffers call updates only briefly.
        attachSocket(call);
        await (media === MEDIA_RELAY
          ? connectRelay(call, result.media)
          : connectWebrtc(call, result.media));
        return;
      }
    } catch (error) {
      fail(
        call,
        error instanceof LiveVoiceFailure ? error.code : 'connection_failed',
      );
    }
  }

  function stop() {
    const call = current;
    if (!call) return;
    // Still starting: a call created later is stopped when its start returns.
    if (!call.callId) {
      finish(call);
      return;
    }
    beginClosing(call);
    requestStop(call);
  }

  function mute(muted = !state.muted) {
    const call = current;
    if (!call || call.closing) return;
    state.muted = muted === true;
    applyMicrophone(call);
  }

  // Hold the running call for `reason`: the microphone stops sending and the
  // assistant goes silent (queued relay audio is dropped) until every hold of
  // every reason is released. Holds count per reason, are independent of the
  // user's mute and end with the call. Returns whether the hold was taken.
  function hold(reason) {
    const call = current;
    if (!call || call.closing || !isText(reason)) return false;
    const wasHeld = isHeld(call);
    call.holds.set(reason, (call.holds.get(reason) ?? 0) + 1);
    if (!wasHeld) {
      state.held = true;
      applyMicrophone(call);
      call.relay?.clear();
      applyOutput(call);
    }
    return true;
  }

  function release(reason) {
    const call = current;
    const count = call?.holds.get(reason);
    if (!count) return;
    if (count > 1) {
      call.holds.set(reason, count - 1);
      return;
    }
    call.holds.delete(reason);
    if (isHeld(call)) return;
    state.held = false;
    applyMicrophone(call);
    applyOutput(call);
  }

  // Whether the current call is held for `reason`, or for any reason.
  function held(reason) {
    const call = current;
    if (!call) return false;
    return reason === undefined ? isHeld(call) : call.holds.has(reason);
  }

  function destroy() {
    const call = current;
    if (!call) return;
    requestStop(call);
    finish(call);
  }

  return {
    start,
    stop,
    mute,
    hold,
    release,
    held,
    active: () => state.phase === 'live',
    destroy,
    handleFrame: (frame) => handleFrame(frame),
  };
}
