import { describe, expect, it } from 'vitest';
import {
  backgroundTasks,
  compactToolValue,
  isSubAgentSpawnTool,
  resolveSubAgentCancelPlan,
  subAgentAgentId,
  subAgentDisplayResult,
  subAgentDotStatus,
  subAgentEffectiveRunId,
  subAgentLastToolName,
  subAgentNavigationTarget,
  subAgentNeedsStatusVerification,
  subAgentPreview,
  subAgentResultEntryAllowsFetch,
  subAgentResultKey,
  subAgentResultTextFromMessages,
  subAgentShouldFetchResult,
  subAgentToolStatusLabel,
} from '../chatTimelinePresentation.js';
import { t } from '../i18n.js';
import {
  backgroundBashTool,
  queuedSubAgentTool,
  runningSubAgentTool,
} from './chatTimelinePresentation.support.js';

const seconds = (value) =>
  t('chat.toolDurationSeconds', '', { seconds: value });

function blockingSubAgentTool(overrides = {}) {
  return runningSubAgentTool({
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
    ...overrides,
  });
}

describe('Sub-Agent spawn rows', () => {
  it.each([
    ['a running spawn', () => runningSubAgentTool(), true],
    [
      'a canonical run action',
      () => ({
        name: 'subagent',
        arguments: { action: 'run', content: 'canonical child task' },
      }),
      true,
    ],
    [
      'a capitalized run action',
      () => ({
        name: 'subagent',
        arguments: { action: 'Run', content: 'Inspect the project' },
      }),
      true,
    ],
    [
      'a spawn action carrying delivery metadata',
      () =>
        runningSubAgentTool({
          arguments: { action: 'spawn', task: 'Inspect the project' },
        }),
      true,
    ],
    [
      'another harness spelling carrying delivery metadata',
      () =>
        runningSubAgentTool({
          arguments: {
            description: 'Inspect',
            prompt: 'Inspect the project',
            subagent_type: 'worker',
          },
        }),
      true,
    ],
    [
      'an unfamiliar action without delivery metadata',
      () => ({
        name: 'subagent',
        arguments: { action: 'spawn', task: 'Inspect the project' },
      }),
      false,
    ],
    [
      'a stop action without delivery metadata',
      () => ({
        name: 'subagent',
        arguments: { action: 'stop', id: 'sub_child' },
      }),
      false,
    ],
    [
      'a cancel call',
      () => ({
        name: 'subagent',
        arguments: { action: 'cancel', id: 'sub_child' },
      }),
      false,
    ],
    [
      'a status call carrying delivery metadata',
      () =>
        runningSubAgentTool({
          arguments: { action: 'status', id: 'sub_child' },
        }),
      false,
    ],
    [
      'a capitalized cancel call carrying delivery metadata',
      () =>
        runningSubAgentTool({
          arguments: { action: 'Cancel', id: 'sub_child' },
        }),
      false,
    ],
  ])('treats %s as a spawn: %s', (_label, tool, spawn) => {
    expect(isSubAgentSpawnTool(tool())).toBe(spawn);
  });

  it.each([
    [{ prompt: 'Review imports', subagent_type: 'worker' }, 'Review imports'],
    [{ goal: 'Fix tests' }, 'Fix tests'],
    [{ content: 'Canonical task', prompt: 'Other' }, 'Canonical task'],
  ])('previews the task from %o', (args, preview) => {
    expect(subAgentPreview(runningSubAgentTool({ arguments: args }))).toBe(
      preview,
    );
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

    expect(tasks.map((task) => [task.tool.id, task.dotStatus])).toEqual([
      [running.id, 'running'],
      [backgroundBash.id, 'failed'],
      [completed.id, 'success'],
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
        target: { agentId: 'reviewer', sessionId: 'session-reviewer' },
      }),
    );
  });

  it('lists a legacy explicit background spawn without delivery metadata', () => {
    const legacy = {
      type: 'tool_call',
      id: 'tool-legacy',
      name: 'subagent',
      arguments: {
        agent_id: 'worker',
        background: true,
        content: 'Inspect the project',
      },
      resultEvent: { type: 'tool_call_result' },
      result: {
        ok: true,
        data: { agent_id: 'worker', session_id: 'session-legacy' },
      },
    };

    expect(
      backgroundTasks([
        { id: 'run-legacy', type: 'assistant_run', items: [legacy] },
      ]),
    ).toEqual([
      expect.objectContaining({
        kind: 'subagent',
        tool: legacy,
        target: { agentId: 'worker', sessionId: 'session-legacy' },
      }),
    ]);
  });
});

