import * as defaultApi from './api.js';

const APP_FIELDS = {
  context: [],
  sessions: ['agent_id'],
  read: ['agent_id', 'session_id'],
  open: ['view', 'agent_id', 'session_id'],
  send: ['agent_id', 'session_id', 'text'],
};
const TERMINAL_FIELDS = {
  list: [],
  start: ['program', 'count', 'workdir', 'name', 'group_id'],
  read: ['terminal_id'],
  input: ['terminal_id', 'text', 'submit', 'key'],
  show: ['terminal_id'],
  maximize: ['terminal_id'],
  restore: [],
  reorder: ['group_id', 'order'],
  create_group: ['name'],
};
const KEYS = {
  enter: '\r',
  escape: '\u001b',
  tab: '\t',
  up: '\u001b[A',
  down: '\u001b[B',
  left: '\u001b[D',
  right: '\u001b[C',
  'ctrl-c': '\u0003',
};
const fail = (code) => {
  throw Object.assign(new Error(code), { code });
};
const required = (value) =>
  typeof value === 'string' && value.trim()
    ? value
    : fail('missing_target_or_text');
const codingTerminal = (item) =>
  /^(codex|claude)(\.(exe|cmd|bat))?$/i.test(
    String(item.launch_command || item.command || '')
      .split(/[\\/]/)
      .pop(),
  );
const terminalSummary = (item) =>
  Object.fromEntries(
    [
      'terminal_id',
      'group_id',
      'name',
      'state',
      'command',
      'launch_command',
      'workdir',
      'screen_revision',
      'exit_code',
    ]
      .filter((key) => item[key] !== undefined)
      .map((key) => [
        key,
        typeof item[key] === 'string' ? item[key].slice(0, 1000) : item[key],
      ]),
  );

function recentMessages(messages) {
  let remaining = 8000;
  const result = [];
  for (const message of messages.toReversed()) {
    if (
      !['user', 'assistant', 'error'].includes(message.role) ||
      typeof message.content !== 'string'
    )
      continue;
    const content = message.content.slice(-remaining);
    result.unshift({
      id: message.id,
      role: message.role,
      content,
      truncated: content.length < message.content.length,
    });
    remaining -= content.length;
    if (!remaining) break;
  }
  return result;
}

