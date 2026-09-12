import './chatTimeline.support.js';

import { describe, expect, it } from 'vitest';
import {
  appendRunEvent,
  createChatState,
  ensureSessionState,
  loadHistory,
  startRun,
  visibleTimelineItemsForRender,
} from '../chatState.js';

describe('cancelled run rendering', () => {
  it('renders a bare cancelled run row from an anchorless run_summary (zero-output cancel)', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-cancelled-empty',
    );
    loadHistory(sessionState, [
      { id: 'u1', role: 'user', content: 'tell me a story' },
      {
        id: 's1',
        role: 'run_summary',
        run_id: 'run-cancelled',
        status: 'cancelled',
        timing: {
          started_at: '2026-07-02T10:00:00+00:00',
          completed_at: '2026-07-02T10:00:12+00:00',
          duration_ms: 12000,
        },
      },
    ]);

    const items = visibleTimelineItemsForRender(sessionState);
    // The cancelled turn is not a hole: a run row with status + timing renders
    // after the user message even though no assistant/tool message anchors it.
    expect(items.map((item) => item.type)).toEqual([
      'message',
      'assistant_run',
    ]);
    const run = items[1];
    expect(run.status).toBe('cancelled');
    expect(run.runId).toBe('run-cancelled');
    expect(run.durationMs).toBe(12000);
    expect(run.items).toEqual([]);
  });

  it('renders a bare interrupted run row when recovery ended before visible output', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-interrupted-empty',
    );
    loadHistory(sessionState, [
      { id: 'u1', role: 'user', content: 'continue the task' },
      {
        id: 's1',
        role: 'run_summary',
        run_id: 'run-interrupted',
        status: 'interrupted',
        timing: { duration_ms: 3000 },
      },
    ]);

    const items = visibleTimelineItemsForRender(sessionState);
    expect(items.map((item) => item.type)).toEqual([
      'message',
      'assistant_run',
    ]);
    expect(items[1].status).toBe('interrupted');
    expect(items[1].runId).toBe('run-interrupted');
    expect(items[1].items).toEqual([]);
  });

  it('does not render bare rows for anchorless completed summaries (page-slice orphans)', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-orphan-summary',
    );
    loadHistory(sessionState, [
      {
        id: 's1',
        role: 'run_summary',
        run_id: 'run-old',
        status: 'completed',
        timing: { duration_ms: 5 },
      },
      { id: 'u1', role: 'user', content: 'next turn' },
    ]);

    const items = visibleTimelineItemsForRender(sessionState);
    expect(items.map((item) => item.type)).toEqual(['message']);
  });

  it('marks the run block cancelled when a preserved interrupted partial is followed by a cancelled summary', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-cancelled-partial',
    );
    loadHistory(sessionState, [
      { id: 'u1', role: 'user', content: 'tell me a story' },
      {
        id: 'a1',
        role: 'assistant',
        content: 'Once upon a',
        interrupted: true,
      },
      {
        id: 's1',
        role: 'run_summary',
        run_id: 'run-cancelled',
        status: 'cancelled',
        timing: { duration_ms: 3000 },
      },
    ]);

    const items = visibleTimelineItemsForRender(sessionState);
    const run = items.find((item) => item.type === 'assistant_run');
    expect(run).toBeTruthy();
    expect(run.status).toBe('cancelled');
    expect(run.durationMs).toBe(3000);
    // The preserved partial stays visible, flagged interrupted for the
    // component (which shows the Cancelled header label instead of the notice).
    expect(run.outputs.map((output) => output.content)).toEqual([
      'Once upon a',
    ]);
    expect(run.outputs[0].interrupted).toBe(true);
  });
});

