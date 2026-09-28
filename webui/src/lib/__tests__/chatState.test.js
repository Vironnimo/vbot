import { describe, expect, it, vi } from 'vitest';
import {
  appendRunEvent,
  createChatState,
  currentSessionState,
  ensureSessionState,
  isProjectSelected,
  isSessionEmpty,
  loadHistory,
  pickProjectAgentSessionId,
  removeQueuedMessage,
  resolveAgentAddressing,
  selectedAgent,
  setAgents,
} from '../chatState.js';
import { setupController } from './chatState.support.js';

describe('Session state', () => {
  it('tracks selected agent and per-agent current session state', () => {
    const state = createChatState();

    const selectedAgentId = setAgents(state, [
      { id: 'alpha', current_session_id: 'session-one' },
      { id: 'beta', current_session_id: 'session-two' },
    ]);
    const sessionState = ensureSessionState(state, 'alpha', 'session-one');

    expect(selectedAgentId).toBe('alpha');
    expect(selectedAgent(state)).toEqual({
      id: 'alpha',
      current_session_id: 'session-one',
    });
    expect(sessionState.key).toBe('alpha::session-one');
  });

  it('falls back to the first canonical Agent when selection is unavailable', () => {
    const state = createChatState();
    state.selectedAgentId = 'removed';

    const selectedAgentId = setAgents(state, [
      { id: 'preferred-first', current_session_id: 'session-one' },
      { id: 'alpha', current_session_id: 'session-two' },
    ]);

    expect(selectedAgentId).toBe('preferred-first');
  });

  it('does not create session state when reading the current session', () => {
    const state = createChatState();

    setAgents(state, [{ id: 'alpha', current_session_id: 'session-one' }]);

    expect(currentSessionState(state)).toBeNull();
    expect(state.sessions).toEqual({});

    const createdSessionState = ensureSessionState(
      state,
      'alpha',
      'session-one',
    );

    expect(currentSessionState(state)).toBe(createdSessionState);
  });

  it('classifies only loaded sessions without conversation activity as empty', async () => {
    const { chatState, controller, listQueue } = setupController();
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');

    expect(isSessionEmpty(sessionState)).toBe(false);

    loadHistory(sessionState, []);
    expect(isSessionEmpty(sessionState)).toBe(true);

    await controller.syncSessionQueue(sessionState);
    expect(isSessionEmpty(sessionState)).toBe(false);

    listQueue.mockResolvedValueOnce({ items: [] });
    await controller.syncSessionQueue(sessionState);
    loadHistory(sessionState, [
      { id: 'message-one', role: 'user', content: 'Hello' },
    ]);
    expect(isSessionEmpty(sessionState)).toBe(false);
  });
});

