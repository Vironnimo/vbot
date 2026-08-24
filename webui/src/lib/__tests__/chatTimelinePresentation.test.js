import { beforeEach, describe, expect, it } from 'vitest';

import {
  backgroundTasks,
  changeStatsLabel,
  changeStatsParts,
  changeStatsTooltip,
  compactToolValue,
  compactionSummaryText,
  errorMessagePresentation,
  formatTime,
  isRowCancellable,
  isBackgroundSubAgentSpawn,
  isRunChildWorking,
  isSubAgentSpawnTool,
  isToolPreparing,
  isReflectionRunKind,
  labelForEvent,
  labelForMessage,
  reflectionElapsedLabel,
  reflectionScopeForRunKind,
  reflectionTaskRows,
  reasoningDurationLabel,
  resolveSubAgentCancelPlan,
  runChangeStats,
  runFooterNotice,
  runFooterParts,
  sessionChangeStats,
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
  takeoverSeparatorLabel,
  toolArgumentSummary,
  toolDetailPresentation,
  toolRowPresentation,
  visibleRunChildren,
} from '../chatTimelinePresentation.js';
import { init } from '../i18n.js';

function runningSubAgentTool(overrides = {}) {
  return {
    name: 'subagent',
    status: 'success',
    arguments: {
      action: 'run',
      agent_id: 'worker',
      content: 'Inspect the project',
    },
    subAgentSession: {
      id: 'sub_child',
      agent_id: 'worker',
      session_id: 'session-child',
      run_id: 'run-child',
      status: 'running',
      delivery: 'automatic',
    },
    result: {
      ok: true,
      error: null,
      data: {
        id: 'sub_child',
        agent_id: 'worker',
        session_id: 'session-child',
        status: 'running',
        delivery: 'automatic',
      },
      artifacts: [],
    },
    ...overrides,
  };
}

function queuedSubAgentTool(overrides = {}) {
  return {
    name: 'subagent',
    status: 'success',
    arguments: {
      action: 'run',
      agent_id: 'worker',
      content: 'Inspect the project',
    },
    subAgentSession: {
      id: 'sub_queued',
      agent_id: 'worker',
      session_id: 'session-child',
      queue_item_id: 'queue-item-1',
      status: 'queued',
      delivery: 'automatic',
    },
    result: {
      ok: true,
      error: null,
      data: {
        id: 'sub_queued',
        agent_id: 'worker',
        session_id: 'session-child',
        status: 'queued',
        delivery: 'automatic',
      },
      artifacts: [],
    },
    ...overrides,
  };
}

function backgroundBashTool(overrides = {}) {
  return {
    type: 'tool_call',
    id: 'bash-background',
    name: 'bash',
    status: 'success',
    resultEvent: { type: 'tool_call_result' },
    arguments: { command: 'npm run dev', mode: 'background' },
    result: {
      ok: true,
      data: {
        session_id: 'process-one',
        status: 'running',
        delivery: 'automatic',
      },
      artifacts: [],
    },
    ...overrides,
  };
}

