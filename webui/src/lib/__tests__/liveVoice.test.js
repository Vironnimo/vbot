import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  createLiveActions,
  createLiveVoice,
  createLiveVoiceState,
} from '../liveVoice.js';

const terminal = (id = 't1') => ({
  terminal_id: id,
  launch_command: 'codex',
  group_id: 'g1',
  screen_revision: 12,
});
function actionsFixture() {
  const api = {
    listTerminals: vi.fn().mockResolvedValue({
      terminals: [terminal(), terminal('t2')],
      groups: [{ group_id: 'g1', name: 'Codex', kind: 'user' }],
    }),
    startTerminal: vi.fn().mockImplementation(async () => ({
      terminal: terminal(`new-${api.startTerminal.mock.calls.length}`),
    })),
    createTerminalGroup: vi
      .fn()
      .mockResolvedValue({ group: { group_id: 'new-group' } }),
    readTerminal: vi.fn().mockResolvedValue({
      terminal: terminal(),
      screen: 'fixture',
      bracketed_paste: true,
    }),
    sendTerminalInput: vi.fn().mockResolvedValue({ terminal: terminal() }),
    killTerminal: vi
      .fn()
      .mockResolvedValue({ terminal: { ...terminal(), state: 'exited' } }),
    forgetTerminal: vi.fn().mockResolvedValue({ terminal: terminal() }),
    renameTerminalGroup: vi
      .fn()
      .mockResolvedValue({
        group: { group_id: 'g1', name: 'Review', kind: 'user' },
      }),
    deleteTerminalGroup: vi
      .fn()
      .mockResolvedValue({ group_id: 'g1', terminals_killed: 2 }),
    setTerminalGroupOrder: vi.fn().mockResolvedValue({ order: ['t2', 't1'] }),
    loadChatHistory: vi.fn().mockResolvedValue({ messages: [] }),
    listSessions: vi
      .fn()
      .mockResolvedValue({ sessions: [{ session_id: 's1' }] }),
    startChatRun: vi.fn().mockResolvedValue({ run_id: 'r1', status: 'queued' }),
  };
  const navigate = vi.fn().mockResolvedValue(true);
  const terminalView = vi
    .fn()
    .mockResolvedValue({ visible_order: ['t1', 't2'] });
  const execute = createLiveActions({
    api,
    navigate,
    terminalView,
    getContext: () => ({ selected_agent_id: 'joel' }),
  });
  return { api, navigate, terminalView, execute };
}

