import { describe, expect, it } from 'vitest';
import {
  appendRunEvent,
  createChatState,
  ensureSessionState,
  loadHistory,
  startRun,
} from '../chatState.js';
import {
  pruneRunEventsPersistedInHistory,
  visibleTimelineItemsForRender,
} from '../chatTimeline.js';

// The Session state is built through the Chat state owner, the only producer
// of the History and Run event shapes the timeline projects.
function session(sessionId = 'session') {
  return ensureSessionState(createChatState(), 'alpha', sessionId);
}

function runEvent(runId, sequence, type, payload = {}, extra = {}) {
  return { type, run_id: runId, sequence, payload, ...extra };
}

function append(state, ...args) {
  appendRunEvent(state, runEvent(...args));
}

function start(state, runId) {
  startRun(state, {
    run_id: runId,
    sse_url: `/api/runs/${runId}/events`,
    status: 'running',
  });
}

const render = visibleTimelineItemsForRender;

it('attaches early speech to its running Tool and keeps that row for the final artifact', () => {
  const state = session();
  start(state, 'run-speech');
  const toolCall = {
    id: 'speech-call',
    name: 'generate_speech',
    arguments: { text: 'test-owned text' },
  };
  append(state, 'run-speech', 1, 'tool_call_started', { tool_call: toolCall });
  append(state, 'run-speech', 2, 'speech_playback', {
    tool_call_id: toolCall.id,
    url: '/api/speech/playback/early',
  });
  const early = assistantRun(state).tools[0];
  expect(early.speechPlayback).toBe('/api/speech/playback/early');
  expect(early.status).toBe('running');
  append(state, 'run-speech', 3, 'tool_call_result', {
    tool_call: toolCall,
    result: {
      ok: true,
      data: {
        artifact: { kind: 'speech', url: '/api/speech/artifacts/final' },
      },
    },
  });
  const final = assistantRun(state).tools[0];
  expect(final.id).toBe(early.id);
  expect(final.speechPlayback).toBe(early.speechPlayback);
  expect(final.status).toBe('success');
});

function assistantRun(state, runId) {
  return render(state).find(
    (item) =>
      item.type === 'assistant_run' &&
      (runId === undefined || item.runId === runId),
  );
}

function separators(items) {
  return items.filter((item) => item.type === 'compaction_separator');
}

function finishedRunEvents(runId, messageId) {
  return [
    runEvent(runId, 1, 'user_message_persisted', {
      message: { id: `user-${runId}`, role: 'user', content: 'Hi' },
    }),
    runEvent(runId, 2, 'run_started', { status: 'running' }),
    runEvent(runId, 3, 'assistant_output', {
      message: { id: messageId, role: 'assistant', content: 'Done.' },
    }),
    runEvent(runId, 4, 'run_completed', { status: 'completed' }),
  ];
}

function compactionRunEvents(runId, { checkpointId, durationMs } = {}) {
  return [
    runEvent(runId, 1, 'run_started', { status: 'running' }),
    runEvent(runId, 2, 'compaction_started', {
      context_tokens_before: 120_000,
    }),
    runEvent(
      runId,
      3,
      'compaction_completed',
      {
        context_tokens_before: 120_000,
        context_tokens_after: 30_000,
        ...(durationMs === undefined ? {} : { duration_ms: durationMs }),
        message: {
          id: checkpointId,
          history_sequence: 3,
          role: 'compaction_checkpoint',
          timestamp: '2026-08-27T14:00:55Z',
        },
      },
      { timestamp: '2026-08-27T14:00:55Z' },
    ),
    runEvent(runId, 4, 'run_completed', {
      status: 'completed',
      history_persisted: true,
    }),
  ];
}

