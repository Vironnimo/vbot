import { describe, expect, it } from 'vitest';

import {
  CHAT_STATUS_COMPLETED,
  CHAT_STATUS_RUNNING,
  appendRunEvent,
  canCreateNewSession,
  createChatState,
  dequeueMessage,
  enqueueMessage,
  ensureSessionState,
  loadHistory,
  removeQueuedMessage,
  restoreDequeuedMessage,
  selectedAgent,
  setAgents,
  startRun,
  visibleTimelineItems,
} from '../chatState.js';

describe('chat state helpers', () => {
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

  it('loads history without losing the visible queue', () => {
    const state = createChatState();
    const sessionState = ensureSessionState(state, 'alpha', 'session-one');
    enqueueMessage(sessionState, 'queued work');

    loadHistory(sessionState, [
      { id: 'message-one', role: 'user', content: 'Hi' },
    ]);

    expect(sessionState.messages).toEqual([
      { id: 'message-one', role: 'user', content: 'Hi' },
    ]);
    expect(sessionState.queue).toHaveLength(1);
  });

  it('preserves active run events when history refreshes during a run', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    startRun(sessionState, {
      run_id: 'run-one',
      sse_url: '/api/runs/run-one/events',
      status: CHAT_STATUS_RUNNING,
    });
    appendRunEvent(sessionState, {
      type: 'reasoning',
      run_id: 'run-one',
      sequence: 1,
      payload: { message: { role: 'assistant', reasoning: 'Working' } },
    });

    loadHistory(sessionState, [
      { id: 'message-one', role: 'user', content: 'Hi' },
    ]);

    expect(sessionState.messages).toEqual([
      { id: 'message-one', role: 'user', content: 'Hi' },
    ]);
    expect(sessionState.runEvents).toEqual([
      {
        type: 'reasoning',
        run_id: 'run-one',
        sequence: 1,
        payload: { message: { role: 'assistant', reasoning: 'Working' } },
        agent_id: undefined,
        session_id: undefined,
        timestamp: undefined,
      },
    ]);
  });

  it('clears run events when history refreshes after a run finishes', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    startRun(sessionState, {
      run_id: 'run-one',
      sse_url: '/api/runs/run-one/events',
      status: CHAT_STATUS_RUNNING,
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-one',
      sequence: 1,
      payload: { message: { role: 'assistant', content: 'Done' } },
    });
    appendRunEvent(sessionState, {
      type: 'run_completed',
      run_id: 'run-one',
      sequence: 2,
      payload: { status: CHAT_STATUS_COMPLETED },
    });

    loadHistory(sessionState, [
      { id: 'message-one', role: 'assistant', content: 'Done' },
    ]);

    expect(sessionState.runEvents).toEqual([]);
  });

  it('keeps queued messages visible, FIFO, and removable before send', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    const firstMessage = enqueueMessage(sessionState, 'first');
    const secondMessage = enqueueMessage(sessionState, 'second');
    const removed = removeQueuedMessage(sessionState, firstMessage.id);
    const nextMessage = dequeueMessage(sessionState);

    expect(removed).toBe(true);
    expect(nextMessage).toEqual(secondMessage);
    expect(sessionState.queue).toEqual([]);
  });

  it('restores a dequeued message to the front when queued send fails', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    const firstMessage = enqueueMessage(sessionState, 'first');
    const secondMessage = enqueueMessage(sessionState, 'second');

    const nextMessage = dequeueMessage(sessionState);
    restoreDequeuedMessage(sessionState, nextMessage);

    expect(sessionState.queue).toEqual([firstMessage, secondMessage]);
  });

  it('blocks new session creation only while the current session has a run', () => {
    const state = createChatState();
    const sessionState = ensureSessionState(state, 'alpha', 'session-one');

    expect(canCreateNewSession(sessionState)).toBe(true);

    startRun(sessionState, {
      run_id: 'run-one',
      sse_url: '/api/runs/run-one/events',
      status: CHAT_STATUS_RUNNING,
    });

    expect(canCreateNewSession(sessionState)).toBe(false);

    appendRunEvent(sessionState, {
      type: 'run_completed',
      sequence: 2,
      payload: { status: CHAT_STATUS_COMPLETED },
    });

    expect(canCreateNewSession(sessionState)).toBe(true);
  });

  it('builds a visible timeline from history and run events', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    loadHistory(sessionState, [
      { id: 'message-one', role: 'user', content: 'Hi' },
    ]);
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      sequence: 1,
      payload: { message: { role: 'assistant', content: 'Hello' } },
    });

    expect(visibleTimelineItems(sessionState)).toEqual([
      {
        id: 'message-one',
        type: 'message',
        message: { id: 'message-one', role: 'user', content: 'Hi' },
      },
      {
        id: 'event-run-1',
        type: 'event',
        event: {
          type: 'assistant_output',
          sequence: 1,
          payload: { message: { role: 'assistant', content: 'Hello' } },
          run_id: undefined,
          agent_id: undefined,
          session_id: undefined,
          timestamp: undefined,
        },
      },
    ]);
  });
});
