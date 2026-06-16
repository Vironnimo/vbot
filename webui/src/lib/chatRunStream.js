import { reconnectBackoffDelay } from './backoff.js';
import { createBoundedKeySet } from './clientCaches.js';
import { t } from './i18n.js';
import {
  TERMINAL_RUN_EVENTS,
  appendCompactionCheckpoint,
  appendRunEvent,
  ensureSessionState,
  highestContiguousRunEventSequence,
  removeQueuedMessage,
  startRun,
} from './chatState.js';

const SSE_RECONNECT_DELAY_MS = 500;
const MAX_SSE_RECONNECT_ATTEMPTS = 3;
// Dedup only has to cover events that can still be re-delivered through
// App.svelte's bounded `runServerEvents` list (500 entries), so the cap just
// needs to comfortably exceed that window; everything older can be forgotten
// without risking a duplicate (handoff3 B10).
const MAX_HANDLED_RUN_SERVER_EVENT_KEYS = 2000;
const RUN_EVENT_FLUSH_DELAY_MS = 33;
const DELAYED_RUN_EVENT_TYPES = new Set([
  'assistant_output_delta',
  'reasoning_delta',
  'tool_call_delta',
]);
const RUN_SERVER_EVENT_TYPES = new Set([
  'run_started',
  'run_output',
  'run_completed',
  'run_cancelled',
  'run_failed',
]);

