import { beforeEach, describe, expect, it } from 'vitest';

import {
  compactToolValue,
  isRowCancellable,
  subAgentDisplayResult,
  subAgentDotStatus,
  subAgentEffectiveRunId,
  subAgentLastToolName,
  subAgentNeedsStatusVerification,
  subAgentResultEntryAllowsFetch,
  subAgentResultKey,
  subAgentResultTextFromMessages,
  subAgentRunDurationMs,
  subAgentShouldFetchResult,
  subAgentToolStatusLabel,
  toolArgumentSummary,
  visibleRunChildren,
} from '../chatTimelinePresentation.js';
import { init } from '../i18n.js';

function runningSubAgentTool(overrides = {}) {
  return {
    name: 'subagent',
    status: 'success',
    arguments: { agent_id: 'worker', content: 'Inspect the project' },
    result: {
      ok: true,
      error: null,
      data: {
        agent_id: 'worker',
        session_id: 'session-child',
        run_id: 'run-child',
        status: 'running',
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
    arguments: { agent_id: 'worker', content: 'Inspect the project' },
    result: {
      ok: true,
      error: null,
      data: {
        agent_id: 'worker',
        session_id: 'session-child',
        queue_item_id: 'queue-item-1',
        status: 'queued',
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

  it('uses the path summary without exposing edit replacement text', () => {
    const summary = toolArgumentSummary({
      name: 'edit',
      arguments: {
        path: 'notes/plan.md',
        oldString: 'before',
        newString: 'after',
      },
    });

    expect(summary).toBe('notes/plan.md');
  });

  it('projects an externally completed sub-agent run as successful', () => {
    const tool = {
      name: 'subagent',
      status: 'running',
      arguments: {
        agent_id: 'worker',
        content: 'Inspect the project',
      },
      subAgentSession: {
        agent_id: 'worker',
        session_id: 'session-child',
        run_id: 'run-child',
        status: 'running',
      },
      startedEvent: {},
    };

    const status = subAgentDotStatus(tool, null, {
      'run:run-child': 'completed',
    });

    expect(status).toBe('success');
  });

  it('ignores a stale session status for a row with a known run id (B6)', () => {
    const tool = runningSubAgentTool();

    // A previous run of the same reused child session left its terminal
    // status under the session key; this spawn's own run has no status yet,
    // so the dot must stay running instead of showing the old run's success.
    const status = subAgentDotStatus(tool, null, {
      'session:worker::session-child': 'completed',
    });

    expect(status).toBe('running');
  });

  it('settles a queued spawn through the queue→run mapping, not the session key', () => {
    const tool = queuedSubAgentTool();

    expect(
      subAgentDotStatus(tool, null, {
        'queueRun:queue-item-1': 'run-from-queue',
        'run:run-from-queue': 'completed',
        'session:worker::session-child': 'running',
      }),
    ).toBe('success');
    // Without the mapping the queued descriptor keeps the dot running.
    expect(subAgentDotStatus(tool, null, {})).toBe('running');
  });

  it('flags a frozen-descriptor running row for status verification when no live status has arrived', () => {
    const tool = runningSubAgentTool();

    // The dot says "running" but no run: or session: key exists in
    // subAgentStatuses, so the only signal is the persisted descriptor.
    expect(subAgentDotStatus(tool, null, {})).toBe('running');
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

  it('keys a sub-agent result by run id when known, by session otherwise', () => {
    expect(subAgentResultKey(runningSubAgentTool())).toBe(
      'worker::session-child::run-child',
    );
    expect(subAgentResultKey(queuedSubAgentTool())).toBe(
      'worker::session-child',
    );
    expect(
      subAgentResultKey(queuedSubAgentTool(), {
        'queueRun:queue-item-1': 'run-from-queue',
      }),
    ).toBe('worker::session-child::run-from-queue');
    expect(subAgentResultKey({ name: 'subagent', arguments: {} })).toBe('');
  });

  it('resolves the effective run id from the descriptor or the queue mapping', () => {
    expect(subAgentEffectiveRunId(runningSubAgentTool())).toBe('run-child');
    expect(subAgentEffectiveRunId(queuedSubAgentTool())).toBe('');
    expect(
      subAgentEffectiveRunId(queuedSubAgentTool(), {
        'queueRun:queue-item-1': 'run-from-queue',
      }),
    ).toBe('run-from-queue');
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

  it('requests a result only for a finished non-blocking spawn without inline output', () => {
    expect(subAgentShouldFetchResult(runningSubAgentTool(), 'success')).toBe(
      true,
    );
    // Still running -> no fetch yet.
    expect(subAgentShouldFetchResult(runningSubAgentTool(), 'running')).toBe(
      false,
    );
    // subagent_result lookups carry their own result already.
    expect(
      subAgentShouldFetchResult(
        { ...runningSubAgentTool(), name: 'subagent_result' },
        'success',
      ),
    ).toBe(false);
  });

  it('does not request a result when a blocking spawn already carries one', () => {
    const blockingTool = runningSubAgentTool({
      result: {
        ok: true,
        error: null,
        data: {
          agent_id: 'worker',
          session_id: 'session-child',
          run_id: 'run-child',
          status: 'completed',
          result: 'Final answer from the worker.',
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

  it('extracts the last assistant message text from session history', () => {
    const messages = [
      { role: 'user', content: 'Do the work' },
      { role: 'assistant', content: 'Working on it' },
      { role: 'tool', content: 'tool output' },
      { role: 'assistant', content: 'All done.' },
    ];

    expect(subAgentResultTextFromMessages(messages)).toBe('All done.');
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

  it('reports no last tool name for subagent_result rows', () => {
    const resultTool = runningSubAgentTool({
      name: 'subagent_result',
      arguments: { agent_id: 'worker', session_id: 'session-child' },
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
          agent_id: 'worker',
          session_id: 'session-child',
          run_id: 'run-child',
          status: 'completed',
          result: 'Final answer from the worker.',
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
    ];

    expect(subAgentResultTextFromMessages(messages)).toBe(
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
