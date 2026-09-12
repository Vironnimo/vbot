import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  createChatState,
  ensureSessionState,
  setAgents,
  visibleTimelineItemsForRender,
} from '../chatState.js';
import { makeStreamHarness } from './chatRunStream.support.js';

describe('createChatRunStream() last-tool-name tracking for sub-agent rows', () => {
  let chatState;
  const DISPLAYED_AGENT_ID = 'alpha';
  const DISPLAYED_SESSION_ID = 'session-displayed';
  const CHILD_AGENT_ID = 'child-agent';
  const CHILD_SESSION_ID = 'session-child';
  const CHILD_RUN_ID = 'run-child-7';

  const childToolCallStartedEvent = (toolName, sequence = 2) => ({
    type: 'run_output',
    payload: {
      run_id: CHILD_RUN_ID,
      agent_id: CHILD_AGENT_ID,
      session_id: CHILD_SESSION_ID,
      run_event_type: 'tool_call_started',
      run_event_sequence: sequence,
      output: {
        tool_call: {
          id: `call-${sequence}`,
          index: 0,
          name: toolName,
          arguments: {},
        },
      },
    },
  });

  beforeEach(() => {
    chatState = createChatState();
    setAgents(chatState, [
      {
        id: DISPLAYED_AGENT_ID,
        name: 'Alpha',
        current_session_id: DISPLAYED_SESSION_ID,
      },
    ]);
  });

  it('records runTool and sessionTool entries from a bridged child tool_call_started event, keeping only the latest name', () => {
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: DISPLAYED_AGENT_ID,
      displayedSessionId: DISPLAYED_SESSION_ID,
    });

    harness.stream.handleServerEvents(childToolCallStartedEvent('read', 2));
    expect(harness.subAgentRunStatuses[`runTool:${CHILD_RUN_ID}`]).toBe('read');
    expect(
      harness.subAgentRunStatuses[
        `sessionTool:${CHILD_AGENT_ID}::${CHILD_SESSION_ID}`
      ],
    ).toBe('read');

    harness.stream.handleServerEvents(childToolCallStartedEvent('bash', 5));
    expect(harness.subAgentRunStatuses[`runTool:${CHILD_RUN_ID}`]).toBe('bash');
    expect(
      harness.subAgentRunStatuses[
        `sessionTool:${CHILD_AGENT_ID}::${CHILD_SESSION_ID}`
      ],
    ).toBe('bash');
  });

  it('records no tool entries when the tool_call_started payload has no usable name', () => {
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: DISPLAYED_AGENT_ID,
      displayedSessionId: DISPLAYED_SESSION_ID,
    });

    const event = childToolCallStartedEvent('  ', 2);
    harness.stream.handleServerEvents(event);

    expect(harness.subAgentRunStatuses).toEqual({});
  });

  it('clears the session-scoped tool name when a new run starts in the same child session', () => {
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: DISPLAYED_AGENT_ID,
      displayedSessionId: DISPLAYED_SESSION_ID,
    });

    harness.stream.handleServerEvents(childToolCallStartedEvent('bash', 2));
    expect(
      harness.subAgentRunStatuses[
        `sessionTool:${CHILD_AGENT_ID}::${CHILD_SESSION_ID}`
      ],
    ).toBe('bash');

    harness.stream.handleServerEvents({
      type: 'run_started',
      payload: {
        run_id: 'run-child-8',
        agent_id: CHILD_AGENT_ID,
        session_id: CHILD_SESSION_ID,
        run_event_type: 'run_started',
        run_event_sequence: 1,
        status: 'running',
        output: { status: 'running' },
      },
    });

    expect(
      harness.subAgentRunStatuses[
        `sessionTool:${CHILD_AGENT_ID}::${CHILD_SESSION_ID}`
      ],
    ).toBe('');
    // The previous run's run-scoped entry stays untouched; rows resolve it
    // strictly by run id, so it cannot leak into the new run's row.
    expect(harness.subAgentRunStatuses[`runTool:${CHILD_RUN_ID}`]).toBe('bash');
  });
});