describe('chatTimelinePresentation', () => {
  beforeEach(() => {
    init('en');
  });

  it('unwraps successful read content and hides envelope metadata', () => {
    const value = compactToolValue(
      {
        ok: true,
        data: { content: 'file contents' },
        artifacts: [{ id: 'internal' }],
      },
      { preferPayload: true, toolName: 'read' },
    );

    expect(value).toBe('file contents');
  });

  it('renders actual line breaks in nested Tool Result strings', () => {
    const value = compactToolValue(
      {
        ok: true,
        data: {
          summary: 'first line\nsecond line',
          nested: { text: 'nested first\r\nnested second' },
          literal: 'keep \\n as text',
        },
      },
      { preferPayload: true, toolName: 'probe' },
    );

    expect(value).toBe(
      'summary: first line\n  second line\n' +
        'nested: text: nested first\r\n    nested second\n' +
        'literal: keep \\n as text',
    );
  });

  it('renders Tool Args line breaks without JSON string wrappers', () => {
    const value = compactToolValue({ query: 'first\nsecond' });

    expect(value).toBe('query: first\n  second');
  });

  it('keeps scalar types available after removing String wrappers', () => {
    const presentation = toolDetailPresentation({
      stringFalse: 'false',
      booleanFalse: false,
      count: 3,
      missing: null,
    });

    expect(presentation.fields).toEqual([
      { key: 'stringFalse', kind: 'string', text: 'false' },
      { key: 'booleanFalse', kind: 'boolean', text: 'false' },
      { key: 'count', kind: 'number', text: '3' },
      { key: 'missing', kind: 'null', text: 'null' },
    ]);
  });

  it('uses the path summary without exposing edit replacement text', () => {
    const summary = toolArgumentSummary({
      name: 'edit',
      arguments: {
        path: 'notes/plan.md',
        old_string: 'before',
        new_string: 'after',
      },
    });

    expect(summary).toBe('notes/plan.md');
  });

  it('labels a still-streaming tool row from its preview arguments', () => {
    const summary = toolArgumentSummary({
      name: 'write',
      arguments: undefined,
      previewArguments: { path: 'notes/plan.md' },
      partialArgumentsText: '{"path": "notes/plan.md", "content": "# Pl',
    });

    expect(summary).toBe('notes/plan.md');
  });

  it('prefers parsed arguments over stale preview arguments', () => {
    const summary = toolArgumentSummary({
      name: 'write',
      arguments: { path: 'final/path.md', content: '# Done' },
      previewArguments: { path: 'stale/path.md' },
    });

    expect(summary).toBe('final/path.md');
  });

  it('summarizes the current flat process action contract', () => {
    const summary = toolArgumentSummary({
      name: 'process',
      arguments: { action: 'status', session_id: 'process-1' },
    });

    expect(summary).toBe('status · process-1');
  });

  it('keeps legacy process request operations visible after history reload', () => {
    const summary = toolArgumentSummary({
      name: 'process',
      arguments: { request: { operation: 'list' } },
    });

    expect(summary).toBe('list');
  });

  it('keeps a streaming row unlabeled while no preview field is complete', () => {
    const summary = toolArgumentSummary({
      name: 'write',
      arguments: undefined,
      previewArguments: null,
      partialArgumentsText: '{"pa',
    });

    expect(summary).toBe('');
  });

  it('keeps structured facts intact while truncating a long description', () => {
    const presentation = toolRowPresentation({
      name: 'grep',
      display: {
        version: 1,
        primary: [
          {
            kind: 'description',
            value:
              'Find every version variable and every derived alias across all runtime packages',
            truncate: 'end',
            tooltip: 'truncated',
            max_characters: 64,
            quote: true,
          },
        ],
        facts: [{ kind: 'count', value: 10, unit: 'matches', at_least: false }],
      },
    });

    expect(presentation.primary[0].text).toHaveLength(64);
    expect(presentation.primary[0].text.endsWith('…')).toBe(true);
    expect(presentation.primary[0].tooltipText).toContain(
      'across all runtime packages',
    );
    expect(presentation.facts[0].text).toBe('10 matches');
  });

  it('compacts a deep read path from the start and keeps its full tooltip', () => {
    const presentation = toolRowPresentation({
      name: 'read',
      display: {
        version: 1,
        primary: [
          {
            kind: 'path',
            value: 'C:/workspace/packages/chat/components/ToolRow.svelte',
            full_value: 'C:/workspace/packages/chat/components/ToolRow.svelte',
            truncate: 'start',
            tooltip: 'always',
            max_characters: 64,
            copyable: true,
          },
        ],
        facts: [],
      },
    });

    expect(presentation.primary[0]).toMatchObject({
      text: '…/chat/components/ToolRow.svelte',
      fullText: 'C:/workspace/packages/chat/components/ToolRow.svelte',
      copyable: true,
    });
  });

  it('preserves filename-only read labels', () => {
    const presentation = toolRowPresentation({
      name: 'read',
      display: {
        primary: [
          {
            kind: 'path',
            value: 'ToolRow.svelte',
            full_value: 'C:/workspace/ToolRow.svelte',
            truncate: 'start',
            tooltip: 'always',
          },
        ],
      },
    });

    expect(presentation.primary[0].text).toBe('ToolRow.svelte');
    expect(presentation.primary[0].tooltipText).toBe(
      'C:/workspace/ToolRow.svelte',
    );
  });

  it('middle-truncates URLs and localizes singular and lower-bound counts', () => {
    const presentation = toolRowPresentation({
      name: 'web_search',
      display: {
        primary: [
          {
            kind: 'url',
            value:
              'https://example.com/a/very/long/path/to/a/result?query=versions',
            truncate: 'middle',
            max_characters: 24,
          },
        ],
        facts: [
          { kind: 'count', value: 1, unit: 'results', at_least: false },
          { kind: 'count', value: 10, unit: 'matches', at_least: true },
        ],
      },
    });

    expect(presentation.primary[0].text).toHaveLength(24);
    expect(presentation.primary[0].text).toContain('…');
    expect(presentation.facts.map((fact) => fact.text)).toEqual([
      '1 result',
      '10+ matches',
    ]);
  });

  it('does not expose arbitrary arguments for an unknown Tool', () => {
    const presentation = toolRowPresentation({
      name: 'extension_private_probe',
      arguments: { token: 'do-not-render', operation: 'inspect' },
    });

    expect(presentation).toEqual({ primary: [], facts: [] });
  });

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
        processSessionId: 'process-one',
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

  it('marks only running bash tool rows as cancellable', () => {
    expect(
      isRowCancellable({
        kind: 'tool_call',
        toolName: 'bash',
        toolStatus: 'running',
      }),
    ).toBe(true);
    expect(
      isRowCancellable({
        kind: 'tool_call',
        toolName: 'bash',
        toolStatus: 'success',
      }),
    ).toBe(false);
    expect(
      isRowCancellable({
        kind: 'tool_call',
        toolName: 'bash',
        toolStatus: 'failed',
      }),
    ).toBe(false);
    expect(
      isRowCancellable({
        kind: 'tool_call',
        toolName: 'bash',
        toolStatus: 'cancelled',
      }),
    ).toBe(false);
  });

  it('does not mark streaming preview tool rows as cancellable', () => {
    expect(
      isRowCancellable({
        kind: 'tool_call',
        toolName: 'bash',
        toolStatus: 'running',
        streaming: true,
      }),
    ).toBe(false);
  });

  it('renders a streaming preview tool row before its started event', () => {
    const assistantRun = {
      items: [
        {
          type: 'tool_call',
          streaming: true,
          name: 'session_search',
          partialArgumentsText: '{"query": "ca',
          startedEvent: null,
          resultEvent: null,
          stdout: '',
          stderr: '',
        },
      ],
    };

    expect(visibleRunChildren(assistantRun)).toHaveLength(1);
  });

  it('marks only the latest visible streaming child as the active work', () => {
    const reasoning = {
      id: 'reasoning-one',
      type: 'reasoning',
      content: 'Inspect the request.',
      streaming: true,
    };
    const answer = {
      id: 'answer-one',
      type: 'assistant_output',
      content: 'I will inspect it.',
      streaming: true,
    };
    const tool = {
      id: 'tool-one',
      type: 'tool_call',
      name: 'read',
      streaming: true,
      status: 'preparing',
    };
    const assistantRun = {
      status: 'running',
      items: [reasoning, answer, tool],
    };

    expect(isRunChildWorking(assistantRun, reasoning)).toBe(false);
    expect(isRunChildWorking(assistantRun, answer)).toBe(false);
    expect(isRunChildWorking(assistantRun, tool)).toBe(true);
  });

  it('does not expose working text after the Run becomes terminal', () => {
    const answer = {
      id: 'answer-one',
      type: 'assistant_output',
      content: 'Done.',
      streaming: true,
    };

    expect(
      isRunChildWorking({ status: 'completed', items: [answer] }, answer),
    ).toBe(false);
  });

  it('does not mark non-bash tool rows as cancellable', () => {
    expect(
      isRowCancellable({
        kind: 'tool_call',
        toolName: 'read',
        toolStatus: 'running',
      }),
    ).toBe(false);
    expect(
      isRowCancellable({
        kind: 'tool_call',
        toolName: 'edit',
        toolStatus: 'running',
      }),
    ).toBe(false);
    expect(
      isRowCancellable({
        kind: 'tool_call',
        toolName: 'grep',
        toolStatus: 'running',
      }),
    ).toBe(false);
  });

  it('marks only running sub-agent rows as cancellable', () => {
    expect(isRowCancellable({ kind: 'sub_agent', dotStatus: 'running' })).toBe(
      true,
    );
    expect(isRowCancellable({ kind: 'sub_agent', dotStatus: 'success' })).toBe(
      false,
    );
    expect(isRowCancellable({ kind: 'sub_agent', dotStatus: 'failed' })).toBe(
      false,
    );
    expect(
      isRowCancellable({ kind: 'sub_agent', dotStatus: 'cancelled' }),
    ).toBe(false);
  });

  it('rejects unknown row shapes', () => {
    expect(isRowCancellable(null)).toBe(false);
    expect(isRowCancellable(undefined)).toBe(false);
    expect(isRowCancellable({})).toBe(false);
    expect(isRowCancellable({ kind: 'reasoning' })).toBe(false);
  });
});