describe('voice app operations', () => {
  it('keeps confirmed launches when displaying the new group fails', async () => {
    const { execute, terminalView, api } = actionsFixture();
    terminalView.mockRejectedValue(new Error('navigation failed'));
    const result = await execute('vbot_terminal', {
      action: 'start',
      program: 'codex',
      count: 4,
      workdir: '/repo',
    });
    expect(result.completed).toHaveLength(4);
    expect(result.layout_error).toBe('navigation_not_applied');
    expect(api.startTerminal).toHaveBeenCalledTimes(4);
  });
  it('bounds quoted chat context and omits launch history from terminal discovery', async () => {
    const { execute, api } = actionsFixture();
    api.loadChatHistory.mockResolvedValue({
      messages: Array.from({ length: 20 }, (_, id) => ({
        id,
        role: 'assistant',
        content: 'x'.repeat(12000),
      })),
    });
    const chat = await execute('vbot_app', {
      action: 'read',
      agent_id: 'joel',
      session_id: 's',
    });
    expect(
      chat.messages.reduce((n, m) => n + m.content.length, 0),
    ).toBeLessThanOrEqual(8000);
    api.listTerminals.mockResolvedValue({
      terminals: [terminal()],
      groups: [],
      launch_history: [{ secret: 'unused' }],
    });
    const catalog = await execute('vbot_terminal', { action: 'list' });
    expect(catalog).not.toHaveProperty('launch_history');
    expect(catalog.terminals[0].terminal_id).toBe('t1');
  });
  it('starts four coding agents in the specified server directory and displays their group', async () => {
    const { execute, api, terminalView } = actionsFixture();
    const result = await execute('vbot_terminal', {
      action: 'start',
      program: 'codex',
      count: 4,
      workdir: '/projects/vbot',
    });
    expect(api.startTerminal).toHaveBeenCalledTimes(4);
    expect(api.startTerminal).toHaveBeenCalledWith({
      command: 'codex',
      workdir: '/projects/vbot',
      group_id: 'g1',
    });
    expect(result.completed).toHaveLength(4);
    expect(terminalView).toHaveBeenCalledWith('show', { terminal_id: 'new-1' });
  });
  it('preserves partial launch results and does not retry an uncertain start', async () => {
    const { execute, api } = actionsFixture();
    api.startTerminal
      .mockResolvedValueOnce({ terminal: terminal('new') })
      .mockRejectedValueOnce(new Error('network'));
    const result = await execute('vbot_terminal', {
      action: 'start',
      program: 'codex',
      count: 4,
      workdir: '/repo',
    });
    expect(result.ok).toBe(false);
    expect(result.completed).toEqual([terminal('new')]);
    expect(api.startTerminal).toHaveBeenCalledTimes(2);
  });
  it.each([
    { program: 'bash' },
    { program: 'codex', count: 1.5 },
    { program: 'codex', count: '5' },
    { program: 'codex', count: Infinity },
    { program: 'codex', count: Number.MAX_SAFE_INTEGER + 1 },
    { program: 'claude', count: 0 },
    { program: 'codex', count: 1, workdir: '' },
  ])('rejects an invalid launch before effects: %j', async (args) => {
    const { execute, api } = actionsFixture();
    await expect(
      execute('vbot_terminal', { action: 'start', workdir: '/repo', ...args }),
    ).rejects.toThrow();
    expect(api.startTerminal).not.toHaveBeenCalled();
    expect(api.createTerminalGroup).not.toHaveBeenCalled();
  });
  it.each([1, 5, 12, 33])(
    'launches the requested %i terminals without a separate Live limit',
    async (count) => {
      const { execute, api } = actionsFixture();
      const result = await execute('vbot_terminal', {
        action: 'start',
        program: 'claude',
        count,
        workdir: '/repo',
      });
      expect(api.startTerminal).toHaveBeenCalledTimes(count);
      expect(result.completed).toHaveLength(count);
    },
  );
  it('defaults an omitted launch count to one', async () => {
    const { execute, api } = actionsFixture();
    await execute('vbot_terminal', {
      action: 'start',
      program: 'codex',
      workdir: '/repo',
    });
    expect(api.startTerminal).toHaveBeenCalledOnce();
  });
  it('reports the server capacity failure with confirmed launches and no retry', async () => {
    const { execute, api } = actionsFixture();
    api.startTerminal.mockImplementation(async () => {
      if (api.startTerminal.mock.calls.length === 6)
        throw Object.assign(new Error('fixture capacity exhausted'), {
          code: 'invalid_request',
        });
      return {
        terminal: terminal(`started-${api.startTerminal.mock.calls.length}`),
      };
    });
    const result = await execute('vbot_terminal', {
      action: 'start',
      program: 'codex',
      count: 12,
      workdir: '/repo',
    });
    expect(result.requested_count).toBe(12);
    expect(result.completed).toHaveLength(5);
    expect(result.error.message).toBe('fixture capacity exhausted');
    expect(api.startTerminal).toHaveBeenCalledTimes(6);
  });
  it('closes the exact terminal by stopping it before removing its tile', async () => {
    const { execute, api, terminalView } = actionsFixture();
    api.forgetTerminal.mockImplementation(async () => {
      expect(api.killTerminal).toHaveBeenCalledWith('t2');
      return {};
    });
    const result = await execute('vbot_terminal', {
      action: 'close',
      terminal_id: 't2',
    });
    expect(result).toEqual({ terminal_id: 't2', stopped: true, removed: true });
    expect(api.killTerminal).toHaveBeenCalledOnce();
    expect(api.forgetTerminal).toHaveBeenCalledWith('t2');
    expect(terminalView).toHaveBeenCalledWith('refresh', {});
  });
  it('removes a finished terminal without attempting to stop it again', async () => {
    const { execute, api } = actionsFixture();
    api.listTerminals.mockResolvedValue({
      terminals: [{ ...terminal(), state: 'exited' }],
    });
    await execute('vbot_terminal', { action: 'close', terminal_id: 't1' });
    expect(api.killTerminal).not.toHaveBeenCalled();
    expect(api.forgetTerminal).toHaveBeenCalledWith('t1');
  });
  it('preserves a confirmed stop if removing the tile fails', async () => {
    const { execute, api } = actionsFixture();
    api.forgetTerminal.mockRejectedValue(new Error('fixture removal failed'));
    const result = await execute('vbot_terminal', {
      action: 'close',
      terminal_id: 't1',
    });
    expect(result).toMatchObject({
      ok: false,
      stopped: true,
      removed: null,
      terminal_id: 't1',
    });
    expect(api.killTerminal).toHaveBeenCalledOnce();
    expect(api.forgetTerminal).toHaveBeenCalledOnce();
  });
  it('does not remove a terminal after an uncertain stop', async () => {
    const { execute, api } = actionsFixture();
    api.killTerminal.mockRejectedValue(new Error('fixture stop failed'));
    const result = await execute('vbot_terminal', {
      action: 'close',
      terminal_id: 't1',
    });
    expect(result).toMatchObject({ ok: false, stopped: null, removed: false });
    expect(api.killTerminal).toHaveBeenCalledOnce();
    expect(api.forgetTerminal).not.toHaveBeenCalled();
  });
  it('stops a close sequence when Live is stopped after the process terminates', async () => {
    const { execute, api } = actionsFixture();
    let active = true;
    api.killTerminal.mockImplementation(async () => {
      active = false;
      return {};
    });
    const result = await execute(
      'vbot_terminal',
      { action: 'close', terminal_id: 't1' },
      () => active,
    );
    expect(result).toMatchObject({ ok: false, stopped: true, removed: false });
    expect(api.forgetTerminal).not.toHaveBeenCalled();
  });
  it('creates and displays an empty group, then can select it by id', async () => {
    const { execute, api, terminalView } = actionsFixture();
    const result = await execute('vbot_terminal', {
      action: 'create_group',
      name: 'Review',
    });
    expect(result.group.group_id).toBe('new-group');
    expect(api.createTerminalGroup).toHaveBeenCalledWith('Review');
    expect(terminalView).toHaveBeenCalledWith('show_group', {
      group_id: 'new-group',
    });
    await execute('vbot_terminal', { action: 'show_group', group_id: 'g1' });
    expect(terminalView).toHaveBeenCalledWith('show_group', { group_id: 'g1' });
  });
  it.each(['user', 'agent'])(
    'renames and deletes %s groups using the existing operator endpoints',
    async (kind) => {
      const { execute, api, terminalView } = actionsFixture();
      api.listTerminals.mockResolvedValue({
        groups: [{ group_id: 'g1', kind }],
        terminals: [terminal()],
      });
      const renamed = await execute('vbot_terminal', {
        action: 'rename_group',
        group_id: 'g1',
        name: 'Review',
      });
      expect(api.renameTerminalGroup).toHaveBeenCalledWith('g1', 'Review');
      expect(renamed.group.name).toBe('Review');
      const deleted = await execute('vbot_terminal', {
        action: 'delete_group',
        group_id: 'g1',
      });
      expect(api.deleteTerminalGroup).toHaveBeenCalledWith('g1');
      expect(deleted).toEqual({ group_id: 'g1', terminals_killed: 2 });
      expect(terminalView).toHaveBeenCalledTimes(2);
    },
  );
  it.each(['automatic', 'finished'])(
    'rejects edits of a built-in %s group before mutation',
    async (kind) => {
      const { execute, api } = actionsFixture();
      api.listTerminals.mockResolvedValue({
        groups: [{ group_id: 'g1', kind }],
      });
      await expect(
        execute('vbot_terminal', {
          action: 'rename_group',
          group_id: 'g1',
          name: 'New',
        }),
      ).rejects.toThrow('group_not_editable');
      await expect(
        execute('vbot_terminal', { action: 'delete_group', group_id: 'g1' }),
      ).rejects.toThrow('group_not_editable');
      expect(api.renameTerminalGroup).not.toHaveBeenCalled();
      expect(api.deleteTerminalGroup).not.toHaveBeenCalled();
    },
  );
  it.each([
    { action: 'close' },
    { action: 'close', terminal_id: 'missing' },
    { action: 'rename_group', group_id: 'g1', name: '' },
    { action: 'rename_group', group_id: 'g1', name: 'x'.repeat(81) },
    { action: 'rename_group', group_id: 'missing', name: 'New' },
    { action: 'delete_group', group_id: 'missing' },
    { action: 'delete_group', group_id: 'g1', name: 'unexpected' },
  ])('rejects invalid targets and fields before effects: %j', async (args) => {
    const { execute, api } = actionsFixture();
    await expect(execute('vbot_terminal', args)).rejects.toThrow();
    expect(api.killTerminal).not.toHaveBeenCalled();
    expect(api.forgetTerminal).not.toHaveBeenCalled();
    expect(api.renameTerminalGroup).not.toHaveBeenCalled();
    expect(api.deleteTerminalGroup).not.toHaveBeenCalled();
  });
  it('keeps successful mutations when refreshing the layout fails', async () => {
    const { execute, api, terminalView } = actionsFixture();
    terminalView.mockRejectedValue(new Error('fixture refresh failed'));
    const created = await execute('vbot_terminal', {
      action: 'create_group',
      name: 'Review',
    });
    const renamed = await execute('vbot_terminal', {
      action: 'rename_group',
      group_id: 'g1',
      name: 'Review',
    });
    const deleted = await execute('vbot_terminal', {
      action: 'delete_group',
      group_id: 'g1',
    });
    const closed = await execute('vbot_terminal', {
      action: 'close',
      terminal_id: 't1',
    });
    expect(created.group.group_id).toBe('new-group');
    expect(renamed.group.name).toBe('Review');
    expect(deleted.terminals_killed).toBe(2);
    expect(closed.stopped && closed.removed).toBe(true);
    for (const result of [created, renamed, deleted, closed])
      expect(result.layout_error).toBe('refresh_failed');
    expect(api.deleteTerminalGroup).toHaveBeenCalledOnce();
    expect(api.killTerminal).toHaveBeenCalledOnce();
  });
  it('does not hide groups or retained terminals beyond the first forty entries', async () => {
    const { execute, api } = actionsFixture();
    const groups = Array.from({ length: 45 }, (_, i) => ({
      group_id: `g${i}`,
      kind: 'user',
    }));
    api.listTerminals.mockResolvedValue({
      groups,
      terminals: Array.from({ length: 45 }, (_, i) => terminal(`t${i}`)),
    });
    const result = await execute('vbot_terminal', { action: 'list' });
    expect(result.groups.at(-1).group_id).toBe('g44');
    expect(result.terminals.at(-1).terminal_id).toBe('t44');
  });
  it('forwards a reply to its exact Session without changing the message or confirming again', async () => {
    const { execute, api } = actionsFixture();
    await execute('vbot_app', {
      action: 'send',
      agent_id: 'joel@project',
      session_id: 's1',
      text: 'Use option one.',
    });
    expect(api.startChatRun).toHaveBeenCalledWith({
      agent_id: 'joel@project',
      session_id: 's1',
      content: 'Use option one.',
      input_origin: 'speech_transcription',
    });
  });
  it('does not guess a Session when the caller omits it', async () => {
    const { execute, api } = actionsFixture();
    await expect(
      execute('vbot_app', {
        action: 'send',
        agent_id: 'joel',
        text: 'continue',
      }),
    ).rejects.toThrow();
    expect(api.startChatRun).not.toHaveBeenCalled();
  });
  it('preserves multiline paste and guards input against an intervening screen change', async () => {
    const { execute, api } = actionsFixture();
    await execute('vbot_terminal', {
      action: 'input',
      terminal_id: 't1',
      text: 'one\ntwo',
    });
    expect(api.sendTerminalInput).toHaveBeenCalledWith(
      't1',
      '\u001b[200~one\ntwo\u001b[201~\r',
      { expectedScreenRevision: 12 },
    );
  });
  it('rejects mixed input modes and arbitrary control sequences', async () => {
    const { execute, api } = actionsFixture();
    await expect(
      execute('vbot_terminal', {
        action: 'input',
        terminal_id: 't1',
        text: 'x',
        key: 'enter',
      }),
    ).rejects.toThrow();
    await expect(
      execute('vbot_terminal', {
        action: 'input',
        terminal_id: 't1',
        text: '\u001b[31m',
      }),
    ).rejects.toThrow();
    expect(api.sendTerminalInput).not.toHaveBeenCalled();
  });
  it('does not operate a shell as if it were a coding agent', async () => {
    const { execute, api } = actionsFixture();
    api.listTerminals.mockResolvedValue({
      terminals: [{ ...terminal(), launch_command: 'bash' }],
    });
    await expect(
      execute('vbot_terminal', {
        action: 'input',
        terminal_id: 't1',
        text: 'rm file',
      }),
    ).rejects.toThrow();
    expect(api.sendTerminalInput).not.toHaveBeenCalled();
  });
  it('uses actual group order and rejects missing or duplicate members', async () => {
    const { execute, api } = actionsFixture();
    await expect(
      execute('vbot_terminal', {
        action: 'reorder',
        group_id: 'g1',
        order: ['t1', 't1'],
      }),
    ).rejects.toThrow();
    expect(api.setTerminalGroupOrder).not.toHaveBeenCalled();
    await execute('vbot_terminal', {
      action: 'reorder',
      group_id: 'g1',
      order: ['t2', 't1'],
    });
    expect(api.setTerminalGroupOrder).toHaveBeenCalledWith('g1', ['t2', 't1']);
  });
  it('routes maximize and restore through the actual terminal view', async () => {
    const { execute, terminalView } = actionsFixture();
    await execute('vbot_terminal', { action: 'maximize', terminal_id: 't2' });
    await execute('vbot_terminal', { action: 'restore' });
    expect(terminalView).toHaveBeenCalledWith('maximize', {
      action: 'maximize',
      terminal_id: 't2',
    });
    expect(terminalView).toHaveBeenCalledWith('restore');
  });
  it('stops a multi-launch when its originating conversation is replaced', async () => {
    const { execute, api } = actionsFixture();
    let current = true;
    api.startTerminal.mockImplementationOnce(async () => {
      current = false;
      return { terminal: terminal('first') };
    });
    const result = await execute(
      'vbot_terminal',
      { action: 'start', program: 'codex', count: 4, workdir: '/repo' },
      () => current,
    );
    expect(result.completed).toHaveLength(1);
    expect(api.startTerminal).toHaveBeenCalledTimes(1);
  });
});