describe('Queue projection', () => {
  it('projects server Queue items: append, authoritative replace, edit and remove', async () => {
    const first = {
      id: 'queue-1',
      content: 'First message',
      editable: true,
      created_at: '2026-05-22T01:00:00+00:00',
      steerable: false,
      steering: false,
    };
    const second = {
      id: 'queue-2',
      content: 'Second message',
      editable: false,
      created_at: '2026-05-22T01:01:00+00:00',
      steerable: false,
      steering: false,
    };

    const startChatRun = vi
      .fn()
      .mockResolvedValueOnce({
        queued: true,
        item: { id: 'queue-old', content: 'Old' },
      })
      .mockResolvedValueOnce({
        queued: true,
        item: { id: 'queue-new', content: 'New' },
      });
    const { chatState, controller } = setupController({
      operationOverrides: {
        startChatRun,
        listQueue: vi.fn().mockResolvedValue({ items: [first, second] }),
        updateQueueItem: vi.fn().mockResolvedValue({ ok: true }),
      },
    });
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');

    await controller.sendMessage(sessionState, 'Old');
    await controller.sendMessage(sessionState, 'New');
    expect(sessionState.queue.map((item) => item.id)).toEqual([
      'queue-old',
      'queue-new',
    ]);

    await controller.syncSessionQueue(sessionState);
    expect(sessionState.queue).toEqual([first, second]);

    await controller.updateQueued(sessionState, 'queue-1', 'Edited', [
      'notes.md',
    ]);
    expect(sessionState.queue[0]).toMatchObject({
      content: 'Edited',
      editable: false,
    });
    // An edit the server accepted for an item this client no longer holds
    // leaves the projection unchanged.
    await controller.updateQueued(sessionState, 'queue-missing', 'Anything');
    expect(sessionState.queue.map((item) => item.content)).toEqual([
      'Edited',
      'Second message',
    ]);

    expect(removeQueuedMessage(sessionState, 'queue-1')).toBe(true);
    expect(removeQueuedMessage(sessionState, 'queue-missing')).toBe(false);
    expect(sessionState.queue).toEqual([second]);
  });

  it('does not resurrect delivered steering input from an older Queue response', async () => {
    const queued = {
      id: 'queue-steer',
      content: 'Correction',
      steerable: true,
    };
    const { chatState, controller } = setupController({
      operationOverrides: {
        startChatRun: vi.fn().mockResolvedValue({ queued: true, item: queued }),
        listQueue: vi.fn().mockResolvedValue({
          items: [queued, { id: 'queue-later', content: 'Later' }],
        }),
      },
    });
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    await controller.sendMessage(sessionState, queued.content);
    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      sequence: 2,
      payload: {
        queue_item_id: queued.id,
        message: { id: 'steered-user', role: 'user', content: queued.content },
      },
    });
    expect(sessionState.queue).toEqual([]);

    await controller.syncSessionQueue(sessionState);
    expect(sessionState.queue.map((item) => item.id)).toEqual(['queue-later']);
  });

  it('loads history without losing the visible queue', async () => {
    const { chatState, controller } = setupController();
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    await controller.syncSessionQueue(sessionState);

    loadHistory(sessionState, [
      { id: 'message-one', role: 'user', content: 'Hi' },
    ]);

    expect(sessionState.messages).toEqual([
      { id: 'message-one', role: 'user', content: 'Hi' },
    ]);
    expect(sessionState.queue).toHaveLength(1);
  });
});

describe('project addressing', () => {
  it('distinguishes a real project selection from Personal/empty', () => {
    expect(isProjectSelected('vbot')).toBe(true);
    for (const personal of ['', '   ', null, undefined]) {
      expect(isProjectSelected(personal)).toBe(false);
    }
  });

  // Chat, Session and History RPCs take the full address; Queue and Tool
  // cancellation take the bare id. A Personal selection never produces a
  // project address, so identity RPCs send the bare id unchanged.
  it.each([
    {
      name: 'an Identity Agent',
      args: ['builder', '', false],
      addressing: {
        bareAgentId: 'builder',
        projectId: null,
        agentAddress: 'builder',
      },
    },
    {
      name: 'a project Agent under a Personal selection',
      args: ['builder', '', true],
      addressing: {
        bareAgentId: 'builder',
        projectId: null,
        agentAddress: 'builder',
      },
    },
    {
      name: 'a project Agent',
      args: ['builder', 'vbot', true],
      addressing: {
        bareAgentId: 'builder',
        projectId: 'vbot',
        agentAddress: 'builder@vbot',
      },
    },
  ])('resolves the addressing of $name', ({ args, addressing }) => {
    expect(resolveAgentAddressing(...args)).toEqual(addressing);
  });

  // A project (config) Agent has no server current_session_id: the Agent bar
  // lands on the newest user Session from session.list, or '' so the caller
  // creates one.
  const userSession = {
    id: 'user-session',
    last_active_at: '2026-07-20T09:00:00+00:00',
    run_kinds: ['user'],
  };
  it.each([
    {
      name: 'the most recently active Session',
      sessions: [
        {
          id: 'old',
          created_at: '2026-06-01T00:00:00+00:00',
          last_active_at: '2026-06-01T08:00:00+00:00',
        },
        {
          id: 'newest',
          created_at: '2026-06-02T00:00:00+00:00',
          last_active_at: '2026-06-10T09:00:00+00:00',
        },
        {
          id: 'middle',
          created_at: '2026-06-03T00:00:00+00:00',
          last_active_at: '2026-06-05T09:00:00+00:00',
        },
      ],
      expected: 'newest',
    },
    {
      name: 'the newest created Session without activity times',
      sessions: [
        { id: 'first', created_at: '2026-06-01T00:00:00+00:00' },
        { id: 'second', created_at: '2026-06-09T00:00:00+00:00' },
      ],
      expected: 'second',
    },
    {
      name: 'a user Session over newer Subagent, Cron and Reflection Sessions',
      sessions: [
        userSession,
        {
          id: 'newer-child',
          last_active_at: '2026-07-20T10:00:00+00:00',
          is_subagent_session: true,
          subagent_parent: {
            agent_id: 'orchestrator',
            session_id: 'parent-session',
          },
        },
        {
          id: 'cron-session',
          last_active_at: '2026-07-20T11:00:00+00:00',
          run_kinds: ['cron'],
        },
        {
          id: 'reflection-session',
          last_active_at: '2026-07-20T12:00:00+00:00',
          run_kinds: ['reflection'],
        },
      ],
      expected: 'user-session',
    },
    {
      name: 'no Session when only background Sessions exist',
      sessions: [
        {
          id: 'child',
          is_subagent_session: true,
          last_active_at: '2026-07-20T10:00:00+00:00',
        },
        {
          id: 'cron-session',
          last_active_at: '2026-07-20T11:00:00+00:00',
          run_kinds: ['cron'],
        },
      ],
      expected: '',
    },
    { name: 'no Session for an empty list', sessions: [], expected: '' },
    { name: 'no Session for a missing list', sessions: null, expected: '' },
    {
      name: 'no Session for rows without an id',
      sessions: [{ created_at: 'x' }],
      expected: '',
    },
  ])('lands a project Agent on $name', ({ sessions, expected }) => {
    expect(pickProjectAgentSessionId(sessions)).toBe(expected);
  });
});