describe('errorMessagePresentation', () => {
  it('extracts the nested provider message and keeps the prefix', () => {
    const presentation = errorMessagePresentation(
      'Provider error: 400 {"error":{"message":"max_tokens: Field required","code":"invalid_request_body"}}',
    );

    expect(presentation.summary).toBe(
      'Provider error: 400 max_tokens: Field required',
    );
    expect(presentation.details).toContain('"code": "invalid_request_body"');
  });

  it('extracts a top-level message field', () => {
    const presentation = errorMessagePresentation(
      'Provider error: 400 {"message":"max_tokens: Field required"}',
    );

    expect(presentation.summary).toBe(
      'Provider error: 400 max_tokens: Field required',
    );
    expect(presentation.details).toContain('"message"');
  });

  it('prefers the deepest error.message over sibling fields', () => {
    const presentation = errorMessagePresentation(
      'Rate limited: 429 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"},"request_id":"req_1"}',
    );

    expect(presentation.summary).toBe('Rate limited: 429 Overloaded');
    expect(presentation.details).toContain('"request_id": "req_1"');
  });

  it('keeps the prefix as summary when the body has no message', () => {
    const presentation = errorMessagePresentation(
      'Provider error: 500 {"status":"boom"}',
    );

    expect(presentation.summary).toBe('Provider error: 500');
    expect(presentation.details).toContain('"status": "boom"');
  });

  it('shows the upstream rate-limit detail and provider name directly', () => {
    const presentation = errorMessagePresentation(
      'Rate limited: 429 {"error":{"message":"Provider returned error","code":429,' +
        '"metadata":{"raw":"stealth/ox-alpha is temporarily rate-limited upstream. Please retry shortly.",' +
        '"provider_name":"Stealth","remedy_hint":"Retry shortly, add your own provider key"}},"user_id":"u1"}',
    );

    expect(presentation.summary).toBe(
      'Rate limited: 429 stealth/ox-alpha is temporarily rate-limited upstream. ' +
        'Please retry shortly. (via Stealth)',
    );
    expect(presentation.summary).not.toContain('Provider returned error');
    expect(presentation.summary).not.toContain('remedy_hint');
    expect(presentation.details).toContain('"remedy_hint"');
  });

  it('reads router metadata from a top-level error body', () => {
    const presentation = errorMessagePresentation(
      'Provider returned error: {"message":"Provider returned error","code":502,' +
        '"metadata":{"raw":"upstream connection reset","provider_name":"Morph"}}',
    );

    expect(presentation.summary).toBe(
      'Provider returned error: upstream connection reset (via Morph)',
    );
  });

  it('does not duplicate the message when the upstream detail matches it', () => {
    const presentation = errorMessagePresentation(
      'Provider error: 500 {"message":"overloaded","metadata":{"raw":"overloaded"}}',
    );

    expect(presentation.summary).toBe('Provider error: 500 overloaded');
  });

  it('returns plain text unchanged without an embedded JSON object', () => {
    expect(errorMessagePresentation('Connection refused')).toEqual({
      summary: 'Connection refused',
      details: '',
    });
  });

  it('returns the full text when the embedded JSON does not parse', () => {
    const text = 'Provider error: 400 {broken json';
    expect(errorMessagePresentation(text)).toEqual({
      summary: text,
      details: '',
    });
  });

  it('handles non-string input', () => {
    expect(errorMessagePresentation(null)).toEqual({
      summary: '',
      details: '',
    });
  });

  it('labels a user message with the sender display name when present', () => {
    const message = {
      role: 'user',
      content: 'hello',
      sender: { id: '50', display_name: 'Alice' },
    };

    expect(labelForMessage(message)).toBe('ALICE');
  });

  it('labels a user message without sender as You', () => {
    expect(labelForMessage({ role: 'user', content: 'hello' })).toBe('YOU');
  });

  it('falls back to You when the sender display name is blank', () => {
    const message = {
      role: 'user',
      content: 'hello',
      sender: { id: '50', display_name: '   ' },
    };

    expect(labelForMessage(message)).toBe('YOU');
  });

  it('labels a live user_message_persisted event with the sender display name', () => {
    const event = {
      type: 'user_message_persisted',
      payload: {
        message: {
          role: 'user',
          content: 'hello',
          sender: { id: '50', display_name: 'Alice' },
        },
      },
    };

    expect(labelForEvent(event)).toBe('ALICE');
  });

  it('labels a live user_message_persisted event without sender as You', () => {
    const event = {
      type: 'user_message_persisted',
      payload: { message: { role: 'user', content: 'hello' } },
    };

    expect(labelForEvent(event)).toBe('YOU');
  });
});