class Events {
  listeners = new Map();
  addEventListener(name, callback) {
    this.listeners.set(name, [...(this.listeners.get(name) || []), callback]);
  }
  removeEventListener(name, callback) {
    this.listeners.set(
      name,
      (this.listeners.get(name) || []).filter((fn) => fn !== callback),
    );
  }
  emit(name, value = {}) {
    for (const callback of this.listeners.get(name) || []) callback(value);
  }
}
function voiceFixture(
  execute = vi.fn().mockResolvedValue({ delivered: true }),
) {
  const channel = new Events();
  Object.assign(channel, { readyState: 'open', send: vi.fn(), close: vi.fn() });
  const track = new Events();
  Object.assign(track, { stop: vi.fn(), enabled: true });
  const microphone = {
    getTracks: () => [track],
    getAudioTracks: () => [track],
  };
  const peer = new Events();
  Object.assign(peer, {
    addTrack: vi.fn(),
    createDataChannel: vi.fn(() => channel),
    createOffer: vi.fn().mockResolvedValue({ sdp: 'v=0' }),
    setLocalDescription: vi.fn(),
    localDescription: { sdp: 'v=0' },
    iceGatheringState: 'complete',
    setRemoteDescription: vi.fn(),
    close: vi.fn(),
  });
  const api = {
    getLiveVoiceStatus: vi.fn().mockResolvedValue({ configured: true }),
    createLiveVoiceSession: vi.fn().mockResolvedValue({
      session: { id: 'live' },
      transport: { sdp: 'answer' },
    }),
    loadChatRunResult: vi
      .fn()
      .mockResolvedValue({ content: 'Which option?', found: true }),
  };
  const mediaDevices = { getUserMedia: vi.fn().mockResolvedValue(microphone) };
  const state = createLiveVoiceState();
  const controller = createLiveVoice({
    state,
    execute,
    api,
    mediaDevices,
    createPeer: () => peer,
    now: () => 1000,
  });
  const emit = (event) =>
    channel.emit('message', { data: JSON.stringify(event) });
  const nested = (event) =>
    emit({ type: 'response.event', delegation_id: 'delegation', event });
  return {
    controller,
    state,
    channel,
    track,
    peer,
    api,
    mediaDevices,
    emit,
    nested,
    execute,
  };
}