export function createLiveActions({
  api = defaultApi,
  getContext,
  navigate,
  terminalView,
  isActive = () => true,
}) {
  const sessions = new Set();
  const sessionKey = (a, s) => JSON.stringify([a, s]);
  const ensureActive = (guard) => {
    if (!isActive() || (guard && !guard())) fail('voice_stopped');
  };
  function validate(name, args) {
    if (!args || typeof args !== 'object' || Array.isArray(args))
      fail('invalid_arguments');
    const fields = (
      name === 'vbot_app'
        ? APP_FIELDS
        : name === 'vbot_terminal'
          ? TERMINAL_FIELDS
          : {}
    )[args.action];
    if (
      !fields ||
      Object.keys(args).some((key) => key !== 'action' && !fields.includes(key))
    )
      fail('invalid_arguments');
  }
  async function chatTarget(args, guard) {
    const agent = required(args.agent_id);
    const session = required(args.session_id);
    if (!sessions.has(sessionKey(agent, session))) {
      // Validate exact addressing with the authoritative history endpoint.
      await api.loadChatHistory({
        agent_id: agent,
        session_id: session,
        limit: 1,
      });
      sessions.add(sessionKey(agent, session));
    }
    ensureActive(guard);
    return { agent_id: agent, session_id: session };
  }
  async function appAction(args, guard) {
    if (args.action === 'context') return getContext();
    if (args.action === 'sessions') {
      const agent = required(args.agent_id);
      const result = await api.listSessions(agent, { limit: 30 });
      return result;
    }
    if (args.action === 'open') {
      if (!['chat', 'terminals'].includes(args.view)) fail('invalid_view');
      let target = {};
      if (args.agent_id || args.session_id) {
        if (args.view !== 'chat') fail('invalid_arguments');
        target = await chatTarget(args, guard);
      }
      ensureActive(guard);
      if ((await navigate(args.view, target)) === false)
        fail('navigation_not_applied');
      return { view: args.view, ...target };
    }
    const target = await chatTarget(args, guard);
    if (args.action === 'read') {
      const history = await api.loadChatHistory({ ...target, limit: 20 });
      return {
        ...target,
        messages: recentMessages(history.messages || []),
      };
    }
    const text = required(args.text);
    if (text.length > 16000) fail('text_too_long');
    ensureActive(guard);
    return api.startChatRun({
      ...target,
      content: text,
      input_origin: 'speech_transcription',
    });
  }
  async function terminalAction(args, guard) {
    if (args.action === 'create_group') {
      const name = required(args.name);
      if (name.length > 80) fail('invalid_name');
      return api.createTerminalGroup(name);
    }
    const catalog = await api.listTerminals();
    ensureActive(guard);
    if (args.action === 'list')
      return {
        terminals: (catalog.terminals || []).slice(0, 40).map(terminalSummary),
        groups: (catalog.groups || []).slice(0, 40),
        truncated:
          (catalog.terminals || []).length > 40 ||
          (catalog.groups || []).length > 40,
        layout: await terminalView('context'),
      };
    if (args.action === 'restore') return terminalView('restore');
    if (args.action === 'reorder') {
      const group = (catalog.groups || []).find(
        (g) => g.group_id === args.group_id,
      );
      const members = (catalog.terminals || [])
        .filter((t) => t.group_id === args.group_id)
        .map((t) => t.terminal_id);
      if (
        !group ||
        !['user', 'agent'].includes(group.kind) ||
        !Array.isArray(args.order) ||
        args.order.length !== members.length ||
        new Set(args.order).size !== members.length ||
        args.order.some((id) => !members.includes(id))
      )
        fail('invalid_order');
      const result = await api.setTerminalGroupOrder(
        group.group_id,
        args.order,
      );
      try {
        ensureActive(guard);
        await terminalView('refresh');
        return result;
      } catch {
        return { ...result, layout_error: 'refresh_failed' };
      }
    }
    if (args.action === 'start') {
      if (!['codex', 'claude'].includes(args.program))
        fail('unsupported_program');
      const count = args.count ?? 1;
      if (!Number.isInteger(count) || count < 1 || count > 4)
        fail('invalid_count');
      const workdir = required(args.workdir);
      if (
        args.name !== undefined &&
        (typeof args.name !== 'string' || args.name.length > 80)
      )
        fail('invalid_name');
      let groupId = args.group_id;
      if (
        groupId &&
        !(catalog.groups || []).some(
          (g) => g.group_id === groupId && ['user', 'agent'].includes(g.kind),
        )
      )
        fail('group_not_found');
      const completed = [];
      try {
        if (!groupId) {
          const name = args.program === 'codex' ? 'Codex' : 'Claude Code';
          groupId = (catalog.groups || []).find(
            (g) =>
              g.name?.toLowerCase() === name.toLowerCase() &&
              ['user', 'agent'].includes(g.kind),
          )?.group_id;
          if (!groupId)
            groupId = (await api.createTerminalGroup(name)).group.group_id;
        }
        for (let i = 0; i < count; i += 1) {
          ensureActive(guard);
          const result = await api.startTerminal({
            command: args.program,
            workdir,
            group_id: groupId,
            ...(args.name ? { name: args.name } : {}),
          });
          completed.push(terminalSummary(result.terminal));
        }
      } catch (error) {
        return {
          ok: false,
          completed,
          group_id: groupId,
          error: {
            code: error.code || 'operation_failed',
            delivery_uncertain: true,
          },
        };
      }
      try {
        ensureActive(guard);
        const layout = await terminalView('show', {
          terminal_id: completed[0].terminal_id,
        });
        return { completed, layout };
      } catch {
        return { completed, layout_error: 'navigation_not_applied' };
      }
    }
    const terminal = (catalog.terminals || []).find(
      (t) => t.terminal_id === args.terminal_id,
    );
    if (!terminal) fail('terminal_not_found');
    if (['show', 'maximize'].includes(args.action))
      return terminalView(args.action, args);
    if (!codingTerminal(terminal)) fail('not_a_coding_terminal');
    if (args.action === 'read') {
      const snapshot = await api.readTerminal(terminal.terminal_id);
      return {
        terminal: terminalSummary(snapshot.terminal),
        screen: snapshot.screen.slice(-8000),
        truncated: snapshot.screen.length > 8000,
      };
    }
    if (
      args.key !== undefined &&
      (args.text !== undefined ||
        args.submit !== undefined ||
        !Object.hasOwn(KEYS, args.key))
    )
      fail('invalid_input');
    if (args.submit !== undefined && typeof args.submit !== 'boolean')
      fail('invalid_input');
    const snapshot = await api.readTerminal(terminal.terminal_id);
    ensureActive(guard);
    let data;
    if (args.key !== undefined) data = KEYS[args.key];
    else {
      const text = required(args.text);
      if (
        text.length > 16000 ||
        [...text].some(
          (char) =>
            char.charCodeAt(0) < 32 && !['\t', '\r', '\n'].includes(char),
        )
      )
        fail('invalid_input');
      data =
        snapshot.bracketed_paste && /[\r\n]/.test(text)
          ? `\u001b[200~${text}\u001b[201~`
          : text;
      if (args.submit !== false) data += '\r';
    }
    const result = await api.sendTerminalInput(terminal.terminal_id, data, {
      expectedScreenRevision: snapshot.terminal.screen_revision,
    });
    return { terminal: terminalSummary(result.terminal) };
  }
  return async (name, args, guard) => {
    ensureActive(guard);
    validate(name, args);
    return name === 'vbot_app'
      ? appAction(args, guard)
      : terminalAction(args, guard);
  };
}