describe('compactionSummaryText', () => {
  it('returns the checkpoint content byte-for-byte without trimming or formatting', () => {
    const summary = '\n# Heading\n\n<tag> & *literal*\n';

    expect(
      compactionSummaryText({
        message: { role: 'compaction_checkpoint', content: summary },
      }),
    ).toBe(summary);
    expect(compactionSummaryText({ message: { content: null } })).toBe('');
    expect(compactionSummaryText(null)).toBe('');
  });
});

describe('takeoverSeparatorLabel', () => {
  beforeEach(() => {
    init('en');
  });

  it('composes the label from the parsed from/to addresses', () => {
    const label = takeoverSeparatorLabel({
      content: JSON.stringify({ from: 'assistant', to: 'builder@vbot' }),
    });
    expect(label).toBe('Taken over by assistant → builder@vbot');
  });

  it('keeps the raw addresses verbatim (identity and project forms)', () => {
    const label = takeoverSeparatorLabel({
      content: JSON.stringify({ from: 'reviewer@vbot', to: 'assistant' }),
    });
    expect(label).toContain('reviewer@vbot');
    expect(label).toContain('assistant');
  });

  it('falls back to a generic label when the content is malformed', () => {
    expect(takeoverSeparatorLabel({ content: 'not json' })).toBe(
      'Session taken over',
    );
    expect(takeoverSeparatorLabel({ content: '' })).toBe('Session taken over');
    expect(takeoverSeparatorLabel({})).toBe('Session taken over');
    expect(takeoverSeparatorLabel(null)).toBe('Session taken over');
  });

  it('falls back to generic when one address is missing', () => {
    expect(
      takeoverSeparatorLabel({ content: JSON.stringify({ from: 'a' }) }),
    ).toBe('Session taken over');
    expect(
      takeoverSeparatorLabel({ content: JSON.stringify({ to: 'b' }) }),
    ).toBe('Session taken over');
  });
});