describe('createChatRunStream() project-agent address reconstruction', () => {
  let chatState;
  const PROJECT_AGENT_ADDRESS = 'builder@vbot';
  const BARE_AGENT_ID = 'builder';
  const PROJECT_ID = 'vbot';
  const SESSION_ID = 'sess-project-1';
  const RUN_ID = 'run-project-1';

  beforeEach(() => {
    chatState = createChatState();
  });

  it('keys a project run server-event by the rebuilt agent@projekt address so the displayed project session matches and the backstop re-attaches', () => {
    const subscribeRunEvents = vi.fn(() => ({ close: vi.fn() }));
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: PROJECT_AGENT_ADDRESS,
      displayedSessionId: SESSION_ID,
      subscribeRunEvents,
    });

    harness.stream.handleServerEvents({
      type: 'run_started',
      payload: {
        run_id: RUN_ID,
        agent_id: BARE_AGENT_ID,
        project_id: PROJECT_ID,
        session_id: SESSION_ID,
        run_event_type: 'run_started',
        run_event_sequence: 1,
        status: 'running',
        output: { status: 'running' },
      },
    });

    // Session state lands under the rebuilt address, not the bare id — so it
    // matches the address-keyed displayed session instead of an orphan.
    expect(
      chatState.sessions[`${PROJECT_AGENT_ADDRESS}::${SESSION_ID}`],
    ).toBeTruthy();
    expect(
      chatState.sessions[`${BARE_AGENT_ID}::${SESSION_ID}`],
    ).toBeUndefined();
    // The status projection keys stay BARE: persisted spawn descriptors carry
    // the child's bare id, so bare keys are the only ones their reads meet.
    expect(
      harness.subAgentRunStatuses[`session:${BARE_AGENT_ID}::${SESSION_ID}`],
    ).toBe('running');
    expect(
      harness.subAgentRunStatuses[
        `session:${PROJECT_AGENT_ADDRESS}::${SESSION_ID}`
      ],
    ).toBeUndefined();
    expect(harness.isDisplayedSession).toHaveBeenCalledWith(
      PROJECT_AGENT_ADDRESS,
      SESSION_ID,
    );
    expect(subscribeRunEvents).toHaveBeenCalledTimes(1);
  });

  it('rebuilds the address from a snapshot active run for the re-attach while keeping the sub-agent status key bare', () => {
    const subscribeRunEvents = vi.fn(() => ({ close: vi.fn() }));
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: PROJECT_AGENT_ADDRESS,
      displayedSessionId: SESSION_ID,
      subscribeRunEvents,
    });

    harness.stream.applyConnectionSnapshot({
      type: 'connection_ready',
      epoch: 'epoch-project',
      last_sequence: 0,
      active_runs: [
        {
          run_id: RUN_ID,
          agent_id: BARE_AGENT_ID,
          project_id: PROJECT_ID,
          session_id: SESSION_ID,
          status: 'running',
          sse_url: `/api/runs/${RUN_ID}/events`,
        },
      ],
    });

    // Status key bare (descriptor-compatible); session STATE stays
    // address-keyed for the displayed-session match below.
    expect(
      harness.subAgentRunStatuses[`session:${BARE_AGENT_ID}::${SESSION_ID}`],
    ).toBe('running');
    expect(harness.isDisplayedSession).toHaveBeenCalledWith(
      PROJECT_AGENT_ADDRESS,
      SESSION_ID,
    );
    expect(subscribeRunEvents).toHaveBeenCalledTimes(1);
    expect(
      chatState.sessions[`${PROJECT_AGENT_ADDRESS}::${SESSION_ID}`],
    ).toBeTruthy();
  });

  it('keys an identity run (no project_id) by the bare id, byte-identical to today', () => {
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: BARE_AGENT_ID,
      displayedSessionId: SESSION_ID,
    });

    harness.stream.handleServerEvents({
      type: 'run_started',
      payload: {
        run_id: RUN_ID,
        agent_id: BARE_AGENT_ID,
        session_id: SESSION_ID,
        run_event_type: 'run_started',
        run_event_sequence: 1,
        status: 'running',
        output: { status: 'running' },
      },
    });

    expect(chatState.sessions[`${BARE_AGENT_ID}::${SESSION_ID}`]).toBeTruthy();
    expect(
      harness.subAgentRunStatuses[`session:${BARE_AGENT_ID}::${SESSION_ID}`],
    ).toBe('running');
  });
});

describe('createChatRunStream() tool output chunk batching', () => {
  const DISPLAYED_AGENT_ID = 'alpha';
  const DISPLAYED_SESSION_ID = 'session-displayed';

  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('batches tool stdout chunks into the streaming flush instead of appending each one eagerly', async () => {
    const chatState = createChatState();
    let onEvent;
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: DISPLAYED_AGENT_ID,
      displayedSessionId: DISPLAYED_SESSION_ID,
      subscribeRunEvents: vi.fn((_url, handlers) => {
        onEvent = handlers.onEvent;
        return { close: vi.fn() };
      }),
    });

    harness.stream.handleServerEvents({
      type: 'run_started',
      payload: {
        run_id: 'run-chunks',
        agent_id: DISPLAYED_AGENT_ID,
        session_id: DISPLAYED_SESSION_ID,
        run_event_type: 'run_started',
        run_event_sequence: 1,
        status: 'running',
        output: { status: 'running' },
      },
    });

    onEvent({
      data: {
        type: 'tool_call_started',
        run_id: 'run-chunks',
        sequence: 2,
        payload: {
          tool_call: {
            id: 'call-one',
            index: 0,
            name: 'bash',
            arguments: { command: 'x' },
          },
        },
      },
    });
    onEvent({
      data: {
        type: 'tool_call_stdout',
        run_id: 'run-chunks',
        sequence: 3,
        payload: { tool_call_id: 'call-one', data: 'chunk-one' },
      },
    });

    const sessionState = ensureSessionState(
      chatState,
      DISPLAYED_AGENT_ID,
      DISPLAYED_SESSION_ID,
    );
    // The non-delayed tool_call_started flushed immediately; the stdout
    // chunk rides the ~33 ms streaming flush.
    expect(sessionState.streamingRunEvents).toHaveLength(0);

    await vi.advanceTimersByTimeAsync(50);

    expect(sessionState.streamingRunEvents).toEqual([
      expect.objectContaining({
        type: 'tool_call_stdout',
        payload: expect.objectContaining({ data: 'chunk-one' }),
      }),
    ]);
    const [assistantRun] = visibleTimelineItemsForRender(sessionState);
    expect(assistantRun.tools[0]).toEqual(
      expect.objectContaining({ toolCallId: 'call-one', stdout: 'chunk-one' }),
    );
  });
});

