import { describe, expect, it } from 'vitest';

import {
  CHAT_STATUS_COMPLETED,
  CHAT_STATUS_FAILED,
  CHAT_STATUS_IDLE,
  CHAT_STATUS_RUNNING,
  AGENT_ACTIVITY_RUNNING,
  AGENT_ACTIVITY_UNREAD,
  agentActivityStatus,
  appendRunEvent,
  canCreateNewSession,
  createChatState,
  ensureSessionState,
  isRunActive,
  loadHistory,
  resetStaleRun,
  newestUnreadSessionForAgent,
  startRun,
  updateSessionUsage,
  visibleTimelineItemsForRender,
} from '../chatState.js';

describe('chat state helpers', () => {
  it('initializes usage as null in new session state', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    expect(sessionState.usage).toBeNull();
  });

  it('sets usage via updateSessionUsage', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    const usage = { input_tokens: 8432, output_tokens: 512 };

    updateSessionUsage(sessionState, usage);

    expect(sessionState.usage).toEqual(usage);
  });

  it('updates session usage when finishRun processes a run_completed event with usage', () => {
    const chatState = createChatState();
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    startRun(sessionState, {
      run_id: 'run-one',
      sse_url: '/api/runs/run-one/events',
      status: CHAT_STATUS_RUNNING,
    });

    appendRunEvent(sessionState, {
      type: 'run_completed',
      run_id: 'run-one',
      sequence: 2,
      payload: {
        status: CHAT_STATUS_COMPLETED,
        usage: { input_tokens: 8432, output_tokens: 512 },
      },
    });

    expect(sessionState.usage).toEqual({
      input_tokens: 8432,
      output_tokens: 512,
    });
    expect(agentActivityStatus(chatState, 'alpha')).toBe(AGENT_ACTIVITY_UNREAD);
  });

  it('prioritizes a running Session over unread results and finds the newest unread Session', () => {
    const chatState = createChatState();
    const runningSession = ensureSessionState(
      chatState,
      'alpha',
      'session-running',
    );
    const olderUnread = ensureSessionState(chatState, 'alpha', 'session-older');
    const newerUnread = ensureSessionState(chatState, 'alpha', 'session-newer');
    olderUnread.hasUnreadCompletion = true;
    olderUnread.unreadRunId = 'run-old';
    olderUnread.unreadRunAt = '2026-07-20T10:00:00+00:00';
    newerUnread.hasUnreadCompletion = true;
    newerUnread.unreadRunId = 'run-new';
    newerUnread.unreadRunAt = '2026-07-20T10:05:00+00:00';
    startRun(runningSession, {
      run_id: 'run-live',
      status: CHAT_STATUS_RUNNING,
    });

    expect(agentActivityStatus(chatState, 'alpha')).toBe(
      AGENT_ACTIVITY_RUNNING,
    );
    expect(newestUnreadSessionForAgent(chatState, 'alpha')).toBe(newerUnread);
  });

  it('updates token usage after a completed model step while the run stays active', () => {
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
    const sessionUsage = {
      measured_turns: 4,
      estimated_turns: 0,
      input_tokens: 20000,
      output_tokens: 900,
    };

    appendRunEvent(sessionState, {
      type: 'model_step_usage',
      run_id: 'run-one',
      sequence: 2,
      payload: {
        usage: { input_tokens: 8432, output_tokens: 512 },
        session_usage: sessionUsage,
      },
    });

    expect(sessionState.usage).toEqual({
      input_tokens: 8432,
      output_tokens: 512,
    });
    expect(sessionState.sessionUsage).toEqual(sessionUsage);
    expect(sessionState.status).toBe(CHAT_STATUS_RUNNING);
    expect(sessionState.currentRun.status).toBe(CHAT_STATUS_RUNNING);
    expect(visibleTimelineItemsForRender(sessionState)).toEqual([]);
  });

  it('does not update usage when finishRun processes a run_failed event', () => {
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
      type: 'run_failed',
      run_id: 'run-one',
      sequence: 2,
      payload: { status: CHAT_STATUS_FAILED, error: 'Something went wrong' },
    });

    expect(sessionState.usage).toBeNull();
  });

  it('does not update usage when run_completed event has no usage payload', () => {
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
      type: 'run_completed',
      run_id: 'run-one',
      sequence: 2,
      payload: { status: CHAT_STATUS_COMPLETED },
    });

    expect(sessionState.usage).toBeNull();
  });

  it('preserves usage when run_completed event includes estimated flag', () => {
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
      type: 'run_completed',
      run_id: 'run-one',
      sequence: 2,
      payload: {
        status: CHAT_STATUS_COMPLETED,
        usage: { input_tokens: 500, output_tokens: 200, estimated: true },
      },
    });

    expect(sessionState.usage).toEqual({
      input_tokens: 500,
      output_tokens: 200,
      estimated: true,
    });
  });

  it('initializes sessionUsage as null and fills it from loadHistory options', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    expect(sessionState.sessionUsage).toBeNull();

    const sessionUsage = {
      measured_turns: 3,
      estimated_turns: 0,
      cache_turns: 3,
      input_tokens: 4200,
      output_tokens: 300,
      cache_read_tokens: 3600,
      cache_write_tokens: 120,
    };
    loadHistory(sessionState, [], { sessionUsage });

    expect(sessionState.sessionUsage).toEqual(sessionUsage);
  });

  it('keeps previous sessionUsage when a history load has none', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    const sessionUsage = { measured_turns: 1, input_tokens: 100 };
    loadHistory(sessionState, [], { sessionUsage });

    loadHistory(sessionState, [], {});

    expect(sessionState.sessionUsage).toEqual(sessionUsage);
  });

  it('refreshes sessionUsage from every terminal run event that carries it', () => {
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

    const sessionUsage = {
      measured_turns: 5,
      estimated_turns: 1,
      cache_turns: 5,
      input_tokens: 9000,
      output_tokens: 800,
      cache_read_tokens: 7200,
      cache_write_tokens: 400,
    };
    appendRunEvent(sessionState, {
      type: 'run_failed',
      run_id: 'run-one',
      sequence: 2,
      payload: {
        status: CHAT_STATUS_FAILED,
        error: 'boom',
        session_usage: sessionUsage,
      },
    });

    expect(sessionState.sessionUsage).toEqual(sessionUsage);
    expect(sessionState.usage).toBeNull();
  });

  it('sets usage from last assistant message when loading history', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Hi' },
      {
        id: 'assistant-one',
        role: 'assistant',
        content: 'Hello!',
        usage: { input_tokens: 100, output_tokens: 50 },
      },
    ]);

    expect(sessionState.usage).toEqual({
      input_tokens: 100,
      output_tokens: 50,
    });
  });

  it('picks the last assistant message usage when loading history with multiple assistant messages', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Hi' },
      {
        id: 'assistant-one',
        role: 'assistant',
        content: 'Hello!',
        usage: { input_tokens: 100, output_tokens: 50 },
      },
      { id: 'user-two', role: 'user', content: 'More' },
      {
        id: 'assistant-two',
        role: 'assistant',
        content: 'Sure!',
        usage: { input_tokens: 200, output_tokens: 75 },
      },
    ]);

    expect(sessionState.usage).toEqual({
      input_tokens: 200,
      output_tokens: 75,
    });
  });

  it('does not set usage when loading history with no assistant messages that have usage', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Hi' },
      { id: 'assistant-one', role: 'assistant', content: 'Hello!' },
    ]);

    expect(sessionState.usage).toBeNull();
  });

  it('does not overwrite usage from run_completed when loading history without usage', () => {
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
      type: 'run_completed',
      run_id: 'run-one',
      sequence: 2,
      payload: {
        status: CHAT_STATUS_COMPLETED,
        usage: { input_tokens: 8432, output_tokens: 512 },
      },
    });

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Hi' },
      { id: 'assistant-one', role: 'assistant', content: 'Hello!' },
    ]);

    expect(sessionState.usage).toEqual({
      input_tokens: 8432,
      output_tokens: 512,
    });
  });

  it('resetStaleRun clears the live run state while preserving history and run events', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-stale',
    );

    // Seed the session with a running run, some streamed content, and a
    // tool-call phase transition so streamingPhase is non-zero before reset.
    startRun(sessionState, {
      run_id: 'run-stale',
      sse_url: '/api/runs/run-stale/events',
      status: CHAT_STATUS_RUNNING,
      events: [
        {
          sequence: 1,
          run_id: 'run-stale',
          type: 'run_started',
          payload: { status: CHAT_STATUS_RUNNING },
        },
        {
          sequence: 2,
          run_id: 'run-stale',
          type: 'reasoning_delta',
          payload: { reasoning_delta: 'thinking...' },
        },
        {
          sequence: 3,
          run_id: 'run-stale',
          type: 'tool_call_started',
          payload: {
            tool_call: {
              id: 'call-one',
              index: 0,
              name: 'read',
              arguments: {},
            },
          },
        },
        {
          sequence: 4,
          run_id: 'run-stale',
          type: 'assistant_output_delta',
          payload: { content_delta: 'partial response' },
        },
      ],
    });

    // Confirm preconditions: the session is running with live run state
    expect(sessionState.status).toBe(CHAT_STATUS_RUNNING);
    expect(sessionState.streamStatus).toBe(CHAT_STATUS_RUNNING);
    expect(sessionState.currentRun).toEqual({
      runId: 'run-stale',
      sseUrl: '/api/runs/run-stale/events',
      status: CHAT_STATUS_RUNNING,
    });
    expect(sessionState.streamingRunEvents).not.toHaveLength(0);
    expect(sessionState.seenStreamingEventKeys.size).toBeGreaterThan(0);
    expect(sessionState.streamingPhase).toBeGreaterThan(0);
    expect(sessionState.runEvents).not.toHaveLength(0);
    expect(sessionState.messages).toEqual([]);

    const runEventsBefore = sessionState.runEvents.slice();

    resetStaleRun(sessionState);

    // After reset: live run state is cleared
    expect(sessionState.status).toBe(CHAT_STATUS_IDLE);
    expect(sessionState.streamStatus).toBe(CHAT_STATUS_IDLE);
    expect(sessionState.currentRun).toBeNull();
    expect(sessionState.streamingRunEvents).toEqual([]);
    expect(sessionState.streamingPhase).toBe(0);
    expect(sessionState.seenStreamingEventKeys).toEqual(new Set());

    // History source (about to be reloaded) is preserved
    expect(sessionState.runEvents).toEqual(runEventsBefore);

    // canCreateNewSession now allows a new session because no run is active
    expect(canCreateNewSession(sessionState)).toBe(true);
    expect(isRunActive(sessionState)).toBe(false);
  });

  it('resetStaleRun leaves loaded messages intact', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-stale-history',
    );

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Hi' },
      { id: 'assistant-one', role: 'assistant', content: 'Hello!' },
    ]);
    startRun(sessionState, {
      run_id: 'run-history',
      sse_url: '/api/runs/run-history/events',
      status: CHAT_STATUS_RUNNING,
      events: [
        {
          sequence: 1,
          run_id: 'run-history',
          type: 'run_started',
          payload: { status: CHAT_STATUS_RUNNING },
        },
      ],
    });

    const messagesBefore = sessionState.messages;
    const runEventsBefore = sessionState.runEvents.slice();

    resetStaleRun(sessionState);

    expect(sessionState.messages).toBe(messagesBefore);
    expect(sessionState.messages).toEqual([
      { id: 'user-one', role: 'user', content: 'Hi' },
      { id: 'assistant-one', role: 'assistant', content: 'Hello!' },
    ]);
    expect(sessionState.runEvents).toEqual(runEventsBefore);
    expect(sessionState.status).toBe(CHAT_STATUS_IDLE);
    expect(sessionState.currentRun).toBeNull();
  });
});