describe('canonical Iteration count projection', () => {
  it('restores the server count from a persisted Run Summary', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-iterations-history',
    );
    loadHistory(sessionState, [
      { id: 'u1', role: 'user', content: 'Use five tools' },
      {
        id: 'a1',
        role: 'assistant',
        tool_calls: Array.from({ length: 5 }, (_, index) => ({
          id: `c${index}`,
          name: 'read',
          arguments: {},
        })),
      },
      ...Array.from({ length: 5 }, (_, index) => ({
        id: `t${index}`,
        role: 'tool',
        tool_call_id: `c${index}`,
        name: 'read',
        content: '{}',
      })),
      { id: 'a2', role: 'assistant', content: 'Done' },
      {
        id: 's1',
        role: 'run_summary',
        run_id: 'run-two-iterations',
        status: 'completed',
        iteration_count: 2,
      },
    ]);

    const run = visibleTimelineItemsForRender(sessionState).find(
      (item) => item.type === 'assistant_run',
    );
    expect(run.iterationCount).toBe(2);
    expect(run.tools).toHaveLength(5);
  });

  it('tracks the server count through live response and terminal events', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-iterations-live',
    );
    startRun(sessionState, {
      run_id: 'run-live',
      sse_url: '/runs/run-live',
      iteration_count: 0,
    });
    appendRunEvent(sessionState, {
      type: 'run_started',
      run_id: 'run-live',
      sequence: 1,
      payload: { status: 'running' },
    });
    appendRunEvent(sessionState, {
      type: 'model_step_usage',
      run_id: 'run-live',
      sequence: 2,
      payload: { iteration_count: 1 },
    });
    appendRunEvent(sessionState, {
      type: 'model_step_usage',
      run_id: 'run-live',
      sequence: 3,
      payload: { iteration_count: 2 },
    });
    appendRunEvent(sessionState, {
      type: 'run_completed',
      run_id: 'run-live',
      sequence: 4,
      payload: { status: 'completed', iteration_count: 2 },
    });

    const run = visibleTimelineItemsForRender(sessionState).find(
      (item) => item.type === 'assistant_run',
    );
    expect(run.iterationCount).toBe(2);
  });

  it('applies streamed change stats to the still-running assistant run', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-change-stats-live',
    );
    startRun(sessionState, {
      run_id: 'run-change-stats',
      sse_url: '/runs/run-change-stats',
      iteration_count: 0,
    });
    appendRunEvent(sessionState, {
      type: 'run_started',
      run_id: 'run-change-stats',
      sequence: 1,
      payload: { status: 'running' },
    });
    appendRunEvent(sessionState, {
      type: 'run_change_stats',
      run_id: 'run-change-stats',
      sequence: 2,
      payload: {
        change_stats: { files: 1, added: 2, removed: 1, paths: ['a.txt'] },
      },
    });

    let run = visibleTimelineItemsForRender(sessionState).find(
      (item) => item.type === 'assistant_run',
    );
    expect(run.status).toBe('running');
    expect(run.changeStats).toEqual({
      files: 1,
      added: 2,
      removed: 1,
      paths: ['a.txt'],
    });

    // An all-zero update (edits reverted to their baseline) retires the
    // earlier total instead of leaving it stale.
    appendRunEvent(sessionState, {
      type: 'run_change_stats',
      run_id: 'run-change-stats',
      sequence: 3,
      payload: { change_stats: { files: 0, added: 0, removed: 0, paths: [] } },
    });
    run = visibleTimelineItemsForRender(sessionState).find(
      (item) => item.type === 'assistant_run',
    );
    expect(run.changeStats).toEqual({
      files: 0,
      added: 0,
      removed: 0,
      paths: [],
    });
  });

  it('does not invent a count for an older summary without the field', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-iterations-unknown',
    );
    loadHistory(sessionState, [
      { id: 'u1', role: 'user', content: 'Old run' },
      { id: 'a1', role: 'assistant', content: 'Done' },
      {
        id: 's1',
        role: 'run_summary',
        run_id: 'run-unknown',
        status: 'completed',
      },
    ]);

    const run = visibleTimelineItemsForRender(sessionState).find(
      (item) => item.type === 'assistant_run',
    );
    expect(run.iterationCount).toBeNull();
  });
});

