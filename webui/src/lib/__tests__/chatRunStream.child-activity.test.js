import { describe, expect, it } from 'vitest';

import { ensureSessionState } from '../chatState.js';
import {
  subAgentDotStatus,
  subAgentLastToolName,
  subAgentToolStatusLabel,
} from '../chatTimelinePresentation.js';
import { t } from '../i18n.js';
import {
  DISPLAYED_AGENT_ID,
  DISPLAYED_SESSION_ID,
  activeRun,
  makeStreamHarness,
  serverRunEvent,
} from './chatRunStream.support.js';

describe('Sub-Agent row statuses', () => {
  const child = {
    run_id: 'run-child-7',
    agent_id: 'child-agent',
    session_id: 'session-child',
  };
  const runTool = `runTool:${child.run_id}`;
  const sessionTool = `sessionTool:child-agent::session-child`;
  const toolStarted = (name, sequence) =>
    serverRunEvent('tool_call_started', sequence, {
      ...child,
      output: {
        tool_call: { id: `call-${sequence}`, index: 0, name, arguments: {} },
      },
    });

  it('tracks the latest named Tool of a child Run and clears the Session entry when a new Run starts', () => {
    const harness = makeStreamHarness();

    harness.stream.handleServerEvents(toolStarted('  ', 2));
    expect(harness.subAgentRunStatuses).toEqual({});

    harness.stream.handleServerEvents(toolStarted('read', 3));
    harness.stream.handleServerEvents(toolStarted('bash', 5));
    expect(harness.subAgentRunStatuses).toMatchObject({
      [runTool]: 'bash',
      [sessionTool]: 'bash',
    });

    harness.stream.handleServerEvents(
      serverRunEvent('run_started', 1, {
        ...child,
        run_id: 'run-child-8',
        status: 'running',
        output: { status: 'running' },
      }),
    );

    expect(harness.subAgentRunStatuses[sessionTool]).toBe('');
    // The previous Run's entry stays; rows resolve it strictly by run id, so
    // it cannot leak into the new Run's row.
    expect(harness.subAgentRunStatuses[runTool]).toBe('bash');
  });

  it('projects interrupted Run status and duration', () => {
    const harness = makeStreamHarness();

    harness.stream.handleServerEvents(
      serverRunEvent('run_interrupted', 3, {
        run_id: 'run-interrupted-1',
        agent_id: 'worker',
        session_id: 'session-child',
        status: 'interrupted',
        cause: 'network',
        timing: { duration_ms: 2500 },
      }),
    );

    expect(harness.subAgentRunStatuses).toMatchObject({
      'run:run-interrupted-1': 'interrupted',
      'session:worker::session-child': 'interrupted',
      'runDuration:run-interrupted-1': 2500,
      'sessionDuration:worker::session-child': 2500,
    });
  });

  it('maps a drained queued Sub-Agent to the Run its run_started announces', () => {
    const harness = makeStreamHarness();

    harness.stream.handleServerEvents(
      serverRunEvent('run_started', 1, {
        run_id: 'run-drained-1',
        agent_id: DISPLAYED_AGENT_ID,
        session_id: DISPLAYED_SESSION_ID,
        status: 'running',
        output: { status: 'running', queue_item_id: 'queue-item-42' },
      }),
    );

    expect(harness.subAgentRunStatuses).toMatchObject({
      'queueRun:queue-item-42': 'run-drained-1',
      'run:run-drained-1': 'running',
    });
  });

  it('projects an explicit Parent-Agent cancellation onto the exact child row', () => {
    const harness = makeStreamHarness();

    harness.stream.handleServerEvents(
      serverRunEvent('subagent_status_changed', 4, {
        run_id: 'parent-run-two',
        agent_id: 'parent',
        session_id: 'parent-session',
        contributes_to_agent_activity: false,
        output: {
          data: {
            agent_id: DISPLAYED_AGENT_ID,
            session_id: DISPLAYED_SESSION_ID,
            run_id: 'run-drained-1',
            queue_item_id: 'queue-item-42',
            status: 'cancelled',
          },
        },
      }),
    );

    expect(harness.subAgentRunStatuses).toMatchObject({
      'run:run-drained-1': 'cancelled',
      'queue:queue-item-42': 'cancelled',
      'queueRun:queue-item-42': 'run-drained-1',
      [`session:${DISPLAYED_AGENT_ID}::${DISPLAYED_SESSION_ID}`]: 'cancelled',
    });
  });
});