describe('runFooterParts', () => {
  beforeEach(() => {
    init('en');
  });

  it('uses only the canonical Iteration count from the backend', () => {
    const parts = runFooterParts({
      status: 'completed',
      durationMs: 1000,
      iterationCount: 2,
      outputs: [{ content: 'done' }],
      tools: Array.from({ length: 5 }, () => ({ name: 'read' })),
    });

    expect(parts[0]).toBe('Completed');
    expect(parts).toContain('2 iter');
  });

  it('does not estimate an Iteration count when backend truth is absent', () => {
    const parts = runFooterParts({
      status: 'completed',
      durationMs: 1000,
      outputs: [{ content: 'done' }, { content: 'another message' }],
      tools: [{ name: 'read' }],
    });

    expect(parts).not.toContain('3 iter');
    expect(parts.every((part) => !part.endsWith(' iter'))).toBe(true);
  });

  it('shows zero before the first Model response returns', () => {
    const parts = runFooterParts({
      status: 'running',
      durationMs: null,
      iterationCount: 0,
      outputs: [],
      tools: [],
    });

    expect(parts[0]).toBe('Running');
    expect(parts).toContain('0 iter');
  });

  it('shows the Cancelled label plus the runtime for a cancelled run', () => {
    const parts = runFooterParts({
      status: 'cancelled',
      durationMs: 12000,
      outputs: [{ content: 'partial' }],
      tools: [],
    });

    expect(parts).toContain('Cancelled');
    const cancelledIndex = parts.indexOf('Cancelled');
    // The duration renders after the user-action label, never instead of it.
    expect(parts.length).toBeGreaterThan(cancelledIndex + 1);
  });

  it('shows only the Cancelled label when a cancelled run has no timing', () => {
    const parts = runFooterParts({
      status: 'cancelled',
      durationMs: null,
      outputs: [],
      tools: [],
    });

    expect(parts).toContain('Cancelled');
  });

  it('shows terminal status and canonical duration for completed runs', () => {
    const parts = runFooterParts({
      status: 'completed',
      durationMs: 8000,
      outputs: [{ content: 'done' }],
      tools: [],
    });

    expect(parts).toContain('Completed');
    expect(parts).toContain('8.0s');
  });

  it('computes a live Run duration from its start timestamp', () => {
    const parts = runFooterParts(
      {
        status: 'running',
        durationMs: null,
        startTimestamp: '2026-08-05T18:00:00.000Z',
        iterationCount: 0,
      },
      Date.parse('2026-08-05T18:00:05.250Z'),
    );

    expect(parts).toContain('Running');
    expect(parts).toContain('5.3s');
  });

  it('formats minute-scale durations as minutes and seconds', () => {
    const parts = runFooterParts({
      status: 'completed',
      durationMs: 325000,
      outputs: [{ content: 'done' }],
      tools: [],
    });

    expect(parts).toContain('5m 25s');
  });

  it('formats hour-scale durations as hours and minutes without seconds', () => {
    const parts = runFooterParts({
      status: 'completed',
      durationMs: 5071000,
      outputs: [{ content: 'done' }],
      tools: [],
    });

    expect(parts).toContain('1h 24m');
  });

  it('reports live Provider liveness only on the separate notice line', () => {
    const assistantRun = {
      status: 'running',
      durationMs: null,
      outputs: [{ content: 'Writing the plan now.' }],
      tools: [],
      providerHeartbeat: { idleSeconds: 75.4 },
    };

    expect(runFooterNotice(assistantRun)).toBe(
      'Provider connected · waiting 75s for the next model chunk',
    );
    expect(runFooterParts(assistantRun)).not.toContain(
      'Provider connected · waiting 75s for the next model chunk',
    );
  });

  it('returns no notice when the run is not running or has no heartbeat', () => {
    expect(
      runFooterNotice({
        status: 'completed',
        providerHeartbeat: { idleSeconds: 75.4 },
      }),
    ).toBe('');
    expect(runFooterNotice({ status: 'running' })).toBe('');
  });
});

describe('isToolPreparing', () => {
  it('is true only while a tool call is still streaming (not yet dispatched)', () => {
    expect(isToolPreparing({ status: 'preparing' })).toBe(true);
  });

  it('is false once the call is dispatched or settled', () => {
    for (const status of [
      'running',
      'success',
      'completed',
      'failed',
      'cancelled',
    ]) {
      expect(isToolPreparing({ status })).toBe(false);
    }
  });

  it('tolerates a missing tool', () => {
    expect(isToolPreparing(null)).toBe(false);
    expect(isToolPreparing(undefined)).toBe(false);
  });
});