describe('per-tool-call user cancel projection', () => {
  const cancelledEnvelope = {
    ok: false,
    error: {
      code: 'cancelled_by_user',
      message: 'Command aborted by the user',
    },
    data: null,
    artifacts: [],
  };

  function liveSessionWithToolResult(result) {
    const chatState = createChatState();
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-1');
    startRun(sessionState, {
      run_id: 'run-1',
      sse_url: '/api/runs/run-1/events',
      status: 'running',
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-1',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-bash',
          index: 0,
          name: 'bash',
          arguments: { command: 'sleep 600' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-1',
      sequence: 2,
      payload: {
        tool_call: { id: 'call-bash', index: 0, name: 'bash' },
        result,
      },
    });
    return sessionState;
  }

  it('renders a user-cancelled tool result as cancelled, not failed (live)', () => {
    const sessionState = liveSessionWithToolResult(cancelledEnvelope);

    const items = visibleTimelineItemsForRender(sessionState);
    const run = items.find((item) => item.type === 'assistant_run');
    expect(run.tools).toHaveLength(1);
    expect(run.tools[0].status).toBe('cancelled');
  });

  it('keeps any other failure envelope failed (live)', () => {
    const sessionState = liveSessionWithToolResult({
      ok: false,
      error: { code: 'process_timeout', message: 'timed out' },
      data: null,
      artifacts: [],
    });

    const items = visibleTimelineItemsForRender(sessionState);
    const run = items.find((item) => item.type === 'assistant_run');
    expect(run.tools[0].status).toBe('failed');
  });

  it('keeps a mixed multi-edit result as partial instead of failed (live)', () => {
    const sessionState = liveSessionWithToolResult({
      ok: true,
      error: null,
      data: { status: 'partial', total: 3, succeeded: 2, failed: 1 },
      artifacts: [],
    });

    const items = visibleTimelineItemsForRender(sessionState);
    const run = items.find((item) => item.type === 'assistant_run');
    expect(run.tools[0].status).toBe('partial');
  });

  it('renders a user-cancelled tool result as cancelled after reload, without failing the run (history)', () => {
    const chatState = createChatState();
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-1');
    loadHistory(sessionState, [
      { id: 'user-1', role: 'user', content: 'Run it' },
      {
        id: 'assistant-tool',
        role: 'assistant',
        content: null,
        tool_calls: [
          {
            id: 'call-bash',
            name: 'bash',
            arguments: { command: 'sleep 600' },
          },
        ],
      },
      {
        id: 'tool-bash',
        role: 'tool',
        tool_call_id: 'call-bash',
        name: 'bash',
        content: JSON.stringify(cancelledEnvelope),
        timing: { duration_ms: 1200 },
      },
    ]);

    const items = visibleTimelineItemsForRender(sessionState);
    const run = items.find((item) => item.type === 'assistant_run');
    expect(run.tools).toHaveLength(1);
    expect(run.tools[0].status).toBe('cancelled');
    // The user's per-tool cancel is not a run failure.
    expect(run.status).not.toBe('failed');
  });
});

describe('cancelled history projection', () => {
  it('settles only pending Tool rows when a cancelled Run reloads from history', () => {
    const chatState = createChatState();
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-1');
    loadHistory(sessionState, [
      { id: 'user-1', role: 'user', content: 'Run both' },
      {
        id: 'assistant-tools',
        role: 'assistant',
        content: null,
        tool_calls: [
          {
            id: 'call-read',
            name: 'read',
            arguments: { path: 'README.md' },
          },
          {
            id: 'call-subagent',
            name: 'subagent',
            arguments: {
              agent_id: 'researcher',
              background: false,
              content: 'Research the API',
            },
          },
        ],
      },
      {
        id: 'tool-read',
        role: 'tool',
        tool_call_id: 'call-read',
        name: 'read',
        content: JSON.stringify({
          ok: true,
          error: null,
          data: { content: 'done' },
          artifacts: [],
        }),
      },
      {
        id: 'summary-1',
        role: 'run_summary',
        run_id: 'run-1',
        status: 'cancelled',
        timestamp: '2026-07-27T09:14:23Z',
      },
    ]);

    const items = visibleTimelineItemsForRender(sessionState);
    const run = items.find((item) => item.type === 'assistant_run');
    const toolsByName = Object.fromEntries(
      run.tools.map((tool) => [tool.name, tool]),
    );

    expect(run.status).toBe('cancelled');
    expect(toolsByName.read.status).toBe('success');
    expect(toolsByName.subagent.status).toBe('cancelled');
  });
});
