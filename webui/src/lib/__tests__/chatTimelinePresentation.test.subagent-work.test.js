import { describe, expect, it } from 'vitest';
import {
  backgroundTasks,
  compactToolValue,
  isBackgroundSubAgentSpawn,
  isSubAgentSpawnTool,
  resolveSubAgentCancelPlan,
  subAgentDisplayResult,
  subAgentDotStatus,
  subAgentEffectiveRunId,
  subAgentLastToolName,
  subAgentNeedsStatusVerification,
  subAgentNavigationTarget,
  subAgentResultEntryAllowsFetch,
  subAgentResultKey,
  subAgentResultTextFromMessages,
  subAgentRunDurationMs,
  subAgentShouldFetchResult,
  subAgentToolStatusLabel,
} from '../chatTimelinePresentation.js';

import {
  runningSubAgentTool,
  queuedSubAgentTool,
  backgroundBashTool,
  setupChatTimelinePresentationSuite,
} from './chatTimelinePresentation.support.js';

describe('chatTimelinePresentation', () => {
  setupChatTimelinePresentationSuite();

  it('projects an externally completed sub-agent run as successful', () => {
    const tool = {
      name: 'subagent',
      status: 'running',
      arguments: {
        action: 'run',
        agent_id: 'worker',
        content: 'Inspect the project',
      },
      subAgentSession: {
        agent_id: 'worker',
        session_id: 'session-child',
        run_id: 'run-child',
        status: 'running',
        delivery: 'automatic',
      },
      startedEvent: {},
    };

    const status = subAgentDotStatus(tool, {
      'run:run-child': 'completed',
    });

    expect(status).toBe('success');
  });

  it('ignores a stale session status for a row with a known run id (B6)', () => {
    const tool = runningSubAgentTool();

    // A previous run of the same reused child session left its terminal
    // status under the session key; this spawn's own run has no status yet,
    // so the dot must stay running instead of showing the old run's success.
    const status = subAgentDotStatus(tool, {
      'session:worker::session-child': 'completed',
    });

    expect(status).toBe('running');
  });

  it('settles a queued spawn through the queue→run mapping, not the session key', () => {
    const tool = queuedSubAgentTool();

    expect(
      subAgentDotStatus(tool, {
        'queueRun:queue-item-1': 'run-from-queue',
        'run:run-from-queue': 'completed',
        'session:worker::session-child': 'running',
      }),
    ).toBe('success');
    // Without the mapping the queued descriptor keeps the dot running.
    expect(subAgentDotStatus(tool, {})).toBe('running');
  });

  it('flags a frozen-descriptor running row for status verification when no live status has arrived', () => {
    const tool = runningSubAgentTool();

    // The dot says "running" but no run: or session: key exists in
    // subAgentStatuses, so the only signal is the persisted descriptor.
    expect(subAgentDotStatus(tool, {})).toBe('running');
    expect(subAgentNeedsStatusVerification(tool, 'running', {})).toBe(true);
  });

  it('does not flag a run-id-less row once a session status has arrived', () => {
    const tool = queuedSubAgentTool();

    expect(
      subAgentNeedsStatusVerification(tool, 'running', {
        'session:worker::session-child': 'running',
      }),
    ).toBe(false);
  });

  it('still flags a row with a known run id when only a session status exists (B6)', () => {
    const tool = runningSubAgentTool();

    // The session entry may describe another run of the same reused child
    // session, so it must not suppress verification of this specific run.
    expect(
      subAgentNeedsStatusVerification(tool, 'running', {
        'session:worker::session-child': 'completed',
      }),
    ).toBe(true);
  });

  it('does not flag a row once a run status has arrived', () => {
    const tool = runningSubAgentTool();

    expect(
      subAgentNeedsStatusVerification(tool, 'running', {
        'run:run-child': 'completed',
      }),
    ).toBe(false);
  });

  it('does not flag rows whose dot is not running', () => {
    const tool = runningSubAgentTool();

    expect(subAgentNeedsStatusVerification(tool, 'success', {})).toBe(false);
    expect(subAgentNeedsStatusVerification(tool, 'failed', {})).toBe(false);
    expect(subAgentNeedsStatusVerification(tool, 'cancelled', {})).toBe(false);
  });

  it('tolerates a missing or malformed status map by treating it as empty', () => {
    const tool = runningSubAgentTool();

    expect(subAgentNeedsStatusVerification(tool, 'running', null)).toBe(true);
    expect(subAgentNeedsStatusVerification(tool, 'running', undefined)).toBe(
      true,
    );
    expect(subAgentNeedsStatusVerification(tool, 'running', 'not-a-map')).toBe(
      true,
    );
  });

  it('keys a sub-agent result by its stable public work id', () => {
    expect(subAgentResultKey(runningSubAgentTool())).toBe('work:sub_child');
    expect(subAgentResultKey(queuedSubAgentTool())).toBe('work:sub_queued');
    expect(
      subAgentResultKey(queuedSubAgentTool(), {
        'queueRun:queue-item-1': 'run-from-queue',
      }),
    ).toBe('work:sub_queued');
    expect(subAgentResultKey({ name: 'subagent', arguments: {} })).toBe('');
  });

  it('keeps a qualified sub-agent target address for navigation and status keys', () => {
    const tool = queuedSubAgentTool({
      // Shape merged from the live `subagent_session_started` event before the
      // final persisted tool result exists.
      subAgentSession: {
        agent_id: 'worker',
        project_id: 'vbot',
        session_id: 'session-child',
        queue_item_id: 'queue-item-1',
        status: 'queued',
      },
    });

    expect(subAgentNavigationTarget(tool)).toEqual({
      agentId: 'worker@vbot',
      sessionId: 'session-child',
    });
    expect(subAgentResultKey(tool)).toBe('work:sub_queued');
    expect(
      subAgentRunDurationMs(tool, {
        'sessionDuration:worker@vbot::session-child': 8700,
      }),
    ).toBe(8700);
    expect(resolveSubAgentCancelPlan(tool)).toEqual({
      kind: 'queue',
      queueItemId: 'queue-item-1',
      agentId: 'worker@vbot',
      sessionId: 'session-child',
    });
  });

  it('resolves the effective run id from the descriptor or controller mappings', () => {
    expect(subAgentEffectiveRunId(runningSubAgentTool())).toBe('run-child');
    expect(subAgentEffectiveRunId(queuedSubAgentTool())).toBe('');
    expect(
      subAgentEffectiveRunId(queuedSubAgentTool(), {
        'workRun:sub_queued': 'run-from-inspection',
        'queueRun:queue-item-1': 'run-from-queue',
      }),
    ).toBe('run-from-inspection');
    expect(
      subAgentEffectiveRunId(queuedSubAgentTool(), {
        'queueRun:queue-item-1': 'run-from-queue',
      }),
    ).toBe('run-from-queue');
  });

  it('plans a run cancel for any resolvable child run id', () => {
    // Descriptor-carried run id (a directly started spawn).
    expect(resolveSubAgentCancelPlan(runningSubAgentTool())).toEqual({
      kind: 'run',
      runId: 'run-child',
    });
    // A queued spawn that has since started resolves through the
    // queueRun:<item> mapping — never through the frozen descriptor.
    expect(
      resolveSubAgentCancelPlan(queuedSubAgentTool(), {
        'queueRun:queue-item-1': 'run-from-queue',
      }),
    ).toEqual({ kind: 'run', runId: 'run-from-queue' });
  });

  it('plans a queue removal for a queued spawn without a resolvable run id', () => {
    expect(resolveSubAgentCancelPlan(queuedSubAgentTool())).toEqual({
      kind: 'queue',
      queueItemId: 'queue-item-1',
      agentId: 'worker',
      sessionId: 'session-child',
    });
  });

  it('plans nothing when the row addresses no run and no queue item', () => {
    expect(resolveSubAgentCancelPlan(null)).toBeNull();
    expect(
      resolveSubAgentCancelPlan({ name: 'subagent', arguments: {} }),
    ).toBeNull();
  });

  it('allows fetching when no entry exists and retries failed entries after the cooldown', () => {
    const now = 1_000_000;
    expect(subAgentResultEntryAllowsFetch(null, now)).toBe(true);
    expect(subAgentResultEntryAllowsFetch(undefined, now)).toBe(true);
    // Loading and successful entries never refetch.
    expect(
      subAgentResultEntryAllowsFetch({ loading: true, result: '' }, now),
    ).toBe(false);
    expect(
      subAgentResultEntryAllowsFetch({ loading: false, result: 'done' }, now),
    ).toBe(false);
    // Failed entries become fetchable again only after the cooldown.
    const failedEntry = {
      loading: false,
      result: '',
      error: true,
      failedAt: now,
    };
    expect(subAgentResultEntryAllowsFetch(failedEntry, now + 1000)).toBe(false);
    expect(subAgentResultEntryAllowsFetch(failedEntry, now + 20000)).toBe(true);
  });

  it('requests a result only for a finished automatic-delivery spawn without inline output', () => {
    expect(subAgentShouldFetchResult(runningSubAgentTool(), 'success')).toBe(
      true,
    );
    // Still running -> no fetch yet.
    expect(subAgentShouldFetchResult(runningSubAgentTool(), 'running')).toBe(
      false,
    );
    // Status calls are ordinary subagent tool rows, not spawn rows.
    expect(
      subAgentShouldFetchResult(
        {
          ...runningSubAgentTool(),
          arguments: { action: 'status', id: 'sub_child' },
        },
        'success',
      ),
    ).toBe(false);
  });

  it('reserves the special Sub-Agent row for spawn calls', () => {
    expect(isSubAgentSpawnTool(runningSubAgentTool())).toBe(true);
    expect(
      isSubAgentSpawnTool({
        name: 'subagent',
        arguments: {
          action: 'run',
          content: 'canonical child task',
        },
      }),
    ).toBe(true);
    expect(
      isSubAgentSpawnTool({
        name: 'subagent',
        arguments: { action: 'cancel', id: 'sub_child' },
      }),
    ).toBe(false);
    expect(
      isSubAgentSpawnTool({
        ...runningSubAgentTool(),
        arguments: { action: 'status', id: 'sub_child' },
      }),
    ).toBe(false);
  });

  it('projects automatic Sub-Agent and Bash tasks with active work first', () => {
    const running = runningSubAgentTool({ type: 'tool_call' });
    const completed = runningSubAgentTool({
      type: 'tool_call',
      id: 'tool-completed',
      arguments: {
        action: 'run',
        agent_id: 'reviewer',
        content: 'Review the implementation',
      },
      subAgentSession: {
        id: 'sub_completed',
        agent_id: 'reviewer',
        session_id: 'session-reviewer',
        run_id: 'run-reviewer',
        status: 'completed',
        delivery: 'automatic',
      },
      result: {
        ok: true,
        data: {
          id: 'sub_completed',
          agent_id: 'reviewer',
          session_id: 'session-reviewer',
          status: 'completed',
          delivery: 'automatic',
        },
        artifacts: [],
      },
    });
    const foreground = runningSubAgentTool({
      type: 'tool_call',
      id: 'tool-foreground',
      subAgentSession: {
        id: 'sub_foreground',
        agent_id: 'worker',
        session_id: 'session-foreground',
        run_id: 'run-foreground',
        status: 'completed',
        delivery: 'inline',
      },
      result: {
        ok: true,
        data: {
          id: 'sub_foreground',
          agent_id: 'worker',
          session_id: 'session-foreground',
          status: 'completed',
          delivery: 'inline',
          result: 'done',
        },
        artifacts: [],
      },
    });

    const backgroundBash = backgroundBashTool();
    const foregroundBash = backgroundBashTool({
      id: 'bash-foreground',
      arguments: { command: 'npm test', mode: 'foreground' },
      result: {
        ok: true,
        data: { status: 'completed', mode: 'foreground' },
        artifacts: [],
      },
    });
    const tasks = backgroundTasks(
      [
        { id: 'run-old', type: 'assistant_run', items: [running] },
        {
          id: 'run-new',
          type: 'assistant_run',
          items: [completed, foreground, foregroundBash, backgroundBash],
        },
      ],
      {},
      { 'process-one': 'failed' },
    );

    expect(tasks).toHaveLength(3);
    expect(tasks.map((task) => task.tool.id)).toEqual([
      running.id,
      backgroundBash.id,
      completed.id,
    ]);
    expect(tasks.map((task) => task.dotStatus)).toEqual([
      'running',
      'failed',
      'success',
    ]);
    expect(tasks[1]).toEqual(
      expect.objectContaining({
        kind: 'bash',
        command: 'npm run dev',
        processId: 'process-one',
        target: null,
      }),
    );
    expect(tasks[2]).toEqual(
      expect.objectContaining({
        kind: 'subagent',
        agentId: 'reviewer',
        preview: 'Review the implementation',
        target: {
          agentId: 'reviewer',
          sessionId: 'session-reviewer',
        },
      }),
    );
  });

  it('recognizes legacy explicit background spawns without delivery metadata', () => {
    expect(
      isBackgroundSubAgentSpawn({
        name: 'subagent',
        arguments: {
          agent_id: 'worker',
          background: true,
          content: 'Inspect the project',
        },
      }),
    ).toBe(true);
  });

  it('keeps an exact queued cancellation settled when its Session runs again', () => {
    const tool = queuedSubAgentTool();

    expect(
      subAgentDotStatus(tool, {
        'queue:queue-item-1': 'cancelled',
        'session:worker::session-child': 'running',
      }),
    ).toBe('cancelled');
  });

  it('does not request a result when a blocking spawn already carries one', () => {
    const blockingTool = runningSubAgentTool({
      result: {
        ok: true,
        error: null,
        data: {
          id: 'sub_child',
          agent_id: 'worker',
          session_id: 'session-child',
          status: 'completed',
          result: 'Final answer from the worker.',
          delivery: 'inline',
        },
        artifacts: [],
      },
    });

    expect(subAgentShouldFetchResult(blockingTool, 'success')).toBe(false);
  });

  it('renders a fetched result the same way a blocking spawn result renders', () => {
    const tool = runningSubAgentTool();
    const displayValue = subAgentDisplayResult(tool, {
      loading: false,
      result: 'Final answer from the worker.',
    });
    const rendered = compactToolValue(displayValue, {
      preferPayload: true,
      toolName: 'subagent',
      tool,
    });

    expect(rendered).toContain('result: Final answer from the worker.');
    expect(rendered).toContain('status: completed');
  });

  it('keeps the original tool result when no fetched output exists', () => {
    const tool = runningSubAgentTool();
    expect(subAgentDisplayResult(tool, null)).toBe(tool.result);
    expect(subAgentDisplayResult(tool, { loading: true, result: '' })).toBe(
      tool.result,
    );
  });

  it('extracts the final assistant message from a terminal Run segment', () => {
    const messages = [
      { role: 'user', content: 'Do the work' },
      { role: 'assistant', content: 'Working on it' },
      { role: 'tool', content: 'tool output' },
      { role: 'assistant', content: 'All done.' },
      { role: 'run_summary', run_id: 'run-child', status: 'completed' },
    ];

    expect(subAgentResultTextFromMessages(messages, 'run-child')).toBe(
      'All done.',
    );
  });

  it('does not treat newer intermediate Assistant output as a final result', () => {
    const messages = [
      { role: 'user', content: 'First task' },
      { role: 'assistant', content: 'First answer.' },
      { role: 'run_summary', run_id: 'run-one', status: 'completed' },
      { role: 'user', content: 'Continue' },
      { role: 'assistant', content: 'Still working.' },
    ];

    expect(subAgentResultTextFromMessages(messages)).toBe('');
    expect(subAgentResultTextFromMessages(messages, 'run-one')).toBe(
      'First answer.',
    );
  });

  it('resolves the child run duration strictly by run id when it is known', () => {
    const tool = runningSubAgentTool();
    expect(subAgentRunDurationMs(tool, { 'runDuration:run-child': 4200 })).toBe(
      4200,
    );
    // The session-scoped duration may belong to another run of the same
    // reused child session, so a row with a known run id must not use it (B6).
    expect(
      subAgentRunDurationMs(tool, {
        'sessionDuration:worker::session-child': 8700,
      }),
    ).toBeNull();
    expect(subAgentRunDurationMs(tool, {})).toBeNull();
  });

  it('falls back to the session duration only when no run id is known', () => {
    const tool = queuedSubAgentTool();
    expect(
      subAgentRunDurationMs(tool, {
        'sessionDuration:worker::session-child': 8700,
      }),
    ).toBe(8700);
    expect(
      subAgentRunDurationMs(tool, {
        'queueRun:queue-item-1': 'run-from-queue',
        'runDuration:run-from-queue': 3100,
        'sessionDuration:worker::session-child': 8700,
      }),
    ).toBe(3100);
  });

  it('resolves the last tool name strictly by run id when it is known', () => {
    const tool = runningSubAgentTool();
    expect(subAgentLastToolName(tool, { 'runTool:run-child': 'bash' })).toBe(
      'bash',
    );
    // The session-scoped name may belong to another run of the same reused
    // child session, so a row with a known run id must not use it (B6).
    expect(
      subAgentLastToolName(tool, {
        'sessionTool:worker::session-child': 'read',
      }),
    ).toBe('');
    expect(subAgentLastToolName(tool, {})).toBe('');
  });

  it('falls back to the session-scoped tool name only when no run id is known', () => {
    const tool = queuedSubAgentTool();
    expect(
      subAgentLastToolName(tool, {
        'sessionTool:worker::session-child': 'read',
      }),
    ).toBe('read');
    expect(
      subAgentLastToolName(tool, {
        'queueRun:queue-item-1': 'run-from-queue',
        'runTool:run-from-queue': 'bash',
        'sessionTool:worker::session-child': 'read',
      }),
    ).toBe('bash');
  });

  it('reports no last tool name for subagent status rows', () => {
    const resultTool = runningSubAgentTool({
      arguments: { action: 'status', id: 'sub_child' },
    });
    expect(
      subAgentLastToolName(resultTool, { 'runTool:run-child': 'bash' }),
    ).toBe('');
  });

  it('labels a non-blocking spawn with the child run runtime, not the spawn call', () => {
    const tool = runningSubAgentTool();
    expect(
      subAgentToolStatusLabel(tool, 'success', {
        'runDuration:run-child': 4200,
      }),
    ).toBe('4.2s');
  });

  it('shows no time for a finished non-blocking spawn without a tracked runtime', () => {
    const tool = runningSubAgentTool();
    expect(subAgentToolStatusLabel(tool, 'success', {})).toBe('');
  });

  it('reports cancelled and running sub-agent states without a duration', () => {
    const tool = runningSubAgentTool();
    expect(subAgentToolStatusLabel(tool, 'cancelled', {})).toBe('cancelled');
    expect(subAgentToolStatusLabel(tool, 'running', {})).toBe('');
  });

  it('falls back to the spawn-call duration for a blocking spawn that carries a result', () => {
    const blockingTool = runningSubAgentTool({
      durationMs: 1500,
      result: {
        ok: true,
        error: null,
        data: {
          id: 'sub_child',
          agent_id: 'worker',
          session_id: 'session-child',
          status: 'completed',
          result: 'Final answer from the worker.',
          delivery: 'inline',
        },
        artifacts: [],
      },
    });

    expect(subAgentToolStatusLabel(blockingTool, 'success', {})).toBe('1.5s');
  });

  it('extracts text from assistant content blocks and ignores empty input', () => {
    const messages = [
      {
        role: 'assistant',
        content: [
          { type: 'text', text: 'First part.' },
          { type: 'media', attachment_id: 'a1' },
          { type: 'text', text: 'Second part.' },
        ],
      },
      { role: 'run_summary', run_id: 'run-child', status: 'completed' },
    ];

    expect(subAgentResultTextFromMessages(messages, 'run-child')).toBe(
      'First part.\n\nSecond part.',
    );
    expect(subAgentResultTextFromMessages([])).toBe('');
    expect(subAgentResultTextFromMessages(null)).toBe('');
  });
});