describe('handled send commands', () => {
  const handled = (data, output = 'action') => ({
    command_handled: true,
    reply: 'Done.',
    output,
    data,
  });
  const move = (agentId) =>
    handled({
      command: 'agent',
      session_id: 'session-keep',
      agent_id: agentId,
    });

  // `/agent` moves keep the Session id and route by the target address:
  // agent@project to the project world, a bare id to the identity world.
  it.each([
    {
      name: 'a move to an Identity Agent',
      response: move('assistant'),
      outcome: {
        kind: 'move',
        move: {
          isProjectTarget: false,
          world: 'identity',
          bareAgentId: 'assistant',
          projectId: null,
          agentAddress: 'assistant',
          sessionId: 'session-keep',
        },
      },
    },
    {
      name: 'a move to a project Agent',
      response: move('builder@vbot'),
      outcome: {
        kind: 'move',
        move: {
          isProjectTarget: true,
          world: 'project',
          bareAgentId: 'builder',
          projectId: 'vbot',
          agentAddress: 'builder@vbot',
          sessionId: 'session-keep',
        },
      },
    },
    {
      name: 'a move to a malformed address',
      response: move('@vbot'),
      outcome: {
        kind: 'move',
        move: {
          isProjectTarget: false,
          world: 'identity',
          sessionId: 'session-keep',
        },
      },
    },
    {
      name: 'a handoff',
      response: handled({
        command: 'handoff',
        session_id: 's',
        agent_id: 'beta',
      }),
      outcome: {
        kind: 'switch',
        sessionSwitch: { sessionId: 's', targetAgentId: 'beta' },
      },
    },
    {
      name: 'a move without its Session',
      response: handled({ command: 'agent', agent_id: 'beta' }),
      outcome: { kind: 'toast', reply: 'Done.' },
    },
    {
      name: 'a move without its target',
      response: handled({ command: 'agent', session_id: 's', agent_id: '' }),
      outcome: { kind: 'toast', reply: 'Done.' },
    },
    {
      name: 'a transient reply',
      response: handled(null, 'transient'),
      outcome: { kind: 'transient', reply: 'Done.' },
    },
  ])('resolves $name without starting a Run', async ({ response, outcome }) => {
    const { chatState, controller } = setupController({
      operationOverrides: { startChatRun: vi.fn().mockResolvedValue(response) },
    });
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');

    expect(await controller.sendMessage(sessionState, '/agent')).toMatchObject(
      outcome,
    );
    expect(sessionState.currentRun).toBeNull();
    expect(sessionState.queue).toEqual([]);
  });
});