describe('Sub-Agent status', () => {
  it.each([
    [
      'settles an externally completed Run',
      () => ({
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
      }),
      { 'run:run-child': 'completed' },
      'success',
    ],
    [
      // A previous Run of the reused child Session left its terminal status
      // under the Session key; this spawn's own Run has no status yet.
      'ignores a stale Session status for a row with a known Run id',
      runningSubAgentTool,
      { 'session:worker::session-child': 'completed' },
      'running',
    ],
    [
      'keeps a frozen running descriptor without live status',
      runningSubAgentTool,
      {},
      'running',
    ],
    [
      'settles a queued spawn through the queue-to-Run mapping',
      queuedSubAgentTool,
      {
        'queueRun:queue-item-1': 'run-from-queue',
        'run:run-from-queue': 'completed',
        'session:worker::session-child': 'running',
      },
      'success',
    ],
    [
      'keeps a queued spawn running without the mapping',
      queuedSubAgentTool,
      {},
      'running',
    ],
    [
      'keeps an exact queued cancellation when its Session runs again',
      queuedSubAgentTool,
      {
        'queue:queue-item-1': 'cancelled',
        'session:worker::session-child': 'running',
      },
      'cancelled',
    ],
  ])('%s', (_label, tool, statuses, dotStatus) => {
    expect(subAgentDotStatus(tool(), statuses)).toBe(dotStatus);
  });

  it.each([
    ['a frozen running descriptor', runningSubAgentTool, 'running', {}, true],
    [
      'a Run-id-less row with a Session status',
      queuedSubAgentTool,
      'running',
      { 'session:worker::session-child': 'running' },
      false,
    ],
    [
      // The Session entry may describe another Run of the reused child Session.
      'a known Run id with only a Session status',
      runningSubAgentTool,
      'running',
      { 'session:worker::session-child': 'completed' },
      true,
    ],
    [
      'a row whose Run status arrived',
      runningSubAgentTool,
      'running',
      { 'run:run-child': 'completed' },
      false,
    ],
    ['a successful dot', runningSubAgentTool, 'success', {}, false],
    ['a failed dot', runningSubAgentTool, 'failed', {}, false],
    ['a cancelled dot', runningSubAgentTool, 'cancelled', {}, false],
    ['a missing status map', runningSubAgentTool, 'running', null, true],
    [
      'an undefined status map',
      runningSubAgentTool,
      'running',
      undefined,
      true,
    ],
    ['a malformed status map', runningSubAgentTool, 'running', 'x', true],
  ])(
    'decides status verification for %s',
    (_label, tool, dotStatus, statuses, verify) => {
      expect(subAgentNeedsStatusVerification(tool(), dotStatus, statuses)).toBe(
        verify,
      );
    },
  );

  it('resolves the effective Run id from the descriptor or controller mappings', () => {
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

  it.each([
    [
      'cancels the descriptor Run',
      runningSubAgentTool,
      undefined,
      { kind: 'run', runId: 'run-child' },
    ],
    [
      // A started queued spawn resolves through the mapping, never through
      // the frozen descriptor.
      'cancels the Run a queued spawn has started',
      queuedSubAgentTool,
      { 'queueRun:queue-item-1': 'run-from-queue' },
      { kind: 'run', runId: 'run-from-queue' },
    ],
    [
      'removes a queued spawn without a resolvable Run id',
      queuedSubAgentTool,
      undefined,
      {
        kind: 'queue',
        queueItemId: 'queue-item-1',
        agentId: 'worker',
        sessionId: 'session-child',
      },
    ],
    ['plans nothing for a missing row', () => null, undefined, null],
    [
      'plans nothing without Run or Queue item',
      () => ({ name: 'subagent', arguments: {} }),
      undefined,
      null,
    ],
  ])('%s', (_label, tool, statuses, plan) => {
    expect(resolveSubAgentCancelPlan(tool(), statuses)).toEqual(plan);
  });
});

describe('Sub-Agent addressing', () => {
  it('keys a Sub-Agent result by its stable public work id', () => {
    expect(subAgentResultKey(runningSubAgentTool())).toBe('work:sub_child');
    expect(subAgentResultKey(queuedSubAgentTool())).toBe('work:sub_queued');
    expect(
      subAgentResultKey(queuedSubAgentTool(), {
        'queueRun:queue-item-1': 'run-from-queue',
      }),
    ).toBe('work:sub_queued');
    expect(subAgentResultKey({ name: 'subagent', arguments: {} })).toBe('');
  });

  it('keeps a qualified target address for navigation, status keys and cancel', () => {
    // Shape merged from the live `subagent_session_started` event before the
    // final persisted Tool result exists.
    const tool = queuedSubAgentTool({
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
      subAgentToolStatusLabel(tool, 'success', {
        'sessionDuration:worker@vbot::session-child': 8700,
      }),
    ).toBe(seconds('8.7'));
    expect(resolveSubAgentCancelPlan(tool)).toEqual({
      kind: 'queue',
      queueItemId: 'queue-item-1',
      agentId: 'worker@vbot',
      sessionId: 'session-child',
    });
  });

  it.each([
    ['a current result with the qualified agent_id', 'worker@vbot', false],
    ['a historical result with a bare agent_id', 'worker', false],
    ['a current result merged over the live start event', 'worker@vbot', true],
  ])(
    'addresses a Project child exactly once from %s',
    (_label, resultAgentId, withLiveStart) => {
      const tool = runningSubAgentTool({
        subAgentSession: withLiveStart
          ? {
              id: 'sub_child',
              agent_id: 'worker',
              project_id: 'vbot',
              session_id: 'session-child',
              status: 'running',
              delivery: 'automatic',
            }
          : undefined,
        result: {
          ok: true,
          error: null,
          data: {
            id: 'sub_child',
            agent_id: resultAgentId,
            project_id: 'vbot',
            session_id: 'session-child',
            status: 'running',
            delivery: 'automatic',
          },
          artifacts: [],
        },
      });

      expect(subAgentNavigationTarget(tool)).toEqual({
        agentId: 'worker@vbot',
        sessionId: 'session-child',
      });
      expect(subAgentAgentId(tool)).toBe('worker@vbot');
      expect(
        subAgentLastToolName(tool, {
          'sessionTool:worker@vbot::session-child': 'read',
        }),
      ).toBe('read');
      expect(
        subAgentDisplayResult(tool, { loading: false, result: 'done' }).data,
      ).toMatchObject({ agent_id: 'worker@vbot', project_id: 'vbot' });
    },
  );
});

describe('Sub-Agent results', () => {
  it('allows fetching without an entry and retries failed entries after the cooldown', () => {
    const now = 1_000_000;
    const failedEntry = {
      loading: false,
      result: '',
      error: true,
      failedAt: now,
    };

    expect(subAgentResultEntryAllowsFetch(null, now)).toBe(true);
    expect(subAgentResultEntryAllowsFetch(undefined, now)).toBe(true);
    expect(
      subAgentResultEntryAllowsFetch({ loading: true, result: '' }, now),
    ).toBe(false);
    expect(
      subAgentResultEntryAllowsFetch({ loading: false, result: 'done' }, now),
    ).toBe(false);
    expect(subAgentResultEntryAllowsFetch(failedEntry, now + 1000)).toBe(false);
    expect(subAgentResultEntryAllowsFetch(failedEntry, now + 20000)).toBe(true);
  });

  it.each([
    [
      'a finished automatic-delivery spawn',
      runningSubAgentTool,
      'success',
      true,
    ],
    ['a running spawn', runningSubAgentTool, 'running', false],
    [
      'a status call',
      () =>
        runningSubAgentTool({
          arguments: { action: 'status', id: 'sub_child' },
        }),
      'success',
      false,
    ],
    [
      'a blocking spawn that carries its result',
      blockingSubAgentTool,
      'success',
      false,
    ],
  ])('fetches a result for %s: %s', (_label, tool, dotStatus, fetch) => {
    expect(subAgentShouldFetchResult(tool(), dotStatus)).toBe(fetch);
  });

  it('renders a fetched result the same way a blocking spawn result renders', () => {
    const tool = runningSubAgentTool();
    const rendered = compactToolValue(
      subAgentDisplayResult(tool, {
        loading: false,
        result: 'Final answer from the worker.',
      }),
      { preferPayload: true, toolName: 'subagent', tool },
    );

    expect(rendered).toContain('result: Final answer from the worker.');
    expect(rendered).toContain('status: completed');
  });

  it('keeps the original Tool result without fetched output', () => {
    const tool = runningSubAgentTool();

    expect(subAgentDisplayResult(tool, null)).toBe(tool.result);
    expect(subAgentDisplayResult(tool, { loading: true, result: '' })).toBe(
      tool.result,
    );
  });

  const intermediateMessages = [
    { role: 'user', content: 'First task' },
    { role: 'assistant', content: 'First answer.' },
    { role: 'run_summary', run_id: 'run-one', status: 'completed' },
    { role: 'user', content: 'Continue' },
    { role: 'assistant', content: 'Still working.' },
  ];

  it.each([
    [
      'the final assistant message of a terminal Run segment',
      [
        { role: 'user', content: 'Do the work' },
        { role: 'assistant', content: 'Working on it' },
        { role: 'tool', content: 'tool output' },
        { role: 'assistant', content: 'All done.' },
        { role: 'run_summary', run_id: 'run-child', status: 'completed' },
      ],
      'run-child',
      'All done.',
    ],
    [
      'no newer intermediate assistant output',
      intermediateMessages,
      undefined,
      '',
    ],
    [
      'the answer of the requested earlier Run',
      intermediateMessages,
      'run-one',
      'First answer.',
    ],
    [
      'the text of assistant content blocks',
      [
        {
          role: 'assistant',
          content: [
            { type: 'text', text: 'First part.' },
            { type: 'media', attachment_id: 'a1' },
            { type: 'text', text: 'Second part.' },
          ],
        },
        { role: 'run_summary', run_id: 'run-child', status: 'completed' },
      ],
      'run-child',
      'First part.\n\nSecond part.',
    ],
    ['nothing from an empty History', [], undefined, ''],
    ['nothing from a missing History', null, undefined, ''],
  ])('extracts %s', (_label, messages, runId, text) => {
    expect(subAgentResultTextFromMessages(messages, runId)).toBe(text);
  });
});

describe('Sub-Agent timing and last Tool', () => {
  const startedAt = '2026-09-04T12:00:00Z';

  it.each([
    [
      'the child Run runtime of a non-blocking spawn',
      runningSubAgentTool,
      'success',
      { 'runDuration:run-child': 4200 },
      () => seconds('4.2'),
    ],
    [
      // The Session-scoped duration may belong to another Run of the reused
      // child Session.
      'no Session duration for a row with a known Run id',
      runningSubAgentTool,
      'success',
      { 'sessionDuration:worker::session-child': 8700 },
      () => '',
    ],
    [
      'no time for a finished non-blocking spawn without a tracked runtime',
      runningSubAgentTool,
      'success',
      {},
      () => '',
    ],
    [
      'the Session duration for a row without Run id',
      queuedSubAgentTool,
      'success',
      { 'sessionDuration:worker::session-child': 8700 },
      () => seconds('8.7'),
    ],
    [
      'the queue-mapped Run duration over the Session duration',
      queuedSubAgentTool,
      'success',
      {
        'queueRun:queue-item-1': 'run-from-queue',
        'runDuration:run-from-queue': 3100,
        'sessionDuration:worker::session-child': 8700,
      },
      () => seconds('3.1'),
    ],
    [
      'the spawn-call duration of a blocking spawn with a result',
      () => blockingSubAgentTool({ durationMs: 1500 }),
      'success',
      {},
      () => seconds('1.5'),
    ],
    [
      'the cancelled label without a tracked runtime',
      runningSubAgentTool,
      'cancelled',
      {},
      () => t('chat.toolCancelled'),
    ],
    [
      'a live tick from the child Run start',
      runningSubAgentTool,
      'running',
      { 'runStarted:run-child': startedAt },
      () => seconds('4.2'),
    ],
    [
      'no time for a running spawn without a start',
      runningSubAgentTool,
      'running',
      {},
      () => '',
    ],
  ])('labels %s', (_label, tool, dotStatus, statuses, expected) => {
    expect(
      subAgentToolStatusLabel(
        tool(),
        dotStatus,
        statuses,
        Date.parse(startedAt) + 4200,
      ),
    ).toBe(expected());
  });

  it.each([
    [
      'by Run id when it is known',
      runningSubAgentTool,
      { 'runTool:run-child': 'bash' },
      'bash',
    ],
    [
      'never from the Session key for a known Run id',
      runningSubAgentTool,
      { 'sessionTool:worker::session-child': 'read' },
      '',
    ],
    ['as empty before the first child Tool', runningSubAgentTool, {}, ''],
    [
      'from the Session key without Run id',
      queuedSubAgentTool,
      { 'sessionTool:worker::session-child': 'read' },
      'read',
    ],
    [
      'by the queue-mapped Run id over the Session key',
      queuedSubAgentTool,
      {
        'queueRun:queue-item-1': 'run-from-queue',
        'runTool:run-from-queue': 'bash',
        'sessionTool:worker::session-child': 'read',
      },
      'bash',
    ],
    [
      'as empty for a status call',
      () =>
        runningSubAgentTool({
          arguments: { action: 'status', id: 'sub_child' },
        }),
      { 'runTool:run-child': 'bash' },
      '',
    ],
  ])('resolves the last child Tool %s', (_label, tool, statuses, name) => {
    expect(subAgentLastToolName(tool(), statuses)).toBe(name);
  });
});