describe('runChangeStats', () => {
  beforeEach(() => {
    init('en');
  });

  function editTool({ path, added, removed, name = 'edit' }) {
    return {
      type: 'tool_call',
      id: `tool-${path}`,
      name,
      status: 'success',
      arguments: { path },
      startedEvent: {
        type: 'tool_call_started',
        payload: { tool_call: { id: `call-${path}`, name } },
      },
      resultEvent: {
        type: 'tool_call_result',
        payload: {
          tool_call: { id: `call-${path}`, name },
          display: {
            version: 1,
            summary: path,
            hidden_argument_keys: [],
            primary: [],
            facts: [
              { kind: 'line_change', change: 'added', value: added },
              { kind: 'line_change', change: 'removed', value: removed },
            ],
          },
        },
      },
    };
  }

  it('sums line changes and counts distinct files per run', () => {
    const stats = runChangeStats({
      type: 'assistant_run',
      items: [
        editTool({ path: 'a.txt', added: 3, removed: 2 }),
        editTool({ path: 'a.txt', added: 1, removed: 0 }),
        editTool({ path: 'b.txt', added: 5, removed: 1 }),
      ],
    });

    expect(stats).toEqual({
      files: 2,
      added: 9,
      removed: 3,
      paths: ['a.txt', 'b.txt'],
    });
  });

  it('counts a write of a new file as added lines only', () => {
    const stats = runChangeStats({
      type: 'assistant_run',
      items: [
        editTool({ path: 'new.txt', added: 4, removed: 0, name: 'write' }),
      ],
    });

    expect(stats).toEqual({
      files: 1,
      added: 4,
      removed: 0,
      paths: ['new.txt'],
    });
  });

  it('ignores non-file tools and tools without line-change facts', () => {
    const stats = runChangeStats({
      type: 'assistant_run',
      items: [
        {
          type: 'tool_call',
          id: 'tool-read',
          name: 'read',
          status: 'success',
          arguments: { path: 'a.txt' },
          startedEvent: {
            type: 'tool_call_started',
            payload: { tool_call: { id: 'call-read', name: 'read' } },
          },
        },
        {
          type: 'tool_call',
          id: 'tool-bash',
          name: 'bash',
          status: 'success',
          arguments: { command: 'ls' },
          startedEvent: {
            type: 'tool_call_started',
            payload: { tool_call: { id: 'call-bash', name: 'bash' } },
          },
        },
      ],
    });

    expect(stats).toBeNull();
  });

  it('returns null for a run without changes', () => {
    expect(runChangeStats({ type: 'assistant_run', items: [] })).toBeNull();
  });

  it('prefers the server-computed git-style stats over the tool-fact sum', () => {
    const stats = runChangeStats({
      type: 'assistant_run',
      changeStats: {
        files: 1,
        added: 1,
        removed: 1,
        paths: ['a.txt'],
      },
      items: [
        editTool({ path: 'a.txt', added: 3, removed: 2 }),
        editTool({ path: 'a.txt', added: 1, removed: 0 }),
      ],
    });

    expect(stats).toEqual({
      files: 1,
      added: 1,
      removed: 1,
      paths: ['a.txt'],
    });
  });

  it('falls back to the tool-fact sum when the server stats are malformed', () => {
    const stats = runChangeStats({
      type: 'assistant_run',
      changeStats: { files: 'x', added: 1, removed: 1, paths: [] },
      items: [editTool({ path: 'a.txt', added: 3, removed: 2 })],
    });

    expect(stats).toEqual({
      files: 1,
      added: 3,
      removed: 2,
      paths: ['a.txt'],
    });
  });

  it('returns null for a server-reported zero instead of the tool-fact sum', () => {
    const stats = runChangeStats({
      type: 'assistant_run',
      changeStats: { files: 0, added: 0, removed: 0, paths: [] },
      items: [editTool({ path: 'a.txt', added: 3, removed: 2 })],
    });

    expect(stats).toBeNull();
  });
});

describe('sessionChangeStats', () => {
  beforeEach(() => {
    init('en');
  });

  it('sums every run and deduplicates files across runs', () => {
    const stats = sessionChangeStats([
      {
        type: 'assistant_run',
        items: [
          {
            type: 'tool_call',
            id: 'tool-1',
            name: 'edit',
            status: 'success',
            arguments: { path: 'a.txt' },
            startedEvent: {
              type: 'tool_call_started',
              payload: { tool_call: { id: 'call-1', name: 'edit' } },
            },
            resultEvent: {
              type: 'tool_call_result',
              payload: {
                tool_call: { id: 'call-1', name: 'edit' },
                display: {
                  version: 1,
                  summary: 'a.txt',
                  hidden_argument_keys: [],
                  primary: [],
                  facts: [
                    { kind: 'line_change', change: 'added', value: 3 },
                    { kind: 'line_change', change: 'removed', value: 2 },
                  ],
                },
              },
            },
          },
        ],
      },
      {
        type: 'assistant_run',
        items: [
          {
            type: 'tool_call',
            id: 'tool-2',
            name: 'edit',
            status: 'success',
            arguments: { path: 'a.txt' },
            startedEvent: {
              type: 'tool_call_started',
              payload: { tool_call: { id: 'call-2', name: 'edit' } },
            },
            resultEvent: {
              type: 'tool_call_result',
              payload: {
                tool_call: { id: 'call-2', name: 'edit' },
                display: {
                  version: 1,
                  summary: 'a.txt',
                  hidden_argument_keys: [],
                  primary: [],
                  facts: [
                    { kind: 'line_change', change: 'added', value: 1 },
                    { kind: 'line_change', change: 'removed', value: 0 },
                  ],
                },
              },
            },
          },
          {
            type: 'tool_call',
            id: 'tool-3',
            name: 'write',
            status: 'success',
            arguments: { path: 'b.txt' },
            startedEvent: {
              type: 'tool_call_started',
              payload: { tool_call: { id: 'call-3', name: 'write' } },
            },
            resultEvent: {
              type: 'tool_call_result',
              payload: {
                tool_call: { id: 'call-3', name: 'write' },
                display: {
                  version: 1,
                  summary: 'b.txt',
                  hidden_argument_keys: [],
                  primary: [],
                  facts: [
                    { kind: 'line_change', change: 'added', value: 5 },
                    { kind: 'line_change', change: 'removed', value: 0 },
                  ],
                },
              },
            },
          },
        ],
      },
    ]);

    expect(stats).toEqual({
      files: 2,
      added: 9,
      removed: 2,
      paths: ['a.txt', 'b.txt'],
    });
  });

  it('returns null for an empty timeline', () => {
    expect(sessionChangeStats([])).toBeNull();
  });

  it('sums server-computed stats across runs and deduplicates files', () => {
    const stats = sessionChangeStats([
      {
        type: 'assistant_run',
        changeStats: { files: 1, added: 1, removed: 1, paths: ['a.txt'] },
        items: [],
      },
      {
        type: 'assistant_run',
        changeStats: {
          files: 2,
          added: 5,
          removed: 0,
          paths: ['a.txt', 'b.txt'],
        },
        items: [],
      },
    ]);

    expect(stats).toEqual({
      files: 2,
      added: 6,
      removed: 1,
      paths: ['a.txt', 'b.txt'],
    });
  });
});