export function createLiveVoiceState() {
  return {
    phase: 'off',
    error: '',
    transcript: [],
    actions: [],
    updates: [],
    muted: false,
    playbackBlocked: false,
    usage: null,
    finalized: false,
  };
}

// A single mounted accessor owns execution. Duplicate upstream events never replay
// a function, and a replaced connection cannot deliver a late result to its successor.
export function createLiveVoice({
  state,
  execute,
  api = defaultApi,
  mediaDevices = globalThis.navigator?.mediaDevices,
  createPeer = () => new RTCPeerConnection(),
  audio,
  onActive = () => {},
  now = Date.now,
}) {
  let generation = 0;
  let peer = null;
  let channel = null;
  let microphone = null;
  let startupTimer;
  let closeTimer;
  let responses = new Map();
  let seenResponses = new Set();
  let calls = new Set();
  let announced = new Set();
  let notices = [];
  let noticePending = false;
  let pending = Promise.resolve();
  let busy = 0;
  let startedAt = 0;
  const active = () => state.phase === 'listening';
  const send = (event) => {
    if (channel?.readyState !== 'open') fail('connection_lost');
    channel.send(JSON.stringify(event));
  };
  function cleanup() {
    generation += 1;
    clearTimeout(startupTimer);
    clearTimeout(closeTimer);
    const oldChannel = channel;
    const oldPeer = peer;
    channel = null;
    peer = null;
    for (const track of microphone?.getTracks() || []) track.stop();
    microphone = null;
    oldChannel?.close();
    oldPeer?.close();
    if (audio?.srcObject) {
      audio.pause();
      audio.srcObject = null;
    }
    onActive(false);
  }
  function failed(code) {
    cleanup();
    state.phase = 'error';
    state.error = code;
  }
  function caption(role, delta) {
    if (typeof delta !== 'string' || !delta) return;
    const last = state.transcript.at(-1);
    if (last?.role === role)
      state.transcript = [
        ...state.transcript.slice(0, -1),
        { role, text: (last.text + delta).slice(-4000) },
      ];
    else
      state.transcript = [
        ...state.transcript,
        { role, text: delta.slice(-4000) },
      ].slice(-40);
  }
  function flushNotices() {
    if (!active() || busy || noticePending || !notices.length) return;
    noticePending = true;
    const notice = notices.shift();
    const message = notice.excerpt?.slice(-6000) || '';
    const parts = Math.ceil(message.length / 180);
    for (let index = 0; index < parts; index += 1) {
      send({
        type: 'session.thinking.append',
        delegation_id: null,
        content: JSON.stringify({
          kind: 'agent_message',
          agent_id: notice.agent_id,
          session_id: notice.session_id,
          part: index + 1,
          parts,
          text: message.slice(index * 180, (index + 1) * 180),
        }),
      });
    }
    // <= 500 tokens even for dense Unicode excerpts; full content stays in Chat.
    const content = JSON.stringify({
      kind: notice.kind,
      agent_id: notice.agent_id,
      session_id: notice.session_id,
      excerpt: notice.excerpt?.slice(-180),
      truncated: Boolean(notice.excerpt?.length > 180),
    });
    send({ type: 'session.commentary.append', delegation_id: null, content });
  }
  function responseEvent(event, token) {
    const nested = event.event;
    if (!nested || !event.delegation_id) return;
    if (nested.type === 'response.created') {
      const id = nested.response?.id;
      if (id && !seenResponses.has(id)) {
        if (seenResponses.size >= 1000) {
          failed('conversation_limit');
          return;
        }
        seenResponses.add(id);
        responses.set(event.delegation_id, { id, calls: [], done: false });
        busy += 1;
      }
    } else if (
      nested.type === 'response.output_item.done' &&
      nested.item?.type === 'function_call'
    ) {
      const response = responses.get(event.delegation_id);
      const item = nested.item;
      if (response && item.call_id && !calls.has(item.call_id)) {
        if (calls.size >= 1000) {
          failed('conversation_limit');
          return;
        }
        calls.add(item.call_id);
        response.calls.push(item);
      }
    } else if (
      [
        'response.completed',
        'response.failed',
        'response.incomplete',
        'response.cancelled',
      ].includes(nested.type)
    ) {
      const response = responses.get(event.delegation_id);
      if (
        !response ||
        response.done ||
        (nested.response?.id && response.id !== nested.response.id)
      )
        return;
      response.done = true;
      pending = pending
        .then(async () => {
          try {
            if (token !== generation || !active()) return;
            if (nested.type !== 'response.completed') {
              state.error = 'backend_failed';
              return;
            }
            for (const item of response.calls) {
              if (token !== generation || !active()) return;
              let output;
              try {
                output = await execute(
                  item.name,
                  JSON.parse(item.arguments),
                  () => token === generation && active(),
                );
              } catch (error) {
                output = {
                  ok: false,
                  error: {
                    code: error.code || 'operation_failed',
                    delivery_uncertain: true,
                  },
                };
              }
              if (token !== generation || !active()) return;
              state.actions = [
                ...state.actions,
                { name: item.name, ok: output?.ok !== false, at: now() },
              ].slice(-20);
              if (token !== generation || !active()) return;
              send({
                type: 'response.item.create',
                item: {
                  type: 'function_call_output',
                  call_id: item.call_id,
                  output: JSON.stringify(output),
                },
              });
            }
            if (response.calls.length && token === generation && active())
              send({ type: 'response.create' });
          } finally {
            if (token === generation) {
              busy = Math.max(0, busy - 1);
              flushNotices();
            }
          }
        })
        .catch(() => {
          if (token === generation) failed('connection_lost');
        });
    }
  }
  function handleEvent(event, token = generation) {
    if (token !== generation) return;
    switch (event?.type) {
      case 'session.started':
        if (state.phase !== 'connecting') return;
        clearTimeout(startupTimer);
        state.phase = 'listening';
        onActive(true);
        flushNotices();
        break;
      case 'session.closed':
        state.usage = event.usage ?? null;
        state.finalized = true;
        cleanup();
        state.phase = 'off';
        break;
      case 'session.input_transcript.delta':
        caption('user', event.delta);
        break;
      case 'session.output_transcript.delta':
        caption('assistant', event.delta);
        break;
      case 'response.event':
        responseEvent(event, token);
        break;
      case 'session.commentary.appended':
        noticePending = false;
        flushNotices();
        break;
      case 'error':
        failed('provider_event_error');
        break;
      default:
        break;
    }
  }
  async function start() {
    if (['connecting', 'listening', 'closing'].includes(state.phase)) return;
    cleanup();
    const token = generation;
    Object.assign(state, createLiveVoiceState(), { phase: 'connecting' });
    responses = new Map();
    seenResponses = new Set();
    calls = new Set();
    announced = new Set();
    notices = [];
    noticePending = false;
    busy = 0;
    pending = Promise.resolve();
    startedAt = now();
    startupTimer = setTimeout(() => {
      if (token === generation) failed('connection_timeout');
    }, 45000);
    try {
      const status = await api.getLiveVoiceStatus();
      if (token !== generation) return;
      if (!status.configured) fail('api_key_required');
      if (!mediaDevices?.getUserMedia) fail('microphone_unavailable');
      const stream = await mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      if (token !== generation) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      microphone = stream;
      peer = createPeer();
      const connection = peer;
      connection.addEventListener('track', (event) => {
        if (token !== generation || !audio) return;
        audio.srcObject = new MediaStream([event.track]);
        void audio.play().catch(() => {
          if (token === generation) state.playbackBlocked = true;
        });
      });
      connection.addEventListener('connectionstatechange', () => {
        if (
          token === generation &&
          ['failed', 'disconnected', 'closed'].includes(
            connection.connectionState,
          )
        )
          failed('connection_lost');
      });
      for (const track of stream.getAudioTracks()) {
        connection.addTrack(track, stream);
        track.addEventListener('ended', () => {
          if (token === generation) failed('microphone_unavailable');
        });
      }
      channel = connection.createDataChannel('oai-events');
      channel.addEventListener('message', ({ data }) => {
        if (token !== generation) return;
        try {
          handleEvent(JSON.parse(data), token);
        } catch {
          failed('invalid_event');
        }
      });
      channel.addEventListener('close', () => {
        if (token === generation) failed('connection_lost');
      });
      channel.addEventListener('error', () => {
        if (token === generation) failed('connection_lost');
      });
      const offer = await connection.createOffer();
      if (token !== generation) return;
      await connection.setLocalDescription(offer);
      await waitForIce(connection);
      if (token !== generation) return;
      const result = await api.createLiveVoiceSession(
        connection.localDescription.sdp,
      );
      if (token !== generation) return;
      if (result.error) fail(result.error);
      await connection.setRemoteDescription({
        type: 'answer',
        sdp: result.transport.sdp,
      });
    } catch (error) {
      if (token === generation)
        failed(
          error.code ||
            (error.name === 'NotAllowedError'
              ? 'microphone_denied'
              : 'connection_failed'),
        );
    }
  }
  function stop() {
    if (state.phase !== 'listening') {
      cleanup();
      state.phase = 'off';
      return;
    }
    state.phase = 'closing';
    // Stop collecting user speech immediately, retain transport for final usage.
    for (const track of microphone?.getAudioTracks() || [])
      track.enabled = false;
    try {
      send({ type: 'session.close' });
    } catch {
      failed('finalization_incomplete');
      return;
    }
    closeTimer = setTimeout(() => failed('finalization_incomplete'), 15000);
  }
  function mute() {
    state.muted = !state.muted;
    for (const track of microphone?.getAudioTracks() || [])
      track.enabled = !state.muted;
  }
  async function notifyRuns(events) {
    if (!active()) return;
    const token = generation;
    for (const event of events) {
      if (
        !['run_completed', 'run_failed', 'run_interrupted'].includes(event.type)
      )
        continue;
      const payload = event.payload || {};
      if (
        event.contributes_to_agent_activity === false ||
        payload.contributes_to_agent_activity === false
      )
        continue;
      const id = event.run_id || payload.run_id;
      if (!id || announced.has(id)) continue;
      const timestamp = Date.parse(
        payload.run_event_timestamp ||
          event.timestamp ||
          payload.completed_at ||
          '',
      );
      if (Number.isFinite(timestamp) && timestamp < startedAt) {
        announced.add(id);
        continue;
      }
      const rawAgent = event.agent_id || payload.agent_id;
      const project = event.project_id || payload.project_id;
      const agent =
        project && !rawAgent?.includes('@')
          ? `${rawAgent}@${project}`
          : rawAgent;
      const session = event.session_id || payload.session_id;
      if (!agent || !session) continue;
      announced.add(id);
      if (announced.size > 2000) {
        failed('conversation_limit');
        return;
      }
      try {
        const result = await api.loadChatRunResult({
          agent_id: agent,
          session_id: session,
          run_id: id,
        });
        if (token !== generation || !active()) return;
        const notice = {
          kind: event.type,
          run_id: id,
          agent_id: agent,
          session_id: session,
          excerpt: result.content || '',
          truncated: result.truncated,
        };
        state.updates = [...state.updates, notice].slice(-30);
        notices.push(notice);
        flushNotices();
      } catch {
        if (token === generation) state.error = 'notification_failed';
      }
    }
  }
  function seedRuns(events) {
    for (const event of events) {
      const id = event.run_id || event.payload?.run_id;
      if (
        id &&
        [
          'run_completed',
          'run_failed',
          'run_interrupted',
          'run_cancelled',
        ].includes(event.type)
      )
        announced.add(id);
    }
  }
  return {
    start,
    stop,
    mute,
    active,
    notifyRuns,
    seedRuns,
    handleEvent,
    flush: () => pending,
    destroy: cleanup,
  };
}

function waitForIce(peer) {
  if (peer.iceGatheringState === 'complete') return Promise.resolve();
  return new Promise((resolve, reject) => {
    const finish = () => {
      if (peer.iceGatheringState !== 'complete') return;
      clearTimeout(timer);
      peer.removeEventListener('icegatheringstatechange', finish);
      resolve();
    };
    const timer = setTimeout(() => {
      peer.removeEventListener('icegatheringstatechange', finish);
      reject(
        Object.assign(new Error('ice_timeout'), { code: 'connection_timeout' }),
      );
    }, 10000);
    peer.addEventListener('icegatheringstatechange', finish);
    finish();
  });
}