describe('History and live Run projection', () => {
  it.each(['live', 'mixed', 'history'])(
    'keeps steered User corrections between responses in %s mode',
    (mode) => {
      const message = (id, role, content) => ({
        id,
        role,
        content,
        history_run_id: 'r',
      });
      const user = message('u1', 'user', 'Original');
      const before = message('a1', 'assistant', 'Before');
      const steer = message('u2', 'user', 'Correction');
      const after = message('a2', 'assistant', 'After');
      const events = [
        ['run_started', { status: 'running' }],
        ['user_message_persisted', { message: user }],
        ['assistant_output', { message: before }],
        ['user_message_persisted', { message: steer, queue_item_id: 'q' }],
        ['assistant_output', { message: after }],
      ].map(([type, payload], index) =>
        runEvent('r', index + 1, type, payload),
      );
      const state = {
        messages: mode === 'live' ? [] : [user, before, steer, after],
        runEvents: mode === 'history' ? [] : events,
        streamingRunEvents: [],
        currentRun:
          mode === 'history' ? null : { runId: 'r', status: 'running' },
        historyRuns: {},
      };

      const all = render(state).flatMap((item) =>
        item.type === 'assistant_run' ? item.items : [item],
      );

      expect(
        all.map(
          (item) =>
            item.message?.content ??
            item.event?.payload?.message?.content ??
            item.content,
        ),
      ).toEqual(['Original', 'Before', 'Correction', 'After']);
      expect(all.filter((item) => item.type === 'user_message')).toHaveLength(
        1,
      );
    },
  );

  describe('Run failure fallback', () => {
    const runId = 'test-failed-run';
    const error = 'test-terminal-failure';
    const failure = runEvent(runId, 2, 'run_failed', {
      status: 'failed',
      error,
    });
    const message = { id: 'test-error', role: 'error', content: error };
    const errors = (state) =>
      render(state).filter((item) => item.message?.role === 'error');

    it('replaces the fallback when the canonical error event arrives', () => {
      const state = { messages: [], runEvents: [failure] };
      expect(errors(state).map((item) => item.message.content)).toEqual([
        error,
      ]);

      state.runEvents.push(
        runEvent(runId, 1, 'error_message_persisted', { message }),
      );
      expect(errors(state).map((item) => item.message)).toEqual([message]);
    });

    it.each([false, true])(
      'uses loaded canonical History once (User anchor: %s)',
      (withUser) => {
        const user = { id: 'test-user', role: 'user', content: 'test-request' };
        const summary = {
          id: 'test-summary',
          role: 'run_summary',
          run_id: runId,
          status: 'failed',
        };
        const state = {
          status: 'failed',
          currentRun: { runId, status: 'failed' },
          messages: withUser ? [user, summary, message] : [summary, message],
          runEvents: [
            failure,
            ...(withUser
              ? [
                  runEvent(runId, 0, 'user_message_persisted', {
                    message: user,
                  }),
                ]
              : []),
          ],
        };
        expect(errors(state).map((item) => item.message)).toEqual([message]);
      },
    );

    it('keeps an earlier identical error from hiding a new failure', () => {
      const state = { messages: [message], runEvents: [failure] };
      expect(errors(state).map((item) => item.message.content)).toEqual([
        error,
        error,
      ]);
    });
  });

  it.each([
    ['a live interrupted output', true, 'live'],
    ['a normal live output', false, 'live'],
    ['an interrupted output loaded from History', true, 'history'],
  ])('flags %s as interrupted: %s', (_label, interrupted, source) => {
    const state = session();
    if (source === 'history') {
      loadHistory(state, [
        { id: 'u1', role: 'user', content: 'Long question' },
        {
          id: 'a1',
          role: 'assistant',
          content: 'Half',
          interrupted: true,
          run_id: 'run-int',
        },
      ]);
    } else {
      append(state, 'run-int', 1, 'run_started', { status: 'running' });
      append(state, 'run-int', 2, 'assistant_output', {
        message: {
          id: 'a-int',
          role: 'assistant',
          content: 'Half',
          ...(interrupted ? { interrupted: true } : {}),
        },
      });
      if (interrupted) {
        append(state, 'run-int', 3, 'run_interrupted', {
          status: 'interrupted',
          cause: 'network',
        });
      }
    }

    const output = assistantRun(state).items.find(
      (child) => child.type === 'assistant_output',
    );
    expect(output.content).toBe('Half');
    expect(output.interrupted).toBe(interrupted);
  });

  it('drops an unfinished Tool preview and settles a dispatched call when the Run is interrupted', () => {
    const state = session();
    start(state, 'run-tool-preview');
    append(state, 'run-tool-preview', 1, 'run_started', { status: 'running' });
    append(state, 'run-tool-preview', 2, 'tool_call_started', {
      tool_call: {
        id: 'call-bash',
        name: 'bash',
        arguments: { command: 'ls' },
      },
    });
    append(state, 'run-tool-preview', 3, 'tool_call_delta', {
      tool_call_id: 'call-partial',
      name_delta: 'subagent',
      arguments_delta: '{"action":"run","agent_id":"work',
    });
    append(state, 'run-tool-preview', 4, 'run_interrupted', {
      status: 'interrupted',
      cause: 'network',
    });

    const run = assistantRun(state);
    expect(run.status).toBe('interrupted');
    expect(run.tools.map((tool) => [tool.name, tool.status])).toEqual([
      ['bash', 'interrupted'],
    ]);
  });

  it('projects an Agent takeover as a divider that closes the previous Run', () => {
    const state = session();
    loadHistory(state, [
      { id: 'u1', role: 'user', content: 'Do the thing' },
      { id: 'a1', role: 'assistant', content: 'before', run_id: 'run-1' },
      {
        id: 'takeover-1',
        role: 'agent_takeover',
        content: JSON.stringify({ from: 'assistant', to: 'builder@vbot' }),
        timestamp: '2026-06-22T10:00:00+00:00',
      },
      { id: 'u2', role: 'user', content: 'Continue' },
      { id: 'a2', role: 'assistant', content: 'after', run_id: 'run-2' },
    ]);

    const items = render(state);
    const index = items.findIndex((item) => item.type === 'takeover_separator');
    const separator = items[index];
    expect(separator).toMatchObject({
      id: 'takeover-takeover-1',
      timestamp: '2026-06-22T10:00:00+00:00',
    });
    // The presentation layer parses from/to from the message it carries.
    expect(separator.message.content).toContain('builder@vbot');
    // The takeover is never folded into a Run: the turn before ends at it and
    // the next User turn starts after it.
    expect(items[index - 1].type).toBe('assistant_run');
    expect(items[index - 1].outputs.map((output) => output.content)).toEqual([
      'before',
    ]);
    expect(items[index + 1].type).toBe('message');
    expect(items[index + 1].message.role).toBe('user');
  });

  it('keeps repeated Provider Tool ids separate by their persisted Assistant identity', () => {
    const state = session();
    loadHistory(
      state,
      ['first', 'second'].flatMap((id, index) => [
        {
          id,
          role: 'assistant',
          history_run_id: 'run',
          history_sequence: index * 2,
          tool_calls: [{ id: 'reused', name: 'read', arguments: { path: id } }],
        },
        {
          id: `${id}-result`,
          role: 'tool',
          history_run_id: 'run',
          history_sequence: index * 2 + 1,
          tool_call_id: 'reused',
          name: 'read',
          content: id,
        },
      ]),
    );
    startRun(state, { run_id: 'run' });
    append(state, 'run', 1, 'tool_call_started', {
      assistant_message_id: 'second',
      tool_call: { id: 'reused', name: 'read', arguments: { path: 'second' } },
    });
    append(state, 'run', 2, 'tool_call_result', {
      assistant_message_id: 'second',
      tool_call: { id: 'reused', name: 'read' },
      result: 'second',
    });

    const tools = render(state).flatMap((item) => item.tools ?? []);
    expect(tools.map((tool) => tool.result)).toEqual(['first', 'second']);
    expect(new Set(tools.map((tool) => tool.id)).size).toBe(2);
  });

  it('keeps a Tool row id from its first live event through History rebuild', () => {
    const state = session();
    const result = {
      ok: true,
      data: { artifact: { kind: 'speech', url: '/a' } },
    };
    const toolCall = { id: 'call', name: 'generate_speech', arguments: {} };
    const toolIds = () => assistantRun(state).tools.map((tool) => tool.id);
    startRun(state, { run_id: 'run', status: 'running' });
    append(state, 'run', 1, 'user_message_persisted', {
      message: { id: 'user', role: 'user', content: 'Speak' },
    });
    // The streamed Tool preview creates the row before the stable events.
    append(state, 'run', 2, 'tool_call_delta', {
      tool_call_id: 'call',
      name_delta: 'generate_speech',
    });
    append(state, 'run', 3, 'assistant_output', {
      message: { id: 'call-step', role: 'assistant', tool_calls: [toolCall] },
    });
    append(state, 'run', 4, 'tool_call_started', {
      assistant_message_id: 'call-step',
      tool_call: toolCall,
    });
    append(state, 'run', 5, 'tool_call_result', {
      assistant_message_id: 'call-step',
      tool_call: toolCall,
      result,
    });
    const liveRun = assistantRun(state);
    const liveToolIds = toolIds();

    append(state, 'run', 6, 'assistant_output', {
      message: { id: 'answer', role: 'assistant', content: 'Spoken.' },
    });
    append(state, 'run', 7, 'run_completed', { status: 'completed' });
    expect(state.streamingRunEvents).toEqual([]);
    expect(toolIds()).toEqual(liveToolIds);
    const completedRun = assistantRun(state);

    loadHistory(
      state,
      [
        { id: 'user', role: 'user', content: 'Speak' },
        { id: 'call-step', role: 'assistant', tool_calls: [toolCall] },
        {
          id: 'tool',
          role: 'tool',
          tool_call_id: 'call',
          name: 'generate_speech',
          content: JSON.stringify(result),
        },
        { id: 'answer', role: 'assistant', content: 'Spoken.' },
      ].map((message, index) => ({
        ...message,
        history_run_id: 'run',
        history_sequence: index + 1,
      })),
      { runs: [{ run_id: 'run', complete: true }] },
    );
    const historyRun = assistantRun(state);

    expect(state.runEvents).toEqual([]);
    expect(historyRun.source).toBe('history');
    expect(historyRun.tools[0].result).toBe(JSON.stringify(result));
    expect(historyRun.id).toBe(liveRun.id);
    expect(toolIds()).toEqual(liveToolIds);
    expect(historyRun.items.map((child) => child.id)).toEqual(
      completedRun.items.map((child) => child.id),
    );
  });

  it('keeps the latest Provider heartbeat on the active Run until output arrives', () => {
    const state = session();
    append(state, 'run-heartbeat', 1, 'run_started', { status: 'running' });
    append(
      state,
      'run-heartbeat',
      2,
      'provider_heartbeat',
      { idle_seconds: 75.4, state: 'waiting_for_model_delta' },
      { timestamp: '2026-07-27T10:00:15Z' },
    );

    const waiting = assistantRun(state, 'run-heartbeat');
    expect(waiting.providerHeartbeat).toEqual({
      idleSeconds: 75.4,
      timestamp: '2026-07-27T10:00:15Z',
    });
    expect(waiting.items).toEqual([]);

    append(state, 'run-heartbeat', 3, 'assistant_output_delta', {
      content_delta: 'The buffered call is ready.',
    });
    expect(assistantRun(state, 'run-heartbeat').providerHeartbeat).toBeNull();
  });

  it('reuses a finished Run projection across renders and rebuilds it on a late event', () => {
    const state = session();
    for (const event of finishedRunEvents(
      'run-finished',
      'assistant-finished',
    )) {
      appendRunEvent(state, event);
    }
    start(state, 'run-active');
    append(state, 'run-active', 1, 'run_started', { status: 'running' });
    append(state, 'run-active', 2, 'assistant_output_delta', {
      content_delta: 'Streaming…',
    });

    const first = assistantRun(state, 'run-finished');
    const second = assistantRun(state, 'run-finished');
    expect(second.items).toBe(first.items);
    expect(second.events).toBe(first.events);
    expect(assistantRun(state, 'run-active').items).toBe(
      assistantRun(state, 'run-active').items,
    );

    append(state, 'run-finished', 5, 'tool_call_result', {
      tool_call: { id: 'call-late', index: 0, name: 'read' },
      result: { ok: true },
    });
    const rebuilt = assistantRun(state, 'run-finished');
    expect(rebuilt.items).not.toBe(first.items);
    expect(rebuilt.tools.map((tool) => tool.toolCallId)).toEqual(['call-late']);
  });

  it.each(['live', 'mixed'])(
    'keeps unchanged %s children immutable through streaming, replay and replacement',
    (mode) => {
      const state = session();
      start(state, 'run-active');
      const message = {
        id: 'first',
        role: 'assistant',
        content: 'First',
        history_run_id: 'run-active',
      };
      append(state, 'run-active', 1, 'assistant_output', { message });
      append(state, 'run-active', 3, 'tool_call_started', {
        tool_call: { id: 'read', name: 'read', arguments: { path: 'notes' } },
      });
      append(state, 'run-active', 4, 'tool_call_result', {
        tool_call: { id: 'read', name: 'read' },
        result: { ok: true },
      });
      if (mode === 'mixed') loadHistory(state, [message]);
      append(state, 'run-active', 5, 'assistant_output_delta', {
        content_delta: 'A',
      });
      const first = assistantRun(state);
      append(state, 'run-active', 6, 'assistant_output_delta', {
        content_delta: 'B',
      });
      const next = assistantRun(state);
      expect(next.items.slice(0, 2)).toEqual(first.items.slice(0, 2));
      expect(next.items[0]).toBe(first.items[0]);
      expect(next.tools[0]).toBe(first.tools[0]);
      expect(next.outputs.at(-1).content).toBe('AB');
      expect(first.outputs.at(-1).content).toBe('A');
      expect(first.outputs.at(-1).events[0].payload.content_delta).toBe('A');

      // An earlier event and a same-length replacement must behave exactly like
      // projecting that replay in a fresh view, even after a prefix was cached.
      append(state, 'run-active', 2, 'model_fallback_activated', {
        to_model: 'backup',
      });
      expect(render(state)).toEqual(render({ ...state }));
      state.runEvents = state.runEvents.map((event) =>
        event.sequence === 4
          ? {
              ...event,
              payload: {
                ...event.payload,
                result: { ok: false, error: 'Changed result' },
              },
            }
          : event,
      );
      expect(assistantRun(state).tools[0].status).toBe('failed');
      expect(render(state)).toEqual(render({ ...state }));
      expect(first.tools[0].status).toBe('success');
    },
  );

  it('hands back unchanged History rows as the same objects while a Run streams', () => {
    const state = session();
    for (const event of finishedRunEvents('run-1', 'assistant-1')) {
      appendRunEvent(state, event);
    }
    const persisted = (message, index) => ({
      ...message,
      history_run_id: 'run-1',
      history_sequence: index + 1,
    });
    loadHistory(
      state,
      [
        { id: 'user-run-1', role: 'user', content: 'Hi' },
        { id: 'assistant-1', role: 'assistant', content: 'Done.' },
      ].map(persisted),
      { runs: [{ run_id: 'run-1', complete: true }] },
    );
    start(state, 'run-2');
    append(state, 'run-2', 1, 'run_started', { status: 'running' });
    append(state, 'run-2', 2, 'assistant_output_delta', {
      content_delta: 'Stream',
    });
    const [user, finished, streaming] = render(state);
    expect(finished.source).toBe('history');

    append(state, 'run-2', 3, 'assistant_output_delta', {
      content_delta: 'ing',
    });
    const flushed = render(state);
    expect(flushed[0]).toBe(user);
    expect(flushed[1]).toBe(finished);
    expect(flushed[2]).not.toBe(streaming);

    // A Run whose Messages change is rebuilt; its neighbours stay untouched.
    loadHistory(
      state,
      [
        persisted(
          {
            id: 'summary-1',
            role: 'run_summary',
            run_id: 'run-1',
            status: 'completed',
            iteration_count: 3,
          },
          2,
        ),
      ],
      { incremental: true, runs: [{ run_id: 'run-1', complete: true }] },
    );
    const reloaded = render(state);
    expect(reloaded[0]).toBe(user);
    expect(reloaded[1]).not.toBe(finished);
    expect(reloaded[1]).toMatchObject({ id: finished.id, iterationCount: 3 });
  });

  it('inserts a retained live Run into History by its timestamp', () => {
    const state = session();
    loadHistory(state, [
      {
        id: 'user-1',
        role: 'user',
        content: 'First',
        timestamp: '2026-08-27T10:00:00Z',
      },
      {
        id: 'assistant-1',
        role: 'assistant',
        content: 'First answer',
        timestamp: '2026-08-27T10:00:05Z',
      },
      {
        id: 'user-3',
        role: 'user',
        content: 'Later',
        timestamp: '2026-08-27T10:10:00Z',
      },
    ]);
    start(state, 'run-live');
    append(
      state,
      'run-live',
      1,
      'user_message_persisted',
      { message: { id: 'user-2', role: 'user', content: 'Middle' } },
      { timestamp: '2026-08-27T10:05:00Z' },
    );
    append(state, 'run-live', 2, 'assistant_output', {
      message: { id: 'assistant-2', role: 'assistant', content: 'Working.' },
    });

    // The live User message stays directly before its Assistant output.
    expect(
      render(state).map((item) =>
        item.type === 'assistant_run'
          ? `run:${item.runId ?? 'history'}`
          : (item.message ?? item.event.payload.message).id,
      ),
    ).toEqual(['user-1', 'run:history', 'user-2', 'run:run-live', 'user-3']);
  });
});