export function createChatRunStream({
  chatState,
  subscribeRunEvents,
  syncSessionQueue,
  isDisplayedSession,
  setActionError,
  updateSubAgentRunStatuses,
}) {
  const activeSubscriptions = {};
  const pendingReconnects = {};
  const pendingRunEventQueues = {};
  const pendingRunEventFlushes = {};
  const handledRunServerEventKeys = createBoundedKeySet(
    MAX_HANDLED_RUN_SERVER_EVENT_KEYS,
  );

  function subscribeToRun(sessionState, sseUrl, options = {}) {
    if (!sseUrl) {
      return;
    }
    if (sessionState.currentRun) {
      sessionState.currentRun.sseUrl = sseUrl;
    }
    activeSubscriptions[sessionState.key]?.close();
    clearPendingReconnect(sessionState.key);
    const afterSequence =
      options.afterSequence ?? highestContiguousRunEventSequence(sessionState);
    let retryAttempt = options.retryAttempt ?? 0;
    const subscription = subscribeRunEvents(
      sseUrl,
      {
        onEvent: ({ data }) => {
          // Events are flowing, so a later drop is a fresh transient failure,
          // not a continuation of earlier ones. Without this reset the
          // attempts accumulate across the whole run and a handful of drops
          // hours apart would permanently close the live stream.
          retryAttempt = 0;
          queueRunEvent(sessionState, data);
        },
        onError: (error) => {
          recoverRunStream(sessionState, sseUrl, retryAttempt, error);
        },
      },
      {
        afterSequence,
      },
    );
    activeSubscriptions[sessionState.key] = subscription;
  }

  function attachRunStream(sessionState, run, options = {}) {
    if (!sessionState || !run?.run_id) {
      return false;
    }

    const sseUrl =
      typeof run.sse_url === 'string' && run.sse_url
        ? run.sse_url
        : sseUrlForRun(run.run_id);
    const currentRun = sessionState.currentRun;
    const alreadySubscribed =
      Boolean(activeSubscriptions[sessionState.key]) &&
      currentRun?.runId === run.run_id &&
      currentRun?.sseUrl === sseUrl;

    if (currentRun?.runId !== run.run_id) {
      startRun(sessionState, { ...run, sse_url: sseUrl });
    } else {
      currentRun.status = run.status ?? currentRun.status;
      currentRun.sseUrl = sseUrl;
    }
    mergeRetainedRunEvents(sessionState, run.events, {
      fromServerEvent: true,
    });

    if (!alreadySubscribed) {
      subscribeToRun(sessionState, sseUrl, {
        afterSequence:
          options.afterSequence ??
          highestContiguousRunEventSequence(sessionState),
      });
    }

    return true;
  }

  function mergeRetainedRunEvents(sessionState, events, options = {}) {
    if (!Array.isArray(events) || events.length === 0) {
      return;
    }
    for (const eventData of events) {
      const event = appendRunEvent(sessionState, eventData);
      handleAppendedRunEvent(sessionState, event, options);
    }
  }

  function queueRunEvent(sessionState, eventData) {
    const sessionKey = sessionState.key;
    pendingRunEventQueues[sessionKey] ??= [];
    pendingRunEventQueues[sessionKey].push(eventData);
    if (!DELAYED_RUN_EVENT_TYPES.has(eventData?.type)) {
      flushPendingRunEvents(sessionKey);
      return;
    }
    scheduleRunEventFlush(sessionKey);
  }

  function scheduleRunEventFlush(sessionKey) {
    if (pendingRunEventFlushes[sessionKey] !== undefined) {
      return;
    }
    pendingRunEventFlushes[sessionKey] = setTimeout(() => {
      delete pendingRunEventFlushes[sessionKey];
      flushPendingRunEvents(sessionKey);
    }, RUN_EVENT_FLUSH_DELAY_MS);
  }

  function flushPendingRunEvents(sessionKey) {
    const pendingEvents = pendingRunEventQueues[sessionKey];
    if (!Array.isArray(pendingEvents) || pendingEvents.length === 0) {
      delete pendingRunEventQueues[sessionKey];
      clearPendingRunEventFlush(sessionKey);
      return null;
    }

    delete pendingRunEventQueues[sessionKey];
    clearPendingRunEventFlush(sessionKey);

    const sessionState = chatState.sessions[sessionKey];
    if (!sessionState) {
      return null;
    }

    let terminalEvent = null;
    for (const eventData of pendingEvents) {
      const event = appendRunEvent(sessionState, eventData);
      handleAppendedRunEvent(sessionState, event);
      if (event && TERMINAL_RUN_EVENTS.has(event.type)) {
        terminalEvent = event;
      }
    }
    return terminalEvent;
  }

  function handleAppendedRunEvent(sessionState, event, options = {}) {
    if (!event) {
      return;
    }
    trackSubAgentRunStatus(event);
    if (event.type === 'compaction_completed' && event.payload?.message) {
      appendCompactionCheckpoint(sessionState, event.payload.message);
    }
    if (
      event.type === 'run_started' &&
      typeof event.payload?.queue_item_id === 'string' &&
      event.payload.queue_item_id.length > 0
    ) {
      // The started run is now executing, so its queued-item handle is no
      // longer "pending" — drop it locally. The terminal-event
      // `syncSessionQueue` call below still re-fetches the server list, so
      // the projection stays consistent if the local removal races.
      removeQueuedMessage(sessionState, event.payload.queue_item_id);
    }
    if (TERMINAL_RUN_EVENTS.has(event.type)) {
      clearPendingReconnect(sessionState.key);
      if (!options.fromServerEvent || !activeSubscriptions[sessionState.key]) {
        closeRunSubscription(sessionState.key);
      }
      if (event.type !== 'run_failed') {
        setActionError('');
      }
      void syncSessionQueue(sessionState);
    }
  }

  function trackSubAgentRunStatus(event) {
    const updates = {};

    // The most recent tool call a run made, so a running sub-agent row can
    // show live activity instead of its frozen prompt preview. Recorded for
    // every run (like the `run:` status keys); only sub-agent rows read it,
    // run-scoped first with the session key as the run-id-less fallback.
    const toolName = toolNameFromRunEvent(event);
    if (toolName) {
      if (event.run_id) {
        updates[`runTool:${event.run_id}`] = toolName;
      }
      if (event.agent_id && event.session_id) {
        updates[`sessionTool:${event.agent_id}::${event.session_id}`] =
          toolName;
      }
    }

    const status = statusFromRunEvent(event);
    if (status) {
      if (event.run_id) {
        updates[`run:${event.run_id}`] = status;
      }
      if (event.agent_id && event.session_id) {
        updates[`session:${event.agent_id}::${event.session_id}`] = status;
      }

      // A reused child session must not surface the previous run's last tool
      // on run-id-less rows, so a fresh run clears the session-scoped name.
      if (event.type === 'run_started' && event.agent_id && event.session_id) {
        updates[`sessionTool:${event.agent_id}::${event.session_id}`] = '';
      }

      // A queued sub-agent spawn's persisted descriptor only knows its
      // queue_item_id. Recording the queue→run mapping when the queued run
      // starts lets presentation resolve that row to its own run id, so its
      // dot/result/duration lookups stay run-scoped even though the descriptor
      // never learns the run id.
      if (
        event.type === 'run_started' &&
        event.run_id &&
        typeof event.payload?.queue_item_id === 'string' &&
        event.payload.queue_item_id.length > 0
      ) {
        updates[`queueRun:${event.payload.queue_item_id}`] = event.run_id;
      }

      // Terminal events carry the run's real wall-clock duration. A
      // non-blocking sub-agent spawn returns immediately, so the parent's
      // spawn tool call has a ~0s duration; the child run's duration is the
      // meaningful runtime to show.
      const durationMs = runEventDurationMs(event);
      if (durationMs !== null) {
        if (event.run_id) {
          updates[`runDuration:${event.run_id}`] = durationMs;
        }
        if (event.agent_id && event.session_id) {
          updates[`sessionDuration:${event.agent_id}::${event.session_id}`] =
            durationMs;
        }
      }
    }

    if (Object.keys(updates).length > 0) {
      updateSubAgentRunStatuses(updates);
    }
  }

  function toolNameFromRunEvent(event) {
    if (event.type !== 'tool_call_started') {
      return '';
    }
    const name = event.payload?.tool_call?.name;
    return typeof name === 'string' ? name.trim() : '';
  }

  function runEventDurationMs(event) {
    const durationMs = event?.payload?.timing?.duration_ms;
    return Number.isFinite(durationMs) && durationMs >= 0 ? durationMs : null;
  }

  function statusFromRunEvent(event) {
    if (event.type === 'run_started') {
      return 'running';
    }
    if (event.type === 'run_completed') {
      return 'completed';
    }
    if (event.type === 'run_failed') {
      return 'failed';
    }
    if (event.type === 'run_cancelled') {
      return 'cancelled';
    }
    return '';
  }

  function recoverRunStream(sessionState, sseUrl, retryAttempt, error) {
    const sessionKey = sessionState.key;
    flushPendingRunEvents(sessionKey);
    const currentRun = sessionState.currentRun;
    if (!currentRun || currentRun.status !== 'running') {
      return;
    }

    if (retryAttempt < MAX_SSE_RECONNECT_ATTEMPTS) {
      setActionError(
        t(
          'errors.streamReconnecting',
          'The live stream closed. Reconnecting...',
        ),
      );
      if (pendingReconnects[sessionKey] !== undefined) {
        return;
      }
      closeRunSubscription(sessionKey);
      pendingReconnects[sessionKey] = setTimeout(
        () => {
          delete pendingReconnects[sessionKey];
          if (sessionState.currentRun?.runId !== currentRun.runId) {
            return;
          }
          subscribeToRun(sessionState, currentRun.sseUrl || sseUrl, {
            afterSequence: highestContiguousRunEventSequence(sessionState),
            retryAttempt: retryAttempt + 1,
          });
        },
        reconnectBackoffDelay(retryAttempt, {
          initialDelayMs: SSE_RECONNECT_DELAY_MS,
        }),
      );
      return;
    }

    setActionError(
      `${t(
        'errors.streamClosed',
        'The live stream closed before the run finished. Waiting for server status.',
      )} ${error?.message ?? ''}`,
    );
    closeRunSubscription(sessionState.key);
  }

  function handleServerEvents(singleEvent, events) {
    for (const serverEvent of normalizedRunServerEvents(singleEvent, events)) {
      const eventKey = runServerEventKey(serverEvent);
      if (!eventKey || handledRunServerEventKeys.has(eventKey)) {
        continue;
      }
      handledRunServerEventKeys.add(eventKey);
      handleRunServerEvent(serverEvent);
    }
  }

  function handleRunServerEvent(serverEvent) {
    const event = runEventFromServerEvent(serverEvent);
    if (!event?.agent_id || !event?.session_id) {
      return;
    }

    const sessionState = ensureSessionState(
      chatState,
      event.agent_id,
      event.session_id,
    );
    flushPendingRunEvents(sessionState.key);
    const appended = appendRunEvent(sessionState, event);
    handleAppendedRunEvent(sessionState, appended, { fromServerEvent: true });
    if (
      event.type === 'run_started' &&
      isDisplayedSession(event.agent_id, event.session_id)
    ) {
      attachRunStream(
        sessionState,
        {
          run_id: event.run_id,
          status: 'running',
          sse_url: sseUrlForRun(event.run_id),
          events: [],
        },
        { afterSequence: highestContiguousRunEventSequence(sessionState) },
      );
    }
  }

  function normalizedRunServerEvents(singleEvent, events) {
    const normalizedEvents = Array.isArray(events)
      ? events.filter(Boolean)
      : [];
    if (singleEvent) {
      normalizedEvents.push(singleEvent);
    }
    return normalizedEvents;
  }

  function runEventFromServerEvent(serverEvent) {
    const payload = serverEvent?.payload ?? {};
    const runEventType = payload.run_event_type;
    if (!RUN_SERVER_EVENT_TYPES.has(serverEvent?.type) || !runEventType) {
      return null;
    }

    const runPayload = { ...(payload.output ?? {}) };
    if (payload.status) {
      runPayload.status = payload.status;
    }
    if (payload.usage) {
      runPayload.usage = payload.usage;
    }
    if (payload.timing) {
      runPayload.timing = payload.timing;
    }

    return {
      type: runEventType,
      run_id: payload.run_id,
      agent_id: payload.agent_id,
      session_id: payload.session_id,
      sequence: payload.run_event_sequence,
      timestamp: payload.run_event_timestamp,
      payload: runPayload,
    };
  }

  function runServerEventKey(serverEvent) {
    const payload = serverEvent?.payload;
    if (
      !payload?.run_id ||
      (payload.run_event_sequence !== 0 && !payload.run_event_sequence)
    ) {
      return '';
    }
    return `${payload.run_id}:${payload.run_event_sequence}:${serverEvent.type}`;
  }

  function closeRunSubscription(sessionKey) {
    activeSubscriptions[sessionKey]?.close();
    delete activeSubscriptions[sessionKey];
  }

  function closeSubscriptionFor(sessionKey) {
    closeRunSubscription(sessionKey);
    clearPendingReconnect(sessionKey);
  }

  function closeSubscriptionsExcept(sessionKey) {
    for (const key of Object.keys(activeSubscriptions)) {
      if (key === sessionKey) {
        continue;
      }
      closeRunSubscription(key);
      clearPendingReconnect(key);
    }
  }

  function sseUrlForRun(runId) {
    return `/api/runs/${encodeURIComponent(String(runId))}/events`;
  }

  function clearPendingReconnect(sessionKey) {
    const timeoutId = pendingReconnects[sessionKey];
    if (timeoutId !== undefined) {
      clearTimeout(timeoutId);
      delete pendingReconnects[sessionKey];
    }
  }

  function clearPendingReconnects() {
    for (const key of Object.keys(pendingReconnects)) {
      clearPendingReconnect(key);
    }
  }

  function clearPendingRunEventFlush(sessionKey) {
    const timeoutId = pendingRunEventFlushes[sessionKey];
    if (timeoutId !== undefined) {
      clearTimeout(timeoutId);
      delete pendingRunEventFlushes[sessionKey];
    }
  }

  function clearPendingRunEventFlushes() {
    for (const key of Object.keys(pendingRunEventFlushes)) {
      clearPendingRunEventFlush(key);
    }
    for (const key of Object.keys(pendingRunEventQueues)) {
      delete pendingRunEventQueues[key];
    }
  }

  function closeSubscriptions() {
    for (const subscription of Object.values(activeSubscriptions)) {
      subscription.close();
    }
    for (const key of Object.keys(activeSubscriptions)) {
      delete activeSubscriptions[key];
    }
    clearPendingReconnects();
    clearPendingRunEventFlushes();
  }

  function applyConnectionSnapshot(snapshot) {
    const activeRuns = Array.isArray(snapshot?.active_runs)
      ? snapshot.active_runs
      : [];
    if (activeRuns.length === 0) {
      return;
    }

    const subAgentUpdates = {};
    for (const activeRun of activeRuns) {
      if (!activeRun?.run_id) {
        continue;
      }
      subAgentUpdates[`run:${activeRun.run_id}`] = 'running';
      if (activeRun.agent_id && activeRun.session_id) {
        subAgentUpdates[
          `session:${activeRun.agent_id}::${activeRun.session_id}`
        ] = 'running';
      }
    }
    if (Object.keys(subAgentUpdates).length > 0) {
      updateSubAgentRunStatuses(subAgentUpdates);
    }

    for (const activeRun of activeRuns) {
      if (!activeRun?.run_id || !activeRun.agent_id || !activeRun.session_id) {
        continue;
      }
      if (!isDisplayedSession(activeRun.agent_id, activeRun.session_id)) {
        continue;
      }
      const sessionState = ensureSessionState(
        chatState,
        activeRun.agent_id,
        activeRun.session_id,
      );
      attachRunStream(sessionState, {
        run_id: activeRun.run_id,
        status: 'running',
        sse_url: activeRun.sse_url,
        events: [],
      });
    }
  }

  return {
    applyConnectionSnapshot,
    attachRunStream,
    closeSubscriptionFor,
    closeSubscriptions,
    closeSubscriptionsExcept,
    handleServerEvents,
    subscribeToRun,
  };
}