describe('changeStatsLabel', () => {
  beforeEach(() => {
    init('en');
  });

  it('formats the compact one-line label', () => {
    expect(changeStatsLabel({ files: 5, added: 151, removed: 15 })).toBe(
      '5 files changed, +151 -15',
    );
  });

  it('uses the singular file form', () => {
    expect(changeStatsLabel({ files: 1, added: 2, removed: 0 })).toBe(
      '1 file changed, +2 -0',
    );
  });

  it('returns an empty string for null stats', () => {
    expect(changeStatsLabel(null)).toBe('');
  });
});

describe('runFooterParts', () => {
  beforeEach(() => {
    init('en');
  });

  it('shows status and duration for a completed run', () => {
    const parts = runFooterParts({
      status: 'completed',
      durationMs: 8000,
      items: [
        {
          type: 'tool_call',
          id: 'tool-1',
          name: 'edit',
          status: 'success',
          arguments: { path: 'a.txt' },
          startedEvent: {
            type: 'tool_call_started',
            payload: { tool_call: { id: 'call-1', name: 'edit' } },
          },
          resultEvent: {
            type: 'tool_call_result',
            payload: {
              tool_call: { id: 'call-1', name: 'edit' },
              display: {
                version: 1,
                summary: 'a.txt',
                hidden_argument_keys: [],
                primary: [],
                facts: [
                  { kind: 'line_change', change: 'added', value: 3 },
                  { kind: 'line_change', change: 'removed', value: 2 },
                ],
              },
            },
          },
        },
      ],
    });

    expect(parts).toEqual(['Completed', '8.0s']);
  });

  it('ticks the live duration while the run is running', () => {
    const parts = runFooterParts(
      {
        status: 'running',
        durationMs: null,
        startTimestamp: '2026-08-05T18:00:00.000Z',
        items: [],
      },
      Date.parse('2026-08-05T18:00:05.250Z'),
    );

    expect(parts).toEqual(['Running', '5.3s']);
  });

  it('omits the change part when the run changed no files', () => {
    const parts = runFooterParts({
      status: 'completed',
      durationMs: 1000,
      items: [],
    });
    expect(parts).toEqual(['Completed', '1.0s']);
  });

  it('includes the iteration count after the duration', () => {
    const parts = runFooterParts({
      status: 'completed',
      durationMs: 8000,
      iterationCount: 3,
      items: [],
    });
    expect(parts).toEqual(['Completed', '8.0s', '3 iter']);
  });

  it('shows the end time once the run reached a terminal state', () => {
    const parts = runFooterParts({
      status: 'completed',
      durationMs: 8000,
      endTimestamp: '2026-08-05T18:20:00Z',
      items: [],
    });

    expect(parts[parts.length - 1]).toBe(formatTime('2026-08-05T18:20:00Z'));
  });

  it('shows no end time while the run is still running', () => {
    const parts = runFooterParts({
      status: 'running',
      durationMs: null,
      endTimestamp: '2026-08-05T18:20:00Z',
      items: [],
    });

    expect(parts).not.toContain(formatTime('2026-08-05T18:20:00Z'));
    expect(
      parts.every((part) => !part.includes('PM') && !part.includes('AM')),
    ).toBe(true);
  });
});

describe('changeStatsParts', () => {
  beforeEach(() => {
    init('en');
  });

  it('splits the change stats into file, added, and removed parts', () => {
    expect(changeStatsParts({ files: 5, added: 151, removed: 15 })).toEqual([
      { kind: 'files', text: '5 files changed,' },
      { kind: 'added', text: '+151' },
      { kind: 'removed', text: '-15' },
    ]);
  });

  it('uses the singular file form', () => {
    expect(changeStatsParts({ files: 1, added: 2, removed: 0 })).toEqual([
      { kind: 'files', text: '1 file changed,' },
      { kind: 'added', text: '+2' },
      { kind: 'removed', text: '-0' },
    ]);
  });

  it('returns an empty array for null stats', () => {
    expect(changeStatsParts(null)).toEqual([]);
  });
});