afterEach(() => vi.useRealTimers());
describe('Live WebRTC lifecycle and tool events', () => {
  it('does not block a new conversation behind an old request or show its late result', async () => {
    let finishOld;
    const execute = vi
      .fn()
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finishOld = resolve;
          }),
      )
      .mockResolvedValue({ delivered: true });
    const f = voiceFixture(execute);
    const request = (id) => {
      f.nested({ type: 'response.created', response: { id } });
      f.nested({
        type: 'response.output_item.done',
        item: {
          type: 'function_call',
          name: 'vbot_app',
          call_id: id,
          arguments: '{}',
        },
      });
      f.nested({ type: 'response.completed', response: { id } });
    };
    await f.controller.start();
    f.emit({ type: 'session.started' });
    request('old');
    await vi.waitFor(() => expect(execute).toHaveBeenCalledTimes(1));
    const oldWork = f.controller.flush();
    f.controller.stop();
    f.emit({ type: 'session.closed' });
    await f.controller.start();
    f.emit({ type: 'session.started' });
    request('new');
    await f.controller.flush();
    expect(execute).toHaveBeenCalledTimes(2);
    expect(f.state.actions).toHaveLength(1);
    finishOld({ delivered: true });
    await oldWork;
    expect(f.state.actions).toHaveLength(1);
    f.controller.destroy();
  });
  it('returns all batch results before continuation and suppresses late effects after Stop', async () => {
    const f = voiceFixture();
    await f.controller.start();
    f.emit({ type: 'session.started' });
    f.nested({ type: 'response.created', response: { id: 'batch' } });
    for (const call_id of ['one', 'two'])
      f.nested({
        type: 'response.output_item.done',
        item: {
          type: 'function_call',
          name: 'vbot_app',
          call_id,
          arguments: '{"action":"context"}',
        },
      });
    f.nested({ type: 'response.completed', response: { id: 'batch' } });
    await f.controller.flush();
    expect(
      f.channel.send.mock.calls.map(([data]) => JSON.parse(data).type),
    ).toEqual([
      'response.item.create',
      'response.item.create',
      'response.create',
    ]);
    f.controller.stop();
    f.nested({ type: 'response.created', response: { id: 'late' } });
    f.nested({
      type: 'response.output_item.done',
      item: {
        type: 'function_call',
        name: 'vbot_app',
        call_id: 'late',
        arguments: '{}',
      },
    });
    f.nested({ type: 'response.completed', response: { id: 'late' } });
    await f.controller.flush();
    expect(f.execute).toHaveBeenCalledTimes(2);
    f.controller.destroy();
  });
  it('requires a usable key before requesting the microphone', async () => {
    const f = voiceFixture();
    f.api.getLiveVoiceStatus.mockResolvedValue({ configured: false });
    await f.controller.start();
    expect(f.state.error).toBe('api_key_required');
    expect(f.mediaDevices.getUserMedia).not.toHaveBeenCalled();
  });
  it('waits for session.started and releases media after graceful close', async () => {
    const f = voiceFixture();
    await f.controller.start();
    expect(f.state.phase).toBe('connecting');
    expect(f.channel.send).not.toHaveBeenCalled();
    f.emit({ type: 'session.started' });
    expect(f.state.phase).toBe('listening');
    f.controller.stop();
    expect(f.track.enabled).toBe(false);
    expect(f.track.stop).not.toHaveBeenCalled();
    expect(JSON.parse(f.channel.send.mock.calls[0][0])).toEqual({
      type: 'session.close',
    });
    f.emit({ type: 'session.closed', usage: { seconds: 10 } });
    expect(f.track.stop).toHaveBeenCalledOnce();
    expect(f.state.finalized).toBe(true);
    expect(f.state.usage).toEqual({ seconds: 10 });
  });
  it('executes completed function items once and submits every result before continuation', async () => {
    const f = voiceFixture();
    await f.controller.start();
    f.emit({ type: 'session.started' });
    f.nested({ type: 'response.created', response: { id: 'r' } });
    const item = {
      type: 'function_call',
      call_id: 'c',
      name: 'vbot_app',
      arguments: '{"action":"context"}',
    };
    f.nested({ type: 'response.output_item.done', item });
    f.nested({ type: 'response.output_item.done', item });
    f.nested({ type: 'response.created', response: { id: 'r' } });
    f.nested({ type: 'response.completed', response: { id: 'r', output: [] } });
    f.nested({ type: 'response.completed', response: { id: 'r', output: [] } });
    await f.controller.flush();
    expect(f.execute).toHaveBeenCalledTimes(1);
    const sent = f.channel.send.mock.calls.map(([data]) => JSON.parse(data));
    expect(sent.map((event) => event.type)).toEqual([
      'response.item.create',
      'response.create',
    ]);
    expect(sent[0].item.call_id).toBe('c');
    f.controller.destroy();
  });
  it('never executes an unfinished or failed response', async () => {
    const f = voiceFixture();
    await f.controller.start();
    f.emit({ type: 'session.started' });
    f.nested({ type: 'response.created', response: { id: 'r' } });
    f.nested({
      type: 'response.output_item.done',
      item: {
        type: 'function_call',
        call_id: 'c',
        name: 'vbot_app',
        arguments: '{}',
      },
    });
    f.nested({ type: 'response.failed', response: { id: 'r' } });
    await f.controller.flush();
    expect(f.execute).not.toHaveBeenCalled();
    f.controller.destroy();
  });
  it('discards a late microphone grant after Stop', async () => {
    const f = voiceFixture();
    let grant;
    f.mediaDevices.getUserMedia.mockImplementation(
      () =>
        new Promise((resolve) => {
          grant = resolve;
        }),
    );
    const started = f.controller.start();
    await Promise.resolve();
    await Promise.resolve();
    f.controller.stop();
    grant({ getTracks: () => [f.track] });
    await started;
    expect(f.track.stop).toHaveBeenCalledOnce();
    expect(f.api.createLiveVoiceSession).not.toHaveBeenCalled();
  });
  it('deduplicates scoped Run notices and excludes old and background activity', async () => {
    const f = voiceFixture();
    await f.controller.start();
    f.emit({ type: 'session.started' });
    const event = {
      type: 'run_completed',
      payload: {
        run_id: 'r',
        agent_id: 'joel',
        project_id: 'project',
        session_id: 's',
        run_event_timestamp: '2026-09-11T12:00:00Z',
      },
    };
    await f.controller.notifyRuns([
      event,
      event,
      {
        ...event,
        payload: {
          ...event.payload,
          run_id: 'old',
          run_event_timestamp: '1970-01-01T00:00:00Z',
        },
      },
      {
        ...event,
        payload: {
          ...event.payload,
          run_id: 'background',
          contributes_to_agent_activity: false,
        },
      },
    ]);
    expect(f.api.loadChatRunResult).toHaveBeenCalledExactlyOnceWith({
      agent_id: 'joel@project',
      session_id: 's',
      run_id: 'r',
    });
    expect(f.state.updates[0].agent_id).toBe('joel@project');
    const notice = JSON.parse(f.channel.send.mock.calls.at(-1)[0]);
    expect(notice.type).toBe('session.commentary.append');
    expect(notice.delegation_id).toBeNull();
    expect(JSON.parse(notice.content).session_id).toBe('s');
    f.controller.destroy();
  });
  it('marks final usage unconfirmed on close timeout', async () => {
    vi.useFakeTimers();
    const f = voiceFixture();
    await f.controller.start();
    f.emit({ type: 'session.started' });
    f.controller.stop();
    await vi.advanceTimersByTimeAsync(15000);
    expect(f.state.error).toBe('finalization_incomplete');
    expect(f.state.finalized).toBe(false);
    expect(f.track.stop).toHaveBeenCalledOnce();
  });
});
