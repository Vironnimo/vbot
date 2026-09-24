// Chat Timeline projection over a long Session.
//
// ChatTimeline and ChatView derive their rows from
// visibleTimelineItemsForRender(sessionState), so it re-runs whenever the
// Session state changes, including every streaming flush. The state is built
// with the real chatState functions (loadHistory, startRun, appendRunEvent).
//
// - `timeline.history[1000msg]`: an idle Session, History only.
// - `timeline.streaming_flush[1000msg]`: the same History while the next Run
//   streams its answer (run_started, user_message_persisted and 200 compressed
//   assistant_output_delta events). One operation is one flush's projection.
import { bench } from 'vitest';

import {
  appendRunEvent,
  createChatState,
  ensureSessionState,
  loadHistory,
  startRun,
  visibleTimelineItemsForRender,
} from '../chatState.js';
import {
  benchName,
  benchOptions,
  code,
  createRandom,
  markdownText,
  words,
} from './benchSupport.js';

const HISTORY_MESSAGES = 1000;
const HISTORY_CHARS = 800_000;
const STREAMED_DELTAS = 200;
const AGENT_ID = 'perf-agent';
const SESSION_ID = 'perf-session';
const ROLE_WEIGHTS = { user: 2, assistant_tools: 0.4, tool: 3, final: 3 };
const TOOL_NAMES = ['bash', 'read', 'search_files'];
const HISTORY_START_MS = Date.parse('2026-01-05T09:00:00Z');

function planRuns(random, messageBudget) {
  const jitter = (kind) => ROLE_WEIGHTS[kind] * (0.5 + random());
  const plans = [];
  let used = 0;
  while (used < messageBudget - 1) {
    const plan = [{ kind: 'user', weight: jitter('user') }];
    const steps = 2 + Math.floor(random() * 4);
    for (let step = 0; step < steps; step += 1) {
      const calls = 1 + Math.floor(random() * 2);
      plan.push({
        kind: 'assistant_tools',
        weight: jitter('assistant_tools'),
        calls,
      });
      for (let call = 0; call < calls; call += 1) {
        plan.push({ kind: 'tool', weight: jitter('tool') });
      }
    }
    plan.push({ kind: 'final', weight: jitter('final') });
    plan.push({ kind: 'summary', weight: 0 });
    if (used + plan.length > messageBudget - 1) break;
    plans.push(plan);
    used += plan.length;
  }
  return plans;
}

// Public History records as the chat history RPC returns them, plus the user
// message of the Run that is about to start.
function buildHistory(random) {
  const plans = planRuns(random, HISTORY_MESSAGES);
  const totalWeight = plans
    .flat()
    .reduce((sum, item) => sum + item.weight, ROLE_WEIGHTS.user);
  const charsPerWeight = HISTORY_CHARS / totalWeight;
  let clock = HISTORY_START_MS;
  const at = (seconds) => {
    clock += seconds * 1000;
    return new Date(clock).toISOString();
  };
  const messages = [];
  const runs = [];

  plans.forEach((plan, runIndex) => {
    const runId = `run-${String(runIndex).padStart(5, '0')}`;
    const startedAt = at(30);
    let calls = [];
    let iterations = 0;
    for (const item of plan) {
      const chars = Math.max(1, Math.round(item.weight * charsPerWeight));
      const base = { history_run_id: runId, id: `${runId}-${messages.length}` };
      if (item.kind === 'user') {
        messages.push({
          ...base,
          role: 'user',
          content: words(random, chars),
          timestamp: startedAt,
        });
      } else if (item.kind === 'assistant_tools') {
        calls = Array.from({ length: item.calls }, (_, index) => ({
          id: `call-${runIndex}-${iterations}-${index}`,
          name: TOOL_NAMES[Math.floor(random() * TOOL_NAMES.length)],
          arguments: { path: `src/${words(random, 12)}.py` },
        }));
        iterations += 1;
        messages.push({
          ...base,
          role: 'assistant',
          model: 'perf-bench/perf-model',
          content: words(random, chars),
          tool_calls: calls,
          timestamp: at(5),
        });
      } else if (item.kind === 'tool') {
        const call = calls.shift();
        const startedToolAt = at(0.1);
        const completedToolAt = at(0.4);
        messages.push({
          ...base,
          role: 'tool',
          tool_call_id: call.id,
          name: call.name,
          content: JSON.stringify({
            ok: true,
            error: null,
            data: { output: code(random, chars) },
            artifacts: [],
          }),
          timing: {
            started_at: startedToolAt,
            completed_at: completedToolAt,
            duration_ms: 400,
          },
          timestamp: completedToolAt,
        });
      } else if (item.kind === 'final') {
        iterations += 1;
        messages.push({
          ...base,
          role: 'assistant',
          model: 'perf-bench/perf-model',
          content: markdownText(random, chars),
          timestamp: at(8),
        });
      } else {
        const completedAt = at(0.2);
        messages.push({
          ...base,
          role: 'run_summary',
          run_id: runId,
          status: 'completed',
          iteration_count: iterations,
          timing: {
            started_at: startedAt,
            completed_at: completedAt,
            duration_ms: Date.parse(completedAt) - Date.parse(startedAt),
          },
          timestamp: completedAt,
        });
        runs.push({ run_id: runId, status: 'completed', complete: true });
      }
    }
  });

  const activeRunId = 'run-active';
  const activeUser = {
    history_run_id: activeRunId,
    id: `${activeRunId}-user`,
    role: 'user',
    content: words(random, Math.round(ROLE_WEIGHTS.user * charsPerWeight)),
    timestamp: at(30),
  };
  messages.push(activeUser);
  runs.push({ run_id: activeRunId, status: 'running', complete: false });
  return { messages, runs, activeRunId, activeUser };
}

