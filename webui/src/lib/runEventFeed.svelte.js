import { isTerminalRunEvent } from './api.js';
// The App's live feed of Run server events (`run_started`, `run_output` and
// the terminal lifecycle events) for its Chat owners. Each event takes the
// next position; an owner keeps the newest position it applied and reads what
// follows, so appending costs O(1) and no consumer re-reads events it already
// applied. Events stay plain objects: only the positions are reactive.

export const MAX_RUN_EVENTS = 500;

export function createRunEventFeed({ capacity = MAX_RUN_EVENTS } = {}) {
  // Position `p` lives in slot `p % capacity` until position `p + capacity`
  // replaces it.
  const slots = new Array(capacity);
  // Run id -> position of its retained terminal event.
  // eslint-disable-next-line svelte/prefer-svelte-reactivity -- bookkeeping, never rendered
  const terminalPositions = new Map();
  let end = $state(0);
  let runStarts = $state(0);
  let lifecycle = $state(0);
  let activeRuns = [];

  function forgetEvicted(position) {
    if (position < 1) {
      return;
    }
    const runId = slots[position % capacity]?.payload?.run_id;
    if (runId && terminalPositions.get(runId) === position) {
      terminalPositions.delete(runId);
    }
  }

  return {
    // The newest position (0 before the first event). Reading it in an
    // effect re-runs the effect for every appended event.
    get end() {
      return end;
    },
    // Counts appended `run_started` events, for an owner that only follows
    // Run starts.
    get runStarts() {
      return runStarts;
    },
    // Work-state readers follow starts/ends without polling on output chunks.
    get lifecycle() {
      return lifecycle;
    },
    // The Runs active now: the latest `connection_ready.active_runs`, plus
    // Runs started since, minus Runs that ended since. Retained events cannot
    // provide this for an owner that starts later or missed events, because
    // the terminal event of a listed Run may already have left the feed.
    get activeRuns() {
      return activeRuns;
    },
    append(serverEvent) {
      const position = end + 1;
      forgetEvicted(position - capacity);
      slots[position % capacity] = serverEvent;
      const runId = serverEvent?.payload?.run_id;
      if (runId && isTerminalRunEvent(serverEvent.type)) {
        terminalPositions.set(runId, position);
      }
      activeRuns = nextActiveRuns(activeRuns, serverEvent);
      if (serverEvent?.type === 'run_started') {
        runStarts += 1;
      }
      if (
        serverEvent?.type === 'run_started' ||
        isTerminalRunEvent(serverEvent?.type)
      ) {
        lifecycle += 1;
      }
      end = position;
    },
    resetActiveRuns(runs) {
      activeRuns = Array.isArray(runs) ? runs : [];
    },
    // The retained events after `cursor`, oldest first: `events[i]` sits at
    // position `from + i`. `missed` reports events after `cursor` that left
    // the feed before this read.
    read(cursor) {
      const after = Math.max(0, Math.trunc(Number(cursor) || 0));
      const from = Math.max(after + 1, end - capacity + 1);
      const events = [];
      for (let position = from; position <= end; position += 1) {
        events.push(slots[position % capacity]);
      }
      return { events, from, end, missed: from > after + 1 };
    },
    // Whether the feed still holds the terminal event of `runId`.
    retainsTerminal(runId) {
      return Boolean(runId) && terminalPositions.has(runId);
    },
  };
}

function nextActiveRuns(activeRuns, serverEvent) {
  const payload = serverEvent?.payload ?? {};
  const runId = payload.run_id;
  if (!runId) {
    return activeRuns;
  }
  const known = activeRuns.some((run) => run?.run_id === runId);
  if (serverEvent.type === 'run_started' && !known) {
    return [...activeRuns, activeRunFromStartedEvent(payload)];
  }
  if (isTerminalRunEvent(serverEvent.type) && known) {
    return activeRuns.filter((run) => run?.run_id !== runId);
  }
  return activeRuns;
}

// Same shape as a `connection_ready.active_runs` entry; the stream derives the
// SSE URL and fills controls/iterations from the Run's own events.
function activeRunFromStartedEvent(payload) {
  return {
    run_id: payload.run_id,
    agent_id: payload.agent_id,
    project_id: payload.project_id ?? null,
    session_id: payload.session_id,
    run_kind: payload.run_kind,
    status: 'running',
    started_at: payload.run_event_timestamp,
    ...(payload.contributes_to_agent_activity === false
      ? { contributes_to_agent_activity: false }
      : {}),
    ...(payload.source_session_id
      ? { source_session_id: payload.source_session_id }
      : {}),
  };
}