describe('Sub-Agent rows without a run id', () => {
  const PARENT_AGENT_ID = 'lead@vbot';
  const PARENT_SESSION_ID = 'session-parent';
  const CHILD_SESSION_ID = 'session-child';
  const CHILD_RUN_ID = 'run-child';
  const STARTED_AT = '2026-09-24T10:00:00.000Z';

  // A persisted background spawn row: public results never carry the child's
  // run id, so until inspection maps the work id the row reads its status,
  // start time, duration, and last Tool through session-scoped keys.
  const spawnRow = (resultIdentity) => ({
    type: 'tool_call',
    name: 'subagent',
    status: 'success',
    arguments: {
      action: 'run',
      agent_id: resultIdentity.agent_id,
      content: 'Work in the background',
      background: true,
    },
    result: {
      ok: true,
      data: {
        id: 'sub-work',
        session_id: CHILD_SESSION_ID,
        status: 'running',
        delivery: 'automatic',
        ...resultIdentity,
      },
    },
  });

  it.each([
    {
      name: 'Project child, current result, WebSocket',
      result: { agent_id: 'builder@vbot', project_id: 'vbot' },
      event: { agent_id: 'builder', project_id: 'vbot' },
      childAddress: 'builder@vbot',
      transport: 'websocket',
    },
    {
      name: 'Project child, historical bare result, WebSocket',
      result: { agent_id: 'builder', project_id: 'vbot' },
      event: { agent_id: 'builder', project_id: 'vbot' },
      childAddress: 'builder@vbot',
      transport: 'websocket',
    },
    {
      name: 'Project child, current result, SSE',
      result: { agent_id: 'builder@vbot', project_id: 'vbot' },
      event: { agent_id: 'builder', project_id: 'vbot' },
      childAddress: 'builder@vbot',
      transport: 'sse',
    },
    {
      name: 'Identity child, WebSocket',
      result: { agent_id: 'helper' },
      event: { agent_id: 'helper' },
      childAddress: 'helper',
      transport: 'websocket',
    },
    {
      name: 'Identity child, SSE',
      result: { agent_id: 'helper' },
      event: { agent_id: 'helper' },
      childAddress: 'helper',
      transport: 'sse',
    },
  ])(
    'follows the child Run events for a $name',
    ({ result, event, childAddress, transport }) => {
      // SSE only streams the displayed Session; WebSocket covers the rest.
      const harness = makeStreamHarness({
        displayedAgentId: transport === 'sse' ? childAddress : PARENT_AGENT_ID,
        displayedSessionId:
          transport === 'sse' ? CHILD_SESSION_ID : PARENT_SESSION_ID,
      });
      const identity = {
        run_id: CHILD_RUN_ID,
        ...event,
        session_id: CHILD_SESSION_ID,
      };
      const deliver = (type, sequence, payload) => {
        if (transport === 'sse') {
          harness.sse({
            type,
            sequence,
            ...identity,
            payload,
            timestamp: STARTED_AT,
          });
          return;
        }
        const fields = type.startsWith('run_') ? payload : { output: payload };
        harness.stream.handleServerEvents(
          serverRunEvent(type, sequence, {
            ...identity,
            run_event_timestamp: STARTED_AT,
            ...fields,
          }),
        );
      };
      const row = spawnRow(result);
      const statuses = harness.subAgentRunStatuses;

      harness.stream.handleServerEvents(
        serverRunEvent('run_started', 1, {
          ...identity,
          run_event_timestamp: STARTED_AT,
          status: 'running',
          output: { status: 'running' },
        }),
      );
      if (transport === 'sse') expect(harness.subscriptions).toHaveLength(1);
      deliver('tool_call_started', 2, {
        tool_call: { id: 'call-1', index: 0, name: 'read', arguments: {} },
      });

      expect(subAgentDotStatus(row, statuses)).toBe('running');
      // The running label measures from the child run's start event.
      expect(
        subAgentToolStatusLabel(
          row,
          'running',
          statuses,
          Date.parse(STARTED_AT) + 4200,
        ),
      ).toBe(t('chat.toolDurationSeconds', '', { seconds: '4.2' }));
      expect(subAgentLastToolName(row, statuses)).toBe('read');

      deliver('run_completed', 3, {
        status: 'completed',
        timing: { duration_ms: 4200 },
      });

      expect(subAgentDotStatus(row, statuses)).toBe('success');
      // The finished label shows the child run's real runtime.
      expect(subAgentToolStatusLabel(row, 'success', statuses)).toBe(
        t('chat.toolDurationSeconds', '', { seconds: '4.2' }),
      );
      // One key form: the bare twin of a Project address is never written.
      expect(
        Object.keys(statuses).filter((key) => key.startsWith('session')),
      ).toEqual(
        expect.arrayContaining([
          `session:${childAddress}::${CHILD_SESSION_ID}`,
          `sessionDuration:${childAddress}::${CHILD_SESSION_ID}`,
          `sessionTool:${childAddress}::${CHILD_SESSION_ID}`,
          `sessionStarted:${childAddress}::${CHILD_SESSION_ID}`,
        ]),
      );
      expect(
        Object.keys(statuses).filter(
          (key) =>
            key.startsWith('session') && !key.includes(`:${childAddress}::`),
        ),
      ).toEqual([]);
    },
  );

  it('keys an explicit Project child status change by the address the row reads', () => {
    const harness = makeStreamHarness({
      displayedAgentId: PARENT_AGENT_ID,
      displayedSessionId: PARENT_SESSION_ID,
    });
    const row = spawnRow({ agent_id: 'builder@vbot', project_id: 'vbot' });

    harness.stream.handleServerEvents(
      serverRunEvent('subagent_status_changed', 4, {
        run_id: 'run-parent',
        agent_id: 'lead',
        project_id: 'vbot',
        session_id: PARENT_SESSION_ID,
        contributes_to_agent_activity: false,
        output: {
          data: {
            id: 'sub-work',
            agent_id: 'builder',
            project_id: 'vbot',
            session_id: CHILD_SESSION_ID,
            status: 'cancelled',
            started_at: STARTED_AT,
          },
        },
      }),
    );

    expect(harness.subAgentRunStatuses).toEqual({
      [`session:builder@vbot::${CHILD_SESSION_ID}`]: 'cancelled',
      [`sessionStarted:builder@vbot::${CHILD_SESSION_ID}`]: STARTED_AT,
    });
    expect(subAgentDotStatus(row, harness.subAgentRunStatuses)).toBe(
      'cancelled',
    );
  });
});

