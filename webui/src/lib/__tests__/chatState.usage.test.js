import { describe, expect, it, vi } from 'vitest';
import {
  agentActivityStatus,
  appendRunEvent,
  contextCompactionState,
  createChatState,
  ensureSessionState,
  loadHistory,
  startRun,
  visibleTimelineItemsForRender,
} from '../chatState.js';
import { setupController } from './chatState.support.js';

describe('turn usage from Run events', () => {
  const measuredSessionUsage = {
    measured_turns: 5,
    estimated_turns: 1,
    cache_turns: 5,
    input_tokens: 9000,
    output_tokens: 800,
    cache_read_tokens: 7200,
    cache_write_tokens: 400,
  };
  const measuredContextUsage = { tokens: 10200, estimated: false };

  it.each([
    {
      name: 'run_completed with turn usage',
      type: 'run_completed',
      payload: {
        status: 'completed',
        usage: { input_tokens: 8432, output_tokens: 512 },
      },
      usage: { input_tokens: 8432, output_tokens: 512 },
    },
    {
      name: 'run_completed with estimated turn usage',
      type: 'run_completed',
      payload: {
        status: 'completed',
        usage: { input_tokens: 500, output_tokens: 200, estimated: true },
      },
      usage: { input_tokens: 500, output_tokens: 200, estimated: true },
    },
    {
      name: 'run_completed without usage',
      type: 'run_completed',
      payload: { status: 'completed' },
      usage: null,
    },
    {
      name: 'run_failed with Session and Context Usage',
      type: 'run_failed',
      payload: {
        status: 'failed',
        error: 'boom',
        session_usage: measuredSessionUsage,
        context_usage: measuredContextUsage,
      },
      usage: null,
      sessionUsage: measuredSessionUsage,
      contextUsage: measuredContextUsage,
    },
  ])(
    'applies usage from $name',
    ({ type, payload, usage, sessionUsage = null, contextUsage = null }) => {
      const chatState = createChatState();
      const sessionState = ensureSessionState(
        chatState,
        'alpha',
        'session-one',
      );
      expect([
        sessionState.usage,
        sessionState.sessionUsage,
        sessionState.contextUsage,
      ]).toEqual([null, null, null]);
      startRun(sessionState, {
        run_id: 'run-one',
        sse_url: '/api/runs/run-one/events',
        status: 'running',
      });

      appendRunEvent(sessionState, {
        type,
        run_id: 'run-one',
        sequence: 2,
        payload,
      });

      expect(sessionState.usage).toEqual(usage);
      expect(sessionState.sessionUsage).toEqual(sessionUsage);
      expect(sessionState.contextUsage).toEqual(contextUsage);
      expect(agentActivityStatus(chatState, 'alpha')).toBe('unread');
    },
  );

  it('updates token usage after a completed model step while the run stays active', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    startRun(sessionState, {
      run_id: 'run-one',
      sse_url: '/api/runs/run-one/events',
      status: 'running',
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
    expect(sessionState.status).toBe('running');
    expect(sessionState.currentRun.status).toBe('running');
    expect(visibleTimelineItemsForRender(sessionState)).toEqual([]);
  });
});

describe('usage from History', () => {
  it('fills Session Usage from History and keeps it when a later load has none', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
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

    loadHistory(sessionState, [], {});
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

  it.each([
    {
      name: 'the last assistant message usage',
      liveUsage: null,
      messages: [
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
      ],
      usage: { input_tokens: 200, output_tokens: 75 },
    },
    {
      name: 'no usage without assistant usage',
      liveUsage: null,
      messages: [
        { id: 'user-one', role: 'user', content: 'Hi' },
        { id: 'assistant-one', role: 'assistant', content: 'Hello!' },
      ],
      usage: null,
    },
    {
      name: 'the completed Run usage when History has none',
      liveUsage: { input_tokens: 8432, output_tokens: 512 },
      messages: [
        { id: 'user-one', role: 'user', content: 'Hi' },
        { id: 'assistant-one', role: 'assistant', content: 'Hello!' },
      ],
      usage: { input_tokens: 8432, output_tokens: 512 },
    },
  ])('shows $name after loading History', ({ liveUsage, messages, usage }) => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    if (liveUsage) {
      startRun(sessionState, {
        run_id: 'run-one',
        sse_url: '/api/runs/run-one/events',
        status: 'running',
      });
      appendRunEvent(sessionState, {
        type: 'run_completed',
        run_id: 'run-one',
        sequence: 2,
        payload: { status: 'completed', usage: liveUsage },
      });
    }

    loadHistory(sessionState, messages);

    expect(sessionState.usage).toEqual(usage);
  });
});

describe('contextCompactionState', () => {
  function sessionWithRun(controls) {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-compaction',
    );
    startRun(sessionState, {
      run_id: 'run-compaction',
      status: 'running',
      controls,
      events: [],
    });
    return sessionState;
  }

  it('offers a manual Compaction Run while no Run is active', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-idle',
    );

    expect(contextCompactionState(sessionState)).toBe('idle');
    expect(contextCompactionState(null)).toBe('unavailable');
  });

  it.each(['pending', 'unavailable'])(
    'follows the running Run advertised %s control',
    (compaction) => {
      expect(contextCompactionState(sessionWithRun({ compaction }))).toBe(
        compaction,
      );
    },
  );

  it('reports a manual Compaction Run as running until its Compaction settles', () => {
    const sessionState = sessionWithRun({ compaction: 'unavailable' });

    appendRunEvent(sessionState, {
      type: 'compaction_started',
      run_id: 'run-compaction',
      sequence: 1,
      payload: {},
    });
    expect(contextCompactionState(sessionState)).toBe('running');

    appendRunEvent(sessionState, {
      type: 'compaction_completed',
      run_id: 'run-compaction',
      sequence: 2,
      payload: {},
    });
    expect(contextCompactionState(sessionState)).toBe('unavailable');
  });

  it('keeps the Session Compaction Policy from History and applies saved changes', async () => {
    const historyPolicy = {
      enabled: true,
      trigger: { type: 'context_ratio', threshold: 0.6 },
      strategy: { type: 'continuation' },
    };
    const loadChatHistory = vi
      .fn()
      .mockResolvedValueOnce({
        has_more: false,
        messages: [],
        compaction_policy: historyPolicy,
      })
      .mockResolvedValueOnce({ has_more: false, messages: [] });
    const { chatState, controller } = setupController({
      isDisplayedSession: () => true,
      operationOverrides: { loadChatHistory },
    });
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    expect(sessionState.compactionPolicy).toBeNull();

    await controller.loadHistoryForSession('alpha', 'session-one');
    expect(sessionState.compactionPolicy).toEqual(historyPolicy);

    // An older-page style response without a Policy keeps the known one.
    await controller.loadHistoryForSession('alpha', 'session-one');
    expect(sessionState.compactionPolicy).toEqual(historyPolicy);

    const savedPolicy = { ...historyPolicy, enabled: false };
    controller.applySessionCompactionPolicy(
      'alpha',
      'session-one',
      savedPolicy,
    );
    expect(sessionState.compactionPolicy).toEqual(savedPolicy);
    controller.applySessionCompactionPolicy('alpha', 'other', historyPolicy);
    expect(chatState.sessions['alpha::other']).toBeUndefined();
  });
});