function idleSession(history) {
  const sessionState = ensureSessionState(
    createChatState(),
    AGENT_ID,
    SESSION_ID,
  );
  loadHistory(sessionState, history.messages.slice(0, -1), {
    runs: history.runs.slice(0, -1),
  });
  return sessionState;
}

function streamingSession(history, random) {
  const sessionState = ensureSessionState(
    createChatState(),
    AGENT_ID,
    SESSION_ID,
  );
  loadHistory(sessionState, history.messages, { runs: history.runs });
  const runId = history.activeRunId;
  startRun(sessionState, { run_id: runId, status: 'running' });
  const event = (sequence, type, payload) => ({
    type,
    run_id: runId,
    agent_id: AGENT_ID,
    session_id: SESSION_ID,
    sequence,
    payload,
    timestamp: new Date(HISTORY_START_MS).toISOString(),
  });
  appendRunEvent(sessionState, event(1, 'run_started', { status: 'running' }));
  appendRunEvent(
    sessionState,
    event(2, 'user_message_persisted', { message: history.activeUser }),
  );
  for (let index = 0; index < STREAMED_DELTAS; index += 1) {
    appendRunEvent(
      sessionState,
      event(3 + index, 'assistant_output_delta', {
        content_delta: `${words(random, 40)} `,
      }),
    );
  }
  return sessionState;
}

function requireRows(sessionState, label) {
  const rows = visibleTimelineItemsForRender(sessionState);
  if (rows.length === 0) {
    throw new Error(`${label} fixture produced no Timeline rows`);
  }
  return rows;
}

const random = createRandom(1000);
const history = buildHistory(random);

const idle = idleSession(history);
const idleRows = requireRows(idle, 'history');
bench(
  benchName('timeline.history[1000msg]', {
    messages: history.messages.length - 1,
    rows: idleRows.length,
  }),
  () => {
    visibleTimelineItemsForRender(idle);
  },
  benchOptions(),
);

const streaming = streamingSession(history, random);
const streamingRows = requireRows(streaming, 'streaming');
if (streamingRows.at(-1)?.type !== 'assistant_run') {
  throw new Error('streaming fixture does not end with the live assistant Run');
}
bench(
  benchName('timeline.streaming_flush[1000msg]', {
    messages: history.messages.length,
    rows: streamingRows.length,
    deltas: STREAMED_DELTAS,
  }),
  () => {
    visibleTimelineItemsForRender(streaming);
  },
  benchOptions(),
);