describe('Run status projection', () => {
  it.each(['cancelled', 'interrupted'])(
    'renders a bare %s Run row from an anchorless Run Summary',
    (status) => {
      const state = session();
      loadHistory(state, [
        { id: 'u1', role: 'user', content: 'tell me a story' },
        {
          id: 's1',
          role: 'run_summary',
          run_id: 'run-ended',
          status,
          timing: { duration_ms: 12000 },
        },
      ]);

      // The ended turn is not a hole: its row renders after the User message
      // although no Assistant or Tool message anchors it.
      const items = render(state);
      expect(items.map((item) => item.type)).toEqual([
        'message',
        'assistant_run',
      ]);
      expect(items[1]).toMatchObject({
        status,
        runId: 'run-ended',
        durationMs: 12000,
        items: [],
      });
    },
  );

  it('does not render bare rows for anchorless completed summaries of a page slice', () => {
    const state = session();
    loadHistory(state, [
      {
        id: 's1',
        role: 'run_summary',
        run_id: 'run-old',
        status: 'completed',
        timing: { duration_ms: 5 },
      },
      { id: 'u1', role: 'user', content: 'next turn' },
    ]);

    expect(render(state).map((item) => item.type)).toEqual(['message']);
  });

  it('marks the Run cancelled when a preserved interrupted partial precedes a cancelled summary', () => {
    const state = session();
    loadHistory(state, [
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

    const run = assistantRun(state);
    expect(run.status).toBe('cancelled');
    expect(run.durationMs).toBe(3000);
    // The partial stays visible and flagged; the component shows the
    // Cancelled header instead of the interruption notice.
    expect(run.outputs.map((output) => output.content)).toEqual([
      'Once upon a',
    ]);
    expect(run.outputs[0].interrupted).toBe(true);
  });

  it('restores the canonical Iteration count from a persisted Run Summary', () => {
    const state = session();
    loadHistory(state, [
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

    const run = assistantRun(state);
    expect(run.iterationCount).toBe(2);
    expect(run.tools).toHaveLength(5);
  });

  it('does not invent an Iteration count for a summary without the field', () => {
    const state = session();
    loadHistory(state, [
      { id: 'u1', role: 'user', content: 'Old run' },
      { id: 'a1', role: 'assistant', content: 'Done' },
      {
        id: 's1',
        role: 'run_summary',
        run_id: 'run-unknown',
        status: 'completed',
      },
    ]);

    expect(assistantRun(state).iterationCount).toBeNull();
  });

  it('tracks the Iteration count and change stats through live Run events', () => {
    const state = session();
    startRun(state, {
      run_id: 'run-live',
      sse_url: '/runs/run-live',
      iteration_count: 0,
    });
    append(state, 'run-live', 1, 'run_started', { status: 'running' });
    append(state, 'run-live', 2, 'model_step_usage', { iteration_count: 1 });
    const changeStats = { files: 1, added: 2, removed: 1, paths: ['a.txt'] };
    append(state, 'run-live', 3, 'run_change_stats', {
      change_stats: changeStats,
    });

    let run = assistantRun(state);
    expect(run.status).toBe('running');
    expect(run.iterationCount).toBe(1);
    expect(run.changeStats).toEqual(changeStats);

    // An all-zero update (edits reverted to their baseline) retires the
    // earlier total instead of leaving it stale.
    const reverted = { files: 0, added: 0, removed: 0, paths: [] };
    append(state, 'run-live', 4, 'run_change_stats', {
      change_stats: reverted,
    });
    append(state, 'run-live', 5, 'model_step_usage', { iteration_count: 2 });
    append(state, 'run-live', 6, 'run_completed', {
      status: 'completed',
      iteration_count: 2,
    });
    run = assistantRun(state);
    expect(run.changeStats).toEqual(reverted);
    expect(run.iterationCount).toBe(2);
  });

  const STOPPED_COMMAND = {
    ok: true,
    error: null,
    data: {
      status: 'stopped',
      exit_code: 1,
      stopped_because: 'the user stopped it.',
      output: 'started',
    },
  };

  it.each([
    [
      'cancelled',
      { ok: false, error: { code: 'cancelled_by_user', message: 'aborted' } },
    ],
    ['cancelled', STOPPED_COMMAND],
    // A terminal kill returns the stopped command's result: the kill succeeded.
    ['success', STOPPED_COMMAND, 'terminal'],
    [
      'failed',
      { ok: false, error: { code: 'process_timeout', message: 'timed out' } },
    ],
    [
      'partial',
      {
        ok: true,
        error: null,
        data: { status: 'partial', total: 3, succeeded: 2, failed: 1 },
      },
    ],
  ])(
    'settles a live Tool row as %s from its result envelope',
    (status, result, name = 'bash') => {
      const state = session();
      start(state, 'run-1');
      const toolCall = { id: 'call-tool', index: 0, name };
      append(state, 'run-1', 1, 'tool_call_started', {
        tool_call: { ...toolCall, arguments: { command: 'sleep 600' } },
      });
      append(state, 'run-1', 2, 'tool_call_result', {
        tool_call: toolCall,
        result: { data: null, artifacts: [], ...result },
      });

      const run = assistantRun(state);
      expect(run.tools).toHaveLength(1);
      expect(run.tools[0].status).toBe(status);
    },
  );

  it.each([
    [
      'a cancelled_by_user failure',
      {
        ok: false,
        error: { code: 'cancelled_by_user', message: 'aborted' },
        data: null,
        artifacts: [],
      },
    ],
    ['a stopped command', { ...STOPPED_COMMAND, artifacts: [] }],
  ])(
    'keeps a reloaded user-cancelled Tool result (%s) cancelled without failing the Run',
    (_case, envelope) => {
      const state = session();
      loadHistory(state, [
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
          content: JSON.stringify(envelope),
          timing: { duration_ms: 1200 },
        },
      ]);

      const run = assistantRun(state);
      expect(run.tools.map((tool) => tool.status)).toEqual(['cancelled']);
      expect(run.status).not.toBe('failed');
    },
  );

  it.each([
    ['cancelled', 'cancelled'],
    // A server restart ends the Run interrupted; its Tool calls never get a
    // Result and must not read as running (with a cancel control) forever.
    ['interrupted', 'interrupted'],
    ['failed', 'interrupted'],
  ])(
    'settles only pending Tool rows of a Run reloaded as %s from History',
    (runStatus, toolStatus) => {
      const state = session();
      loadHistory(state, [
        { id: 'user-1', role: 'user', content: 'Run both' },
        {
          id: 'assistant-tools',
          role: 'assistant',
          content: null,
          tool_calls: [
            { id: 'call-read', name: 'read', arguments: { path: 'README.md' } },
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
          status: runStatus,
          timestamp: '2026-07-27T09:14:23Z',
        },
      ]);

      const run = assistantRun(state);
      const statuses = Object.fromEntries(
        run.tools.map((tool) => [tool.name, tool.status]),
      );
      expect(run.status).toBe(runStatus);
      expect(statuses).toEqual({ read: 'success', subagent: toolStatus });
    },
  );
});

describe('Compaction projection', () => {
  it('keeps an in-Run checkpoint between the Tool steps around its events', () => {
    const state = session();
    loadHistory(state, [
      { id: 'old-user', role: 'user', content: 'Earlier turn' },
      { id: 'old-assistant', role: 'assistant', content: 'Earlier answer' },
    ]);
    start(state, 'run-compaction');
    append(state, 'run-compaction', 1, 'user_message_persisted', {
      message: { id: 'current-user', role: 'user', content: 'Keep working' },
    });
    append(state, 'run-compaction', 2, 'tool_call_started', {
      tool_call: { id: 'call-read', index: 0, name: 'read', arguments: {} },
    });
    append(state, 'run-compaction', 3, 'tool_call_result', {
      tool_call: { id: 'call-read', index: 0, name: 'read' },
      result: { ok: true },
    });
    append(state, 'run-compaction', 4, 'compaction_started', {
      context_tokens_before: 250_000,
    });
    append(state, 'run-compaction', 5, 'compaction_completed', {
      context_tokens_before: 250_000,
      context_tokens_after: 30_000,
      message: {
        id: 'checkpoint-1',
        role: 'compaction_checkpoint',
        timestamp: '2026-07-29T17:55:25Z',
      },
    });
    append(state, 'run-compaction', 6, 'tool_call_started', {
      tool_call: { id: 'call-edit', index: 0, name: 'edit', arguments: {} },
    });

    const items = render(state);
    const run = assistantRun(state, 'run-compaction');
    expect(run.items.map((child) => child.type)).toEqual([
      'tool_call',
      'compaction_separator',
      'tool_call',
    ]);
    expect(run.items[1]).toMatchObject({
      message: { id: 'checkpoint-1' },
      status: 'completed',
      contextTokensBefore: 250_000,
      contextTokensAfter: 30_000,
    });
    expect(run.items[1].events.map((event) => event.type)).toEqual([
      'compaction_started',
      'compaction_completed',
    ]);
    // The separator stays inside its Run block, never beside it.
    expect(separators(items)).toEqual([]);
    expect(
      state.messages.some(
        (message) => message.role === 'compaction_checkpoint',
      ),
    ).toBe(false);
  });

  it.each(['failed', 'stale_context', 'insufficient_reclaim'])(
    'settles a Compaction attempt aborted with reason %s',
    (reason) => {
      const state = session();
      append(state, 'run-progress', 1, 'compaction_started', {
        context_tokens_before: 250_000,
      });

      // A Run whose only child is the divider renders it bare.
      expect(separators(render(state))).toEqual([
        expect.objectContaining({
          status: 'running',
          contextTokensBefore: 250_000,
        }),
      ]);

      append(state, 'run-progress', 2, 'compaction_aborted', { reason });

      const items = render(state);
      expect(separators(items).map((item) => item.status)).toEqual(
        reason === 'failed' ? ['failed'] : [],
      );
      expect(
        items.find((item) => item.type === 'assistant_run')?.items ?? [],
      ).toEqual([]);
    },
  );

  it('renders a finished standalone Compaction Run as a bare separator', () => {
    const state = session();
    loadHistory(state, [
      { id: 'user-1', role: 'user', content: 'Earlier turn' },
    ]);
    for (const event of compactionRunEvents('run-compaction', {
      checkpointId: 'checkpoint-live',
      durationMs: 54_000,
    })) {
      appendRunEvent(state, event);
    }

    expect(assistantRun(state, 'run-compaction')).toBeUndefined();
    expect(separators(render(state))).toEqual([
      expect.objectContaining({
        status: 'completed',
        message: expect.objectContaining({ id: 'checkpoint-live' }),
        durationMs: 54_000,
      }),
    ]);
  });

  it('keeps the failed Run block when a standalone Compaction aborts', () => {
    const state = session();
    loadHistory(state, [
      { id: 'user-1', role: 'user', content: 'Earlier turn' },
    ]);
    append(state, 'run-failed', 1, 'run_started', { status: 'running' });
    append(state, 'run-failed', 2, 'compaction_started', {
      context_tokens_before: 120_000,
    });
    append(state, 'run-failed', 3, 'compaction_aborted', { reason: 'failed' });
    append(state, 'run-failed', 4, 'run_failed', {
      status: 'failed',
      error: 'Provider request failed',
    });

    expect(separators(render(state))).toEqual([]);
    expect(assistantRun(state, 'run-failed').status).toBe('failed');
  });

  it('reads a reloaded checkpoint duration from its usage', () => {
    const state = session();
    loadHistory(state, [
      { id: 'user-1', role: 'user', content: 'Earlier turn' },
      {
        id: 'checkpoint-1',
        history_sequence: 3,
        role: 'compaction_checkpoint',
        timestamp: '2026-08-27T14:00:55Z',
        usage: {
          context_tokens_before: 120_000,
          context_tokens_after: 30_000,
          compaction_duration_ms: 54_000,
        },
      },
    ]);

    expect(separators(render(state)).map((item) => item.durationMs)).toEqual([
      54_000,
    ]);
  });

  it('renders a persisted checkpoint once while a newer Run streams', () => {
    const state = session();
    loadHistory(
      state,
      [
        { id: 'user-1', role: 'user', content: 'Earlier turn' },
        { id: 'assistant-1', role: 'assistant', content: 'Earlier answer' },
        {
          id: 'summary-prev',
          role: 'run_summary',
          run_id: 'run-prev',
          status: 'completed',
          timing: { duration_ms: 87_000 },
          iteration_count: 1,
        },
        {
          id: 'checkpoint-1',
          history_sequence: 3,
          role: 'compaction_checkpoint',
          timestamp: '2026-08-27T14:00:55Z',
        },
        { id: 'user-2', role: 'user', content: 'Go ahead.' },
      ],
      {
        runs: [
          { run_id: 'run-compaction', status: 'completed', complete: true },
        ],
      },
    );
    for (const event of compactionRunEvents('run-compaction', {
      checkpointId: 'checkpoint-1',
    })) {
      appendRunEvent(state, event);
    }
    start(state, 'run-next');
    append(state, 'run-next', 1, 'user_message_persisted', {
      message: { id: 'user-2', role: 'user', content: 'Go ahead.' },
    });
    append(state, 'run-next', 2, 'assistant_output', {
      message: { id: 'assistant-2', role: 'assistant', content: 'Working.' },
    });

    expect(assistantRun(state, 'run-compaction')).toBeUndefined();
    expect(separators(render(state))).toHaveLength(1);
  });
});

describe('pruneRunEventsPersistedInHistory', () => {
  const activeStart = runEvent('run-active', 1, 'run_started', {
    status: 'running',
  });
  const emptyRun = [
    runEvent('run-empty', 1, 'run_started', { status: 'running' }),
    runEvent('run-empty', 2, 'run_completed', { status: 'completed' }),
  ];

  it.each([
    [
      'drops a finished Run with a complete persisted page',
      [...finishedRunEvents('run-finished', 'a'), activeStart],
      { 'run-finished': { complete: true, status: 'completed' } },
      ['run-active'],
    ],
    [
      'drops a finished Compaction Run once its checkpoint is persisted',
      compactionRunEvents('run-compaction', { checkpointId: 'checkpoint-1' }),
      { 'run-compaction': { complete: true, status: 'completed' } },
      [],
    ],
  ])('%s', (_label, events, runs, remainingRunIds) => {
    const pruned = pruneRunEventsPersistedInHistory(events, runs, 'run-active');
    expect(pruned.map((event) => event.run_id)).toEqual(
      events
        .map((event) => event.run_id)
        .filter((runId) => remainingRunIds.includes(runId)),
    );
  });

  it.each([
    [
      'a Run whose output the page does not fully hold',
      finishedRunEvents('run-finished', 'a'),
      { 'run-finished': { complete: false } },
    ],
    ['a Run without persisted output', emptyRun, {}],
    [
      'the active Run even when its output is persisted',
      finishedRunEvents('run-active', 'a'),
      { 'run-active': { complete: true } },
    ],
  ])('keeps %s', (_label, events, runs) => {
    expect(pruneRunEventsPersistedInHistory(events, runs, 'run-active')).toBe(
      events,
    );
  });
});