describe('changeStatsTooltip', () => {
  it('lists every changed file, one per line', () => {
    expect(
      changeStatsTooltip({
        files: 2,
        added: 9,
        removed: 3,
        paths: ['a.txt', 'b.txt'],
      }),
    ).toBe('a.txt\nb.txt');
  });

  it('returns an empty string when no paths are known', () => {
    expect(changeStatsTooltip({ files: 1, added: 2, removed: 0 })).toBe('');
    expect(changeStatsTooltip(null)).toBe('');
  });
});

describe('reflection panel helpers', () => {
  it('classifies reflection run kinds and derives their review scope', () => {
    expect(isReflectionRunKind('memory_reflection')).toBe(true);
    expect(isReflectionRunKind('skill_reflection')).toBe(true);
    expect(isReflectionRunKind('reflection')).toBe(true);
    expect(isReflectionRunKind('user')).toBe(false);
    expect(isReflectionRunKind(undefined)).toBe(false);

    expect(reflectionScopeForRunKind('memory_reflection')).toBe('memory');
    expect(reflectionScopeForRunKind('skill_reflection')).toBe('skill');
    expect(reflectionScopeForRunKind('reflection')).toBe('combined');
    expect(reflectionScopeForRunKind('cron')).toBe('');
  });

  it('projects tracking entries into rows sorted running-first, newest first', () => {
    const sessionState = {
      reflectionTasks: {
        'run-old-finished': {
          sessionId: 'fork-a',
          runKind: 'skill_reflection',
          status: 'completed',
          startedAt: '2026-08-24T08:00:00.000Z',
        },
        'run-running': {
          sessionId: 'fork-b',
          runKind: 'memory_reflection',
          status: 'running',
          startedAt: '2026-08-24T07:00:00.000Z',
        },
        'run-newer-finished': {
          sessionId: 'fork-c',
          runKind: 'memory_reflection',
          status: 'failed',
          startedAt: '2026-08-24T10:00:00.000Z',
        },
        'run-broken': { sessionId: '' },
      },
    };

    const rows = reflectionTaskRows(sessionState);

    expect(rows.map((row) => row.runId)).toEqual([
      'run-running',
      'run-newer-finished',
      'run-old-finished',
    ]);
    expect(rows[0]).toMatchObject({
      sessionId: 'fork-b',
      scope: 'memory',
      status: 'running',
    });
    expect(rows[1].scope).toBe('memory');
    expect(rows[2].scope).toBe('skill');
  });

  it('tolerates a session state without tracking entries', () => {
    expect(reflectionTaskRows(undefined)).toEqual([]);
    expect(reflectionTaskRows({})).toEqual([]);
  });

  it('formats coarse elapsed labels and stays empty without a parseable start', () => {
    const start = '2026-08-24T10:00:00.000Z';

    expect(reflectionElapsedLabel(start, Date.parse(start) + 45_123)).toBe(
      '45s',
    );
    expect(reflectionElapsedLabel(start, Date.parse(start) + 125_000)).toBe(
      '2m',
    );
    expect(reflectionElapsedLabel('', Date.now())).toBe('');
    expect(reflectionElapsedLabel(start, Number.NaN)).toBe('');
  });
});

describe('reasoningDurationLabel', () => {
  it('prefers the persisted duration from the stable boundary', () => {
    expect(reasoningDurationLabel({ durationMs: 4200 }, Date.now())).toBe(
      '4.2s',
    );
  });

  it('prefers the measured span over the frozen estimate', () => {
    expect(
      reasoningDurationLabel({ durationMs: 4200, durationEstimateMs: 3000 }),
    ).toBe('4.2s');
  });

  it('shows the frozen estimate from when the deltas stopped growing', () => {
    expect(
      reasoningDurationLabel({
        durationMs: null,
        durationEstimateMs: 3000,
        streaming: true,
        timestamp: '2026-08-24T10:00:00+00:00',
      }),
    ).toBe('3.0s');
  });

  it('ticks live from the first streamed delta while streaming', () => {
    const child = {
      durationMs: null,
      streaming: true,
      timestamp: '2026-08-24T10:00:00+00:00',
    };

    expect(
      reasoningDurationLabel(child, Date.parse(child.timestamp) + 8300),
    ).toBe('8.3s');
  });

  it('stays empty without a measurable span and after a non-streamed block', () => {
    expect(reasoningDurationLabel({ durationMs: null, streaming: false })).toBe(
      '',
    );
    expect(
      reasoningDurationLabel(
        { durationMs: null, streaming: true, timestamp: null },
        Date.now(),
      ),
    ).toBe('');
    // Non-streamed blocks never estimate: only the persisted value counts.
    expect(
      reasoningDurationLabel(
        { durationMs: null, streaming: false },
        Date.now(),
      ),
    ).toBe('');
  });
});