describe('reflection review tracking', () => {
  const SOURCE_SESSION_ID = 'session-source';

  function reflectionServerEvent(type, overrides = {}) {
    return serverRunEvent(type, 1, {
      run_id: 'run-refl-1',
      agent_id: 'alpha',
      project_id: null,
      session_id: 'session-fork',
      run_kind: 'memory_reflection',
      run_event_timestamp: '2026-08-24T10:00:00.000Z',
      contributes_to_agent_activity: false,
      source_session_id: SOURCE_SESSION_ID,
      status: type === 'run_started' ? 'running' : 'completed',
      ...overrides,
    });
  }

  const sourceReflectionTasks = (harness) =>
    harness.chatState.sessions[`alpha::${SOURCE_SESSION_ID}`]?.reflectionTasks;

  it('tracks a reflection Run on the reviewed source Session from start to its terminal event', () => {
    const harness = makeStreamHarness();

    harness.stream.handleServerEvents(reflectionServerEvent('run_started'));

    expect(sourceReflectionTasks(harness)).toEqual({
      'run-refl-1': {
        sessionId: 'session-fork',
        runKind: 'memory_reflection',
        status: 'running',
        startedAt: '2026-08-24T10:00:00.000Z',
      },
    });

    harness.stream.handleServerEvents(
      reflectionServerEvent('run_completed', {
        run_event_sequence: 2,
        run_event_timestamp: '2026-08-24T10:05:00.000Z',
      }),
    );

    expect(sourceReflectionTasks(harness)['run-refl-1']).toMatchObject({
      sessionId: 'session-fork',
      status: 'completed',
      startedAt: '2026-08-24T10:00:00.000Z',
    });
  });

  it('ignores non-reflection runs and reflection events without source provenance', () => {
    const harness = makeStreamHarness();

    harness.stream.handleServerEvents(
      serverRunEvent('run_started', 1, {
        run_id: 'run-user',
        agent_id: 'alpha',
        session_id: SOURCE_SESSION_ID,
        run_kind: 'user',
        run_event_timestamp: '2026-08-24T10:00:00.000Z',
      }),
    );
    harness.stream.handleServerEvents(
      reflectionServerEvent('run_started', { source_session_id: '' }),
    );

    expect(sourceReflectionTasks(harness)).toEqual({});
  });

  it('seeds running reflections from the connection snapshot, drops stale running entries, and keeps finished ones', () => {
    const harness = makeStreamHarness();
    const source = ensureSessionState(
      harness.chatState,
      'alpha',
      SOURCE_SESSION_ID,
    );
    const finished = {
      sessionId: 'session-fork-done',
      runKind: 'memory_reflection',
      status: 'completed',
      startedAt: '2026-08-24T08:00:00.000Z',
    };
    source.reflectionTasks = {
      'run-stale': {
        sessionId: 'session-fork-old',
        runKind: 'skill_reflection',
        status: 'running',
        startedAt: '2026-08-24T09:00:00.000Z',
      },
      'run-finished': finished,
    };

    harness.stream.applyConnectionSnapshot({
      type: 'connection_ready',
      active_runs: [
        activeRun('run-refl-live', {
          session_id: 'session-fork',
          run_kind: 'memory_reflection',
          source_session_id: SOURCE_SESSION_ID,
          started_at: '2026-08-24T11:00:00.000Z',
          contributes_to_agent_activity: false,
        }),
      ],
    });

    expect(source.reflectionTasks).toEqual({
      'run-refl-live': {
        sessionId: 'session-fork',
        runKind: 'memory_reflection',
        status: 'running',
        startedAt: '2026-08-24T11:00:00.000Z',
      },
      'run-finished': finished,
    });
  });
});
