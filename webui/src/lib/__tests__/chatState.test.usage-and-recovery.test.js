import { describe, expect, it } from 'vitest';

import {
  CHAT_STATUS_COMPLETED,
  CHAT_STATUS_FAILED,
  CHAT_STATUS_IDLE,
  CHAT_STATUS_RUNNING,
  AGENT_ACTIVITY_IDLE,
  AGENT_ACTIVITY_RUNNING,
  AGENT_ACTIVITY_UNREAD,
  agentActivityStatus,
  applySessionCompletionActivity,
  appendRunEvent,
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
  it('initializes turn, Session, and Context Usage as null', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    expect(sessionState.usage).toBeNull();
    expect(sessionState.sessionUsage).toBeNull();
    expect(sessionState.contextUsage).toBeNull();
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

  it('does not project the displayed Session unread while preserving other unread results', () => {
    const chatState = createChatState();
    const displayed = ensureSessionState(chatState, 'alpha', 'session-shown');
    const background = ensureSessionState(
      chatState,
      'alpha',
      'session-background',
    );
    displayed.hasUnreadCompletion = true;

    expect(agentActivityStatus(chatState, 'alpha', displayed.key)).toBe(
      AGENT_ACTIVITY_IDLE,
    );

    background.hasUnreadCompletion = true;
    expect(agentActivityStatus(chatState, 'alpha', displayed.key)).toBe(
      AGENT_ACTIVITY_UNREAD,
    );
  });

  it.each([
    ['run_completed', 'completed'],
    ['run_failed', 'failed'],
    ['run_cancelled', 'cancelled'],
    ['run_interrupted', 'interrupted'],
  ])(
    'keeps an excluded Run out of Agent activity through %s while retaining its timeline',
    (terminalType, terminalStatus) => {
      const chatState = createChatState();
      const sessionState = ensureSessionState(
        chatState,
        'alpha',
        'session-system',
      );

      appendRunEvent(sessionState, {
        type: 'run_started',
        run_id: 'run-system',
        sequence: 1,
        contributes_to_agent_activity: false,
        payload: { status: CHAT_STATUS_RUNNING },
      });

      expect(isRunActive(sessionState)).toBe(true);
      expect(sessionState.currentRun?.contributesToAgentActivity).toBe(false);
      expect(agentActivityStatus(chatState, 'alpha')).toBe(AGENT_ACTIVITY_IDLE);

      appendRunEvent(sessionState, {
        type: terminalType,
        run_id: 'run-system',
        sequence: 2,
        contributes_to_agent_activity: false,
        payload: { status: terminalStatus },
      });

      expect(sessionState.status).toBe(terminalStatus);
      expect(sessionState.hasUnreadCompletion).toBe(false);
      expect(sessionState.latestCompletionRunId).toBe('');
      expect(agentActivityStatus(chatState, 'alpha')).toBe(AGENT_ACTIVITY_IDLE);
      expect(sessionState.runEvents.map((event) => event.type)).toEqual([
        'run_started',
        terminalType,
      ]);
    },
  );

  it('accepts the exact backend read state after a terminal event is replayed', () => {
    const chatState = createChatState();
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    sessionState.hasUnreadCompletion = true;
    sessionState.unreadRunId = 'run-one';
    sessionState.unreadRunAt = '2026-07-20T10:00:00+00:00';

    applySessionCompletionActivity(sessionState, {
      last_active_at: '2026-07-20T10:00:00+00:00',
      latest_completion_run_id: 'run-one',
      has_unread_completion: false,
      unread_run_id: null,
      unread_run_status: null,
      unread_run_at: null,
    });

    expect(sessionState.hasUnreadCompletion).toBe(false);
    expect(sessionState.unreadRunId).toBe('');
  });

  it('does not let a retained terminal event overwrite an exact backend read state', () => {
    const chatState = createChatState();
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    applySessionCompletionActivity(sessionState, {
      last_active_at: '2026-07-20T10:00:00+00:00',
      latest_completion_run_id: 'run-one',
      has_unread_completion: false,
      unread_run_id: null,
      unread_run_status: null,
      unread_run_at: null,
    });

    appendRunEvent(sessionState, {
      type: 'run_completed',
      run_id: 'run-one',
      sequence: 2,
      timestamp: '2026-07-20T10:00:00+00:00',
      payload: { status: CHAT_STATUS_COMPLETED },
    });

    expect(sessionState.hasUnreadCompletion).toBe(false);
    expect(sessionState.unreadRunId).toBe('');
  });

  it('does not let a stale unread listing resurrect the exact backend-read run', () => {
    const chatState = createChatState();
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    applySessionCompletionActivity(sessionState, {
      last_active_at: '2026-07-20T10:00:00+00:00',
      latest_completion_run_id: 'run-one',
      has_unread_completion: false,
      unread_run_id: null,
      unread_run_status: null,
      unread_run_at: null,
    });

    applySessionCompletionActivity(sessionState, {
      last_active_at: '2026-07-20T10:00:00+00:00',
      latest_completion_run_id: 'run-one',
      has_unread_completion: true,
      unread_run_id: 'run-one',
      unread_run_status: CHAT_STATUS_COMPLETED,
      unread_run_at: '2026-07-20T10:00:00+00:00',
    });

    expect(sessionState.hasUnreadCompletion).toBe(false);
    expect(sessionState.unreadRunId).toBe('');
  });

  it('keeps a newer local completion when an older clean listing arrives', () => {
    const chatState = createChatState();
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    sessionState.latestCompletionRunId = 'run-new';
    sessionState.hasUnreadCompletion = true;
    sessionState.unreadRunId = 'run-new';
    sessionState.unreadRunAt = '2026-07-20T10:05:00+00:00';

    applySessionCompletionActivity(sessionState, {
      last_active_at: '2026-07-20T10:00:00+00:00',
      latest_completion_run_id: 'run-old',
      has_unread_completion: false,
      unread_run_id: null,
      unread_run_status: null,
      unread_run_at: null,
    });

    expect(sessionState.hasUnreadCompletion).toBe(true);
    expect(sessionState.unreadRunId).toBe('run-new');
  });

  it('keeps a local completion when a different listing has the same timestamp', () => {
    const chatState = createChatState();
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    sessionState.latestCompletionRunId = 'run-local';
    sessionState.hasUnreadCompletion = true;
    sessionState.unreadRunId = 'run-local';
    sessionState.unreadRunAt = '2026-07-20T10:05:00+00:00';

    applySessionCompletionActivity(sessionState, {
      last_active_at: '2026-07-20T10:05:00+00:00',
      latest_completion_run_id: 'run-listing',
      has_unread_completion: true,
      unread_run_id: 'run-listing',
      unread_run_status: CHAT_STATUS_COMPLETED,
      unread_run_at: '2026-07-20T10:05:00+00:00',
    });

    expect(sessionState.hasUnreadCompletion).toBe(true);
    expect(sessionState.unreadRunId).toBe('run-local');
  });

  it('accepts a genuinely newer unread completion from the backend', () => {
    const chatState = createChatState();
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    applySessionCompletionActivity(sessionState, {
      last_active_at: '2026-07-20T10:00:00+00:00',
      latest_completion_run_id: 'run-old',
      has_unread_completion: false,
      unread_run_id: null,
      unread_run_status: null,
      unread_run_at: null,
    });

    applySessionCompletionActivity(sessionState, {
      last_active_at: '2026-07-20T10:05:00+00:00',
      latest_completion_run_id: 'run-new',
      has_unread_completion: true,
      unread_run_id: 'run-new',
      unread_run_status: CHAT_STATUS_COMPLETED,
      unread_run_at: '2026-07-20T10:05:00+00:00',
    });

    expect(sessionState.hasUnreadCompletion).toBe(true);
    expect(sessionState.unreadRunId).toBe('run-new');
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
    const contextUsage = {
      tokens: 9459,
      estimated: true,
      provider_input_tokens: 8432,
      provider_output_tokens: 512,
      estimated_delta_tokens: 515,
    };

    appendRunEvent(sessionState, {
      type: 'model_step_usage',
      run_id: 'run-one',
      sequence: 2,
      payload: {
        usage: { input_tokens: 8432, output_tokens: 512 },
        session_usage: sessionUsage,
        context_usage: contextUsage,
      },
    });

    expect(sessionState.usage).toEqual({
      input_tokens: 8432,
      output_tokens: 512,
    });
    expect(sessionState.sessionUsage).toEqual(sessionUsage);
    expect(sessionState.contextUsage).toEqual(contextUsage);
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

  it('fills Context Usage from authoritative History and clears it when absent', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    const contextUsage = { tokens: 155489, estimated: true };

    loadHistory(sessionState, [], { contextUsage });
    expect(sessionState.contextUsage).toEqual(contextUsage);

    loadHistory(sessionState, [], { contextUsage: undefined });
    expect(sessionState.contextUsage).toBeNull();
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
    const contextUsage = { tokens: 10200, estimated: false };
    appendRunEvent(sessionState, {
      type: 'run_failed',
      run_id: 'run-one',
      sequence: 2,
      payload: {
        status: CHAT_STATUS_FAILED,
        error: 'boom',
        session_usage: sessionUsage,
        context_usage: contextUsage,
      },
    });

    expect(sessionState.sessionUsage).toEqual(sessionUsage);
    expect(sessionState.contextUsage).toEqual(contextUsage);
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

  it('resetStaleRun clears live Run replay after History settles the Session', () => {
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
      startedAt: null,
      iterationCount: 0,
    });
    expect(sessionState.streamingRunEvents).not.toHaveLength(0);
    expect(sessionState.seenStreamingEventKeys.size).toBeGreaterThan(0);
    expect(sessionState.streamingPhase).toBeGreaterThan(0);
    expect(sessionState.runEvents).not.toHaveLength(0);
    expect(sessionState.messages).toEqual([]);

    resetStaleRun(sessionState);

    // After reset: live run state is cleared
    expect(sessionState.status).toBe(CHAT_STATUS_IDLE);
    expect(sessionState.streamStatus).toBe(CHAT_STATUS_IDLE);
    expect(sessionState.currentRun).toBeNull();
    expect(sessionState.streamingRunEvents).toEqual([]);
    expect(sessionState.streamingPhase).toBe(0);
    expect(sessionState.seenStreamingEventKeys).toEqual(new Set());

    // Freshly loaded History is authoritative; no sparse replay may be
    // appended behind it after the stale Run marker is removed.
    expect(sessionState.runEvents).toEqual([]);

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
    resetStaleRun(sessionState);

    expect(sessionState.messages).toBe(messagesBefore);
    expect(sessionState.messages).toEqual([
      { id: 'user-one', role: 'user', content: 'Hi' },
      { id: 'assistant-one', role: 'assistant', content: 'Hello!' },
    ]);
    expect(sessionState.runEvents).toEqual([]);
    expect(sessionState.status).toBe(CHAT_STATUS_IDLE);
    expect(sessionState.currentRun).toBeNull();
  });
});