describe('reflection review tracking', () => {
  let chatState;
  const SOURCE_SESSION_ID = 'session-source';

  beforeEach(() => {
    chatState = createChatState();
  });

  function reflectionServerEvent(type, overrides = {}) {
    return {
      type,
      payload: {
        run_id: 'run-refl-1',
        agent_id: 'alpha',
        project_id: null,
        session_id: 'session-fork',
        run_kind: 'memory_reflection',
        run_event_type: type,
        run_event_sequence: 1,
        run_event_timestamp: '2026-08-24T10:00:00.000Z',
        contributes_to_agent_activity: false,
        source_session_id: SOURCE_SESSION_ID,
        status: type === 'run_started' ? 'running' : 'completed',
        ...overrides,
      },
    };
  }

  function sourceReflectionTasks() {
    return chatState.sessions[`${'alpha'}::${SOURCE_SESSION_ID}`]
      ?.reflectionTasks;
  }

  it('creates a running tracking entry on the reviewed source session', () => {
    const harness = makeStreamHarness({ chatState });

    harness.stream.handleServerEvents(reflectionServerEvent('run_started'));

    expect(sourceReflectionTasks()).toEqual({
      'run-refl-1': {
        sessionId: 'session-fork',
        runKind: 'memory_reflection',
        status: 'running',
        startedAt: '2026-08-24T10:00:00.000Z',
      },
    });
  });

  it('settles the entry on a terminal event and preserves the start timestamp', () => {
    const harness = makeStreamHarness({ chatState });

    harness.stream.handleServerEvents(reflectionServerEvent('run_started'));
    harness.stream.handleServerEvents(
      reflectionServerEvent('run_completed', {
        run_event_sequence: 2,
        run_event_timestamp: '2026-08-24T10:05:00.000Z',
      }),
    );

    expect(sourceReflectionTasks()['run-refl-1']).toMatchObject({
      sessionId: 'session-fork',
      status: 'completed',
      startedAt: '2026-08-24T10:00:00.000Z',
    });
  });

  it('ignores non-reflection runs and reflection events without source provenance', () => {
    const harness = makeStreamHarness({ chatState });
    const userRunEvent = {
      type: 'run_started',
      payload: {
        run_id: 'run-user',
        agent_id: 'alpha',
        session_id: SOURCE_SESSION_ID,
        run_kind: 'user',
        run_event_type: 'run_started',
        run_event_sequence: 1,
        run_event_timestamp: '2026-08-24T10:00:00.000Z',
      },
    };

    harness.stream.handleServerEvents(userRunEvent);
    harness.stream.handleServerEvents(
      reflectionServerEvent('run_started', { source_session_id: '' }),
    );

    expect(chatState.sessions[`alpha::${SOURCE_SESSION_ID}`]).toBeTruthy();
    expect(
      chatState.sessions[`alpha::${SOURCE_SESSION_ID}`].reflectionTasks,
    ).toEqual({});
  });

  it('seeds running reflections from the connection snapshot, drops stale running entries, and keeps finished ones', () => {
    const harness = makeStreamHarness({ chatState });
    const source = ensureSessionState(chatState, 'alpha', SOURCE_SESSION_ID);
    source.reflectionTasks = {
      'run-stale': {
        sessionId: 'session-fork-old',
        runKind: 'skill_reflection',
        status: 'running',
        startedAt: '2026-08-24T09:00:00.000Z',
      },
      'run-finished': {
        sessionId: 'session-fork-done',
        runKind: 'memory_reflection',
        status: 'completed',
        startedAt: '2026-08-24T08:00:00.000Z',
      },
    };

    harness.stream.applyConnectionSnapshot({
      type: 'connection_ready',
      active_runs: [
        {
          run_id: 'run-refl-live',
          agent_id: 'alpha',
          session_id: 'session-fork',
          run_kind: 'memory_reflection',
          source_session_id: SOURCE_SESSION_ID,
          status: 'running',
          started_at: '2026-08-24T11:00:00.000Z',
          sse_url: '/api/runs/run-refl-live/events',
          contributes_to_agent_activity: false,
        },
      ],
    });

    expect(source.reflectionTasks).toEqual({
      'run-refl-live': {
        sessionId: 'session-fork',
        runKind: 'memory_reflection',
        status: 'running',
        startedAt: '2026-08-24T11:00:00.000Z',
      },
      'run-finished': {
        sessionId: 'session-fork-done',
        runKind: 'memory_reflection',
        status: 'completed',
        startedAt: '2026-08-24T08:00:00.000Z',
      },
    });
  });
});
