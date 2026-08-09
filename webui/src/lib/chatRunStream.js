import { reconnectBackoffDelay } from './backoff.js';
import { createBoundedKeySet } from './clientCaches.js';
import { t } from './i18n.js';
import {
  TERMINAL_RUN_EVENTS,
  appendRunEvent,
  ensureSessionState,
  formatAgentAddress,
  highestContiguousRunEventSequence,
  isRunActive,
  removeQueuedMessage,
  resetStaleRun,
  startRun,
} from './chatState.js';

const SSE_RECONNECT_DELAY_MS = 500;
const MAX_SSE_RECONNECT_ATTEMPTS = 3;
const SSE_HEARTBEAT_TIMEOUT_MS = 25_000;
const SSE_RECOVERY_RETRY_DELAY_MS = 5_000;
const RUN_EVENT_GAP_TIMEOUT_MS = 2_000;
const TERMINAL_RECONCILIATION_DELAY_MS = 1_000;
// Dedup only has to cover events that can still be re-delivered through
// App.svelte's bounded `runServerEvents` list (500 entries), so the cap just
// needs to comfortably exceed that window; everything older can be forgotten
// without risking a duplicate (handoff3 B10).
const MAX_HANDLED_RUN_SERVER_EVENT_KEYS = 2000;
const MAX_PENDING_ORDERED_RUN_EVENTS = 1024;
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
  'run_interrupted',
]);

// Session-scoped status keys are read against persisted spawn descriptors,
// which carry the child's BARE agent id. Live `/ws` events arrive re-addressed
// to the full `agent@projekt` form (the session-STATE key), while SSE-delivered
// run events stay bare — stripping the project suffix makes both sides write
// the same key. Session ids are globally unique UUIDs, so a bare key cannot
// collide across projects; an identity id has no `@` and passes unchanged.
function bareAgentIdForStatusKey(agentId) {
  const value = typeof agentId === 'string' ? agentId : '';
  const separatorIndex = value.indexOf('@');
  return separatorIndex === -1 ? value : value.slice(0, separatorIndex);
}

function defaultStreamDiagnostic(diagnostic) {
  console.warn('vBot Run stream recovery', diagnostic);
}

export function createChatRunStream({
  chatState,
  subscribeRunEvents,
  syncSessionQueue,
  reconcileRunSession = async () => false,
  isDisplayedSession,
  updateSubAgentRunStatuses,
  reportStreamDiagnostic = defaultStreamDiagnostic,
}) {
  const activeSubscriptions = {};
  const pendingReconnects = {};
  const pendingHeartbeatWatchdogs = {};
  const pendingRecoveryRetries = {};
  const pendingReconciliations = {};
  const pendingGapWatchdogs = {};
  const pendingTerminalReconciliations = {};
  const pendingRunEventQueues = {};
  const pendingRunEventFlushes = {};
  const orderedRunEventBuffers = {};
  let destroyed = false;
  const handledRunServerEventKeys = createBoundedKeySet(
    MAX_HANDLED_RUN_SERVER_EVENT_KEYS,
  );

  function reportDiagnostic(diagnostic) {
    try {
      reportStreamDiagnostic(diagnostic);
    } catch {
      // Diagnostics are best-effort and must never interfere with recovery.
    }
  }

  function subscribeToRun(sessionState, sseUrl, options = {}) {
    if (!sseUrl || destroyed) {
      return;
    }
    if (sessionState.currentRun) {
      sessionState.currentRun.sseUrl = sseUrl;
    }
    closeRunSubscription(sessionState.key);
    clearPendingReconnect(sessionState.key);
    clearPendingRecoveryRetry(sessionState.key);
    const afterSequence =
      options.afterSequence ?? highestContiguousRunEventSequence(sessionState);
    prepareOrderedRunEventBuffer(sessionState, afterSequence);
    let retryAttempt = options.retryAttempt ?? 0;
    let awaitingReplayHead = true;
    let subscription;
    const markStreamAlive = ({ resetRetryBudget = false } = {}) => {
      if (
        subscription &&
        activeSubscriptions[sessionState.key] !== subscription
      ) {
        return;
      }
      // Only a heartbeat or Run event proves the complete data path is
      // flowing. `open` alone can still be followed by a blackholed stream, so
      // it starts the watchdog without resetting consecutive failures.
      if (resetRetryBudget) {
        retryAttempt = 0;
      }
      sessionState.streamError = '';
      if (resetRetryBudget) {
        clearPendingRecoveryRetry(sessionState.key);
      }
      armHeartbeatWatchdog(sessionState, sseUrl, () => retryAttempt);
    };
    const markStreamHealthy = () => markStreamAlive({ resetRetryBudget: true });
    subscription = subscribeRunEvents(
      sseUrl,
      {
        onOpen: markStreamAlive,
        onHeartbeat: markStreamHealthy,
        onEvent: ({ data }) => {
          markStreamHealthy();
          queueRunEvent(sessionState, data, {
            acceptAsReplayHead: awaitingReplayHead,
          });
          awaitingReplayHead = false;
        },
        onError: (error) => {
          if (activeSubscriptions[sessionState.key] !== subscription) {
            return;
          }
          recoverRunStream(sessionState, sseUrl, retryAttempt, error);
        },
      },
      {
        afterSequence,
      },
    );
    activeSubscriptions[sessionState.key] = subscription;
    armHeartbeatWatchdog(sessionState, sseUrl, () => retryAttempt);
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
      currentRun.startedAt = run.started_at ?? currentRun.startedAt ?? null;
      if (Number.isInteger(run.iteration_count) && run.iteration_count >= 0) {
        currentRun.iterationCount = run.iteration_count;
      }
      if (run.contributes_to_agent_activity === false) {
        currentRun.contributesToAgentActivity = false;
      }
    }
    mergeRetainedRunEvents(sessionState, run.events);

    if (!alreadySubscribed) {
      subscribeToRun(sessionState, sseUrl, {
        afterSequence:
          options.afterSequence ??
          highestContiguousRunEventSequence(sessionState),
      });
    }

    return true;
  }

  function mergeRetainedRunEvents(sessionState, events) {
    if (!Array.isArray(events) || events.length === 0) {
      return;
    }
    for (const eventData of events) {
      const event = appendRunEvent(sessionState, eventData);
      handleAppendedRunEvent(sessionState, event);
    }
  }

  function mergeRunResponse(sessionState, run) {
    if (
      !sessionState ||
      !run?.run_id ||
      sessionState.currentRun?.runId !== run.run_id
    ) {
      return false;
    }
    mergeRetainedRunEvents(sessionState, run.events);
    return true;
  }

  function queueRunEvent(sessionState, eventData, options = {}) {
    const sessionKey = sessionState.key;
    pendingRunEventQueues[sessionKey] ??= [];
    pendingRunEventQueues[sessionKey].push({ eventData, options });
    if (!DELAYED_RUN_EVENT_TYPES.has(eventData?.type)) {
      const terminalEvent = flushPendingRunEvents(sessionKey);
      if (
        TERMINAL_RUN_EVENTS.has(eventData?.type) &&
        terminalEvent?.run_id !== eventData?.run_id
      ) {
        scheduleTerminalReconciliation(sessionState, eventData);
      }
      return;
    }
    scheduleRunEventFlush(sessionKey);
  }

  function prepareOrderedRunEventBuffer(sessionState, afterSequence = 0) {
    const runId = sessionState.currentRun?.runId ?? '';
    if (!runId) {
      delete orderedRunEventBuffers[sessionState.key];
      return null;
    }
    const nextSequence =
      Math.max(
        0,
        Math.trunc(afterSequence),
        highestContiguousRunEventSequence(sessionState),
      ) + 1;
    const existing = orderedRunEventBuffers[sessionState.key];
    if (existing?.runId === runId) {
      existing.nextSequence = Math.max(existing.nextSequence, nextSequence);
      return existing;
    }
    const buffer = {
      runId,
      nextSequence,
      // A plain Map is intentional: this transport-owned buffer is not UI
      // state. Only events promoted through appendRunEvent become reactive.
      pending: new Map(),
    };
    orderedRunEventBuffers[sessionState.key] = buffer;
    return buffer;
  }

  function appendOrderedRunEvent(sessionState, eventData, options = {}) {
    const runId = typeof eventData?.run_id === 'string' ? eventData.run_id : '';
    const sequence = Number(eventData?.sequence);
    if (!runId || !Number.isInteger(sequence) || sequence < 1) {
      const appended = appendRunEvent(sessionState, eventData);
      return appended ? [appended] : [];
    }

    let buffer = orderedRunEventBuffers[sessionState.key];
    if (buffer?.runId !== runId) {
      const afterSequence =
        sessionState.currentRun?.runId === runId
          ? highestContiguousRunEventSequence(sessionState)
          : 0;
      buffer = prepareOrderedRunEventBuffer(sessionState, afterSequence);
    }
    if (!buffer || sequence < buffer.nextSequence) {
      return [];
    }
    // The first Run event on a new SSE connection is the server's oldest
    // retained event after our requested cursor. If it starts beyond the
    // expected sequence, the missing prefix has already fallen out of the
    // bounded replay window and can never arrive on this connection. Rebase
    // only at that transport-proven boundary; later gaps still wait for their
    // missing event so mirrored WebSocket terminal output cannot overtake SSE.
    if (options.acceptAsReplayHead && sequence > buffer.nextSequence) {
      reportDiagnostic({
        reason: 'replay_rebased',
        runId,
        expectedSequence: buffer.nextSequence,
        receivedSequence: sequence,
      });
      buffer.nextSequence = sequence;
      for (const pendingSequence of buffer.pending.keys()) {
        if (pendingSequence < sequence) {
          buffer.pending.delete(pendingSequence);
        }
      }
    }
    if (sequence - buffer.nextSequence >= MAX_PENDING_ORDERED_RUN_EVENTS) {
      armGapWatchdog(sessionState, buffer, sequence);
      return [];
    }
    if (!buffer.pending.has(sequence)) {
      buffer.pending.set(sequence, eventData);
    }

    const appendedEvents = [];
    while (buffer.pending.has(buffer.nextSequence)) {
      const nextEventData = buffer.pending.get(buffer.nextSequence);
      buffer.pending.delete(buffer.nextSequence);
      buffer.nextSequence += 1;
      const appended = appendRunEvent(sessionState, nextEventData);
      if (appended) {
        appendedEvents.push(appended);
      }
    }
    syncGapWatchdog(sessionState, buffer);
    return appendedEvents;
  }

  function syncGapWatchdog(sessionState, buffer) {
    if (buffer.pending.size === 0) {
      clearGapWatchdog(sessionState.key);
      return;
    }
    const firstPendingSequence = Math.min(...buffer.pending.keys());
    if (firstPendingSequence <= buffer.nextSequence) {
      clearGapWatchdog(sessionState.key);
      return;
    }
    armGapWatchdog(sessionState, buffer, firstPendingSequence);
  }

  function armGapWatchdog(sessionState, buffer, receivedSequence) {
    const sessionKey = sessionState.key;
    const existing = pendingGapWatchdogs[sessionKey];
    if (
      existing?.runId === buffer.runId &&
      existing.expectedSequence === buffer.nextSequence
    ) {
      existing.receivedSequence = Math.max(
        existing.receivedSequence,
        receivedSequence,
      );
      return;
    }
    clearGapWatchdog(sessionKey);
    const gap = {
      runId: buffer.runId,
      expectedSequence: buffer.nextSequence,
      receivedSequence,
      timeoutId: null,
    };
    gap.timeoutId = setTimeout(() => {
      if (pendingGapWatchdogs[sessionKey] !== gap) {
        return;
      }
      delete pendingGapWatchdogs[sessionKey];
      const currentRun = sessionState.currentRun;
      const currentBuffer = orderedRunEventBuffers[sessionKey];
      if (
        currentRun?.runId !== gap.runId ||
        currentRun.status !== 'running' ||
        currentBuffer?.runId !== gap.runId ||
        currentBuffer.nextSequence !== gap.expectedSequence
      ) {
        return;
      }
      reportDiagnostic({
        reason: 'sequence_gap_timeout',
        runId: gap.runId,
        expectedSequence: gap.expectedSequence,
        receivedSequence: gap.receivedSequence,
      });
      recoverRunStream(
        sessionState,
        currentRun.sseUrl,
        0,
        new Error(
          `Run event sequence gap at ${gap.expectedSequence} before ${gap.receivedSequence}.`,
        ),
      );
    }, RUN_EVENT_GAP_TIMEOUT_MS);
    pendingGapWatchdogs[sessionKey] = gap;
  }

  function scheduleTerminalReconciliation(sessionState, event) {
    const sessionKey = sessionState.key;
    const runId = event?.run_id;
    if (!runId || pendingTerminalReconciliations[sessionKey]?.runId === runId) {
      return;
    }
    clearTerminalReconciliation(sessionKey);
    const pending = {
      runId,
      terminalSequence: event.sequence,
      timeoutId: null,
    };
    pending.timeoutId = setTimeout(() => {
      if (pendingTerminalReconciliations[sessionKey] !== pending) {
        return;
      }
      delete pendingTerminalReconciliations[sessionKey];
      if (
        sessionState.currentRun?.runId !== runId ||
        sessionState.currentRun.status !== 'running'
      ) {
        return;
      }
      const buffer = orderedRunEventBuffers[sessionKey];
      reportDiagnostic({
        reason: 'terminal_event_blocked',
        runId,
        expectedSequence: buffer?.nextSequence ?? null,
        receivedSequence: pending.terminalSequence ?? null,
      });
      sessionState.streamError = t(
        'errors.streamClosed',
        'The live stream closed before the run finished. Waiting for server status.',
      );
      closeRunSubscription(sessionKey);
      void reconcileAfterStreamFailure(sessionState, runId);
    }, TERMINAL_RECONCILIATION_DELAY_MS);
    pendingTerminalReconciliations[sessionKey] = pending;
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
    for (const pendingEvent of pendingEvents) {
      for (const event of appendOrderedRunEvent(
        sessionState,
        pendingEvent.eventData,
        pendingEvent.options,
      )) {
        handleAppendedRunEvent(sessionState, event);
        if (TERMINAL_RUN_EVENTS.has(event.type)) {
          terminalEvent = event;
        }
      }
    }
    return terminalEvent;
  }

  function handleAppendedRunEvent(sessionState, event) {
    if (!event) {
      return;
    }
    if (
      event.contributes_to_agent_activity !== false ||
      event.type === 'subagent_status_changed'
    ) {
      trackSubAgentRunStatus(event);
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
      delete orderedRunEventBuffers[sessionState.key];
      clearGapWatchdog(sessionState.key);
      clearTerminalReconciliation(sessionState.key);
      clearPendingReconnect(sessionState.key);
      clearPendingRecoveryRetry(sessionState.key);
      closeRunSubscription(sessionState.key);
      sessionState.streamError = '';
      void syncSessionQueue(sessionState);
    }
  }

  function trackSubAgentRunStatus(event) {
    const updates = {};
    trackExplicitSubAgentStatus(event, updates);
    const statusAgentId = bareAgentIdForStatusKey(event.agent_id);

    // The most recent tool call a run made, so a running sub-agent row can
    // show live activity instead of its frozen prompt preview. Recorded for
    // every run (like the `run:` status keys); only sub-agent rows read it,
    // run-scoped first with the session key as the run-id-less fallback.
    const toolName = toolNameFromRunEvent(event);
    if (toolName) {
      if (event.run_id) {
        updates[`runTool:${event.run_id}`] = toolName;
      }
      if (statusAgentId && event.session_id) {
        updates[`sessionTool:${statusAgentId}::${event.session_id}`] = toolName;
      }
    }

    const status = statusFromRunEvent(event);
    if (status) {
      if (event.run_id) {
        updates[`run:${event.run_id}`] = status;
      }
      if (statusAgentId && event.session_id) {
        updates[`session:${statusAgentId}::${event.session_id}`] = status;
      }

      // A reused child session must not surface the previous run's last tool
      // on run-id-less rows, so a fresh run clears the session-scoped name.
      if (event.type === 'run_started' && statusAgentId && event.session_id) {
        updates[`sessionTool:${statusAgentId}::${event.session_id}`] = '';
      }
      if (event.type === 'run_started' && event.timestamp) {
        if (event.run_id) {
          updates[`runStarted:${event.run_id}`] = event.timestamp;
        }
        if (statusAgentId && event.session_id) {
          updates[`sessionStarted:${statusAgentId}::${event.session_id}`] =
            event.timestamp;
        }
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
      // background sub-agent spawn returns immediately, so the parent's
      // spawn tool call has a ~0s duration; the child run's duration is the
      // meaningful runtime to show.
      const durationMs = runEventDurationMs(event);
      if (durationMs !== null) {
        if (event.run_id) {
          updates[`runDuration:${event.run_id}`] = durationMs;
        }
        if (statusAgentId && event.session_id) {
          updates[`sessionDuration:${statusAgentId}::${event.session_id}`] =
            durationMs;
        }
      }
    }

    if (Object.keys(updates).length > 0) {
      updateSubAgentRunStatuses(updates);
    }
  }

  function trackExplicitSubAgentStatus(event, updates) {
    if (event.type !== 'subagent_status_changed') {
      return;
    }
    const data = event.payload?.data;
    const childAgentId =
      typeof data?.agent_id === 'string' ? data.agent_id.trim() : '';
    const childSessionId =
      typeof data?.session_id === 'string' ? data.session_id.trim() : '';
    const childStatus =
      typeof data?.status === 'string' ? data.status.trim() : '';
    if (!childAgentId || !childSessionId || !childStatus) {
      return;
    }

    const childProjectId =
      typeof data?.project_id === 'string' ? data.project_id.trim() : '';
    const childAddresses = new Set([
      bareAgentIdForStatusKey(childAgentId),
      formatAgentAddress(childAgentId, childProjectId),
    ]);
    for (const childAddress of childAddresses) {
      if (childAddress) {
        updates[`session:${childAddress}::${childSessionId}`] = childStatus;
      }
    }

    const childRunId =
      typeof data?.run_id === 'string' ? data.run_id.trim() : '';
    if (childRunId) {
      updates[`run:${childRunId}`] = childStatus;
    }
    const childStartedAt =
      typeof data?.started_at === 'string' ? data.started_at.trim() : '';
    if (childStartedAt) {
      if (childRunId) {
        updates[`runStarted:${childRunId}`] = childStartedAt;
      }
      for (const childAddress of childAddresses) {
        if (childAddress) {
          updates[`sessionStarted:${childAddress}::${childSessionId}`] =
            childStartedAt;
        }
      }
    }
    const queueItemId =
      typeof data?.queue_item_id === 'string' ? data.queue_item_id.trim() : '';
    if (queueItemId) {
      updates[`queue:${queueItemId}`] = childStatus;
      if (childRunId) {
        updates[`queueRun:${queueItemId}`] = childRunId;
      }
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
    if (event.type === 'run_interrupted') {
      return 'interrupted';
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

    reportDiagnostic({
      reason:
        retryAttempt < MAX_SSE_RECONNECT_ATTEMPTS
          ? 'stream_reconnect'
          : 'stream_recovery_exhausted',
      runId: currentRun.runId,
      afterSequence: highestContiguousRunEventSequence(sessionState),
      retryAttempt,
      errorType:
        typeof error?.name === 'string' && error.name ? error.name : 'Error',
    });

    if (retryAttempt < MAX_SSE_RECONNECT_ATTEMPTS) {
      sessionState.streamError = t(
        'errors.streamReconnecting',
        'The live stream closed. Reconnecting...',
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

    sessionState.streamError = `${t(
      'errors.streamClosed',
      'The live stream closed before the run finished. Waiting for server status.',
    )} ${error?.message ?? ''}`;
    closeRunSubscription(sessionState.key);
    void reconcileAfterStreamFailure(sessionState, currentRun.runId);
  }

  function armHeartbeatWatchdog(sessionState, sseUrl, retryAttempt) {
    const sessionKey = sessionState.key;
    clearHeartbeatWatchdog(sessionKey);
    const runId = sessionState.currentRun?.runId;
    if (!runId || sessionState.currentRun?.status !== 'running') {
      return;
    }
    pendingHeartbeatWatchdogs[sessionKey] = setTimeout(() => {
      delete pendingHeartbeatWatchdogs[sessionKey];
      if (sessionState.currentRun?.runId !== runId) {
        return;
      }
      recoverRunStream(
        sessionState,
        sseUrl,
        retryAttempt(),
        new Error('No stream heartbeat received.'),
      );
    }, SSE_HEARTBEAT_TIMEOUT_MS);
  }

  function reconcileAfterStreamFailure(sessionState, expectedRunId) {
    const sessionKey = sessionState.key;
    const reconciliationKey = `${sessionKey}::${expectedRunId}`;
    if (pendingReconciliations[reconciliationKey]) {
      return pendingReconciliations[reconciliationKey];
    }

    const reconciliation = Promise.resolve()
      .then(() => reconcileRunSession(sessionState, expectedRunId))
      .catch(() => false)
      .then((reconciled) => {
        if (destroyed) {
          return;
        }
        if (
          sessionState.currentRun?.runId !== expectedRunId ||
          sessionState.currentRun?.status !== 'running'
        ) {
          clearPendingRecoveryRetry(sessionKey);
          return;
        }
        if (reconciled) {
          if (activeSubscriptions[sessionKey]) {
            sessionState.streamError = '';
          }
          return;
        }
        scheduleRecoveryRetry(sessionState, expectedRunId);
      })
      .finally(() => {
        if (pendingReconciliations[reconciliationKey] === reconciliation) {
          delete pendingReconciliations[reconciliationKey];
        }
      });
    pendingReconciliations[reconciliationKey] = reconciliation;
    return reconciliation;
  }

  function scheduleRecoveryRetry(sessionState, expectedRunId) {
    const sessionKey = sessionState.key;
    if (pendingRecoveryRetries[sessionKey] !== undefined) {
      return;
    }
    pendingRecoveryRetries[sessionKey] = setTimeout(() => {
      delete pendingRecoveryRetries[sessionKey];
      if (
        sessionState.currentRun?.runId === expectedRunId &&
        sessionState.currentRun?.status === 'running'
      ) {
        void reconcileAfterStreamFailure(sessionState, expectedRunId);
      }
    }, SSE_RECOVERY_RETRY_DELAY_MS);
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
    const displayed = isDisplayedSession(event.agent_id, event.session_id);
    flushPendingRunEvents(sessionState.key);
    // A displayed Run has both its complete SSE stream and the stable events
    // mirrored over WebSocket. Feed both transports through one contiguous
    // reducer so a mirrored terminal event cannot overtake missing output.
    // Non-displayed Runs intentionally have no SSE subscription, while their
    // WebSocket feed omits high-volume deltas and therefore has sequence gaps;
    // reduce those stable events directly in WebSocket order. Opening such a
    // Session later reloads its durable History rather than relying on this
    // transient projection.
    if (
      event.type !== 'run_started' &&
      sessionState.currentRun?.runId === event.run_id &&
      displayed
    ) {
      let terminalAppended = false;
      for (const appendedEvent of appendOrderedRunEvent(sessionState, event)) {
        handleAppendedRunEvent(sessionState, appendedEvent);
        terminalAppended ||= TERMINAL_RUN_EVENTS.has(appendedEvent.type);
      }
      if (TERMINAL_RUN_EVENTS.has(event.type) && !terminalAppended) {
        scheduleTerminalReconciliation(sessionState, event);
      }
      return;
    }
    const appended = appendRunEvent(sessionState, event);
    handleAppendedRunEvent(sessionState, appended);
    if (event.type === 'run_started' && displayed) {
      attachRunStream(
        sessionState,
        {
          run_id: event.run_id,
          status: 'running',
          sse_url: sseUrlForRun(event.run_id),
          ...(event.contributes_to_agent_activity === false
            ? { contributes_to_agent_activity: false }
            : {}),
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
    if (payload.session_usage) {
      runPayload.session_usage = payload.session_usage;
    }
    if (payload.context_usage) {
      runPayload.context_usage = payload.context_usage;
    }
    if (payload.timing) {
      runPayload.timing = payload.timing;
    }
    if (typeof payload.error === 'string') {
      runPayload.error = payload.error;
    }

    return {
      type: runEventType,
      run_id: payload.run_id,
      // The server sends a bare agent id plus the run's project. Session state
      // is keyed by the outside `agent@projekt` address, so rebuild it here at
      // the single ingestion seam; an identity run (no project) yields the bare
      // id unchanged, keeping the identity path byte-identical.
      agent_id: formatAgentAddress(payload.agent_id, payload.project_id),
      session_id: payload.session_id,
      ...(payload.contributes_to_agent_activity === false
        ? { contributes_to_agent_activity: false }
        : {}),
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
    clearHeartbeatWatchdog(sessionKey);
    clearGapWatchdog(sessionKey);
  }

  function closeSubscriptionFor(sessionKey) {
    closeRunSubscription(sessionKey);
    delete orderedRunEventBuffers[sessionKey];
    clearPendingReconnect(sessionKey);
    clearPendingRecoveryRetry(sessionKey);
    clearTerminalReconciliation(sessionKey);
  }

  function closeSubscriptionsExcept(sessionKey) {
    const subscriptionKeys = new Set([
      ...Object.keys(activeSubscriptions),
      ...Object.keys(pendingReconnects),
      ...Object.keys(pendingHeartbeatWatchdogs),
      ...Object.keys(pendingRecoveryRetries),
      ...Object.keys(pendingGapWatchdogs),
      ...Object.keys(pendingTerminalReconciliations),
    ]);
    for (const key of subscriptionKeys) {
      if (key === sessionKey) {
        continue;
      }
      closeSubscriptionFor(key);
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

  function clearHeartbeatWatchdog(sessionKey) {
    const timeoutId = pendingHeartbeatWatchdogs[sessionKey];
    if (timeoutId !== undefined) {
      clearTimeout(timeoutId);
      delete pendingHeartbeatWatchdogs[sessionKey];
    }
  }

  function clearHeartbeatWatchdogs() {
    for (const key of Object.keys(pendingHeartbeatWatchdogs)) {
      clearHeartbeatWatchdog(key);
    }
  }

  function clearPendingRecoveryRetry(sessionKey) {
    const timeoutId = pendingRecoveryRetries[sessionKey];
    if (timeoutId !== undefined) {
      clearTimeout(timeoutId);
      delete pendingRecoveryRetries[sessionKey];
    }
  }

  function clearPendingRecoveryRetries() {
    for (const key of Object.keys(pendingRecoveryRetries)) {
      clearPendingRecoveryRetry(key);
    }
  }

  function clearGapWatchdog(sessionKey) {
    const pending = pendingGapWatchdogs[sessionKey];
    if (pending) {
      clearTimeout(pending.timeoutId);
      delete pendingGapWatchdogs[sessionKey];
    }
  }

  function clearGapWatchdogs() {
    for (const key of Object.keys(pendingGapWatchdogs)) {
      clearGapWatchdog(key);
    }
  }

  function clearTerminalReconciliation(sessionKey) {
    const pending = pendingTerminalReconciliations[sessionKey];
    if (pending) {
      clearTimeout(pending.timeoutId);
      delete pendingTerminalReconciliations[sessionKey];
    }
  }

  function clearTerminalReconciliations() {
    for (const key of Object.keys(pendingTerminalReconciliations)) {
      clearTerminalReconciliation(key);
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
    destroyed = true;
    for (const subscription of Object.values(activeSubscriptions)) {
      subscription.close();
    }
    for (const key of Object.keys(activeSubscriptions)) {
      delete activeSubscriptions[key];
    }
    clearPendingReconnects();
    clearHeartbeatWatchdogs();
    clearPendingRecoveryRetries();
    clearGapWatchdogs();
    clearTerminalReconciliations();
    clearPendingRunEventFlushes();
    for (const key of Object.keys(orderedRunEventBuffers)) {
      delete orderedRunEventBuffers[key];
    }
  }

  function applyConnectionSnapshot(snapshot) {
    if (!Array.isArray(snapshot?.active_runs)) {
      return;
    }
    const activeRuns = snapshot.active_runs;
    const activeRunIds = new Set(
      activeRuns
        .map((activeRun) => activeRun?.run_id)
        .filter((runId) => typeof runId === 'string' && runId.length > 0),
    );

    for (const sessionState of Object.values(chatState.sessions)) {
      const currentRunId = sessionState.currentRun?.runId;
      if (
        isRunActive(sessionState) &&
        currentRunId &&
        !activeRunIds.has(currentRunId)
      ) {
        closeSubscriptionFor(sessionState.key);
        if (isDisplayedSession(sessionState.agentId, sessionState.sessionId)) {
          sessionState.streamError = t(
            'errors.streamClosed',
            'The live stream closed before the run finished. Waiting for server status.',
          );
          void reconcileAfterStreamFailure(sessionState, currentRunId);
        } else {
          resetStaleRun(sessionState);
        }
      }
    }

    const subAgentUpdates = {};
    for (const activeRun of activeRuns) {
      if (
        !activeRun?.run_id ||
        activeRun.contributes_to_agent_activity === false
      ) {
        continue;
      }
      subAgentUpdates[`run:${activeRun.run_id}`] = 'running';
      if (activeRun.started_at) {
        subAgentUpdates[`runStarted:${activeRun.run_id}`] =
          activeRun.started_at;
      }
      // Status keys stay bare (the snapshot's agent_id already is) so they meet
      // the descriptor-derived reads; only session STATE below keys by address.
      if (activeRun.agent_id && activeRun.session_id) {
        subAgentUpdates[
          `session:${activeRun.agent_id}::${activeRun.session_id}`
        ] = 'running';
        if (activeRun.started_at) {
          subAgentUpdates[
            `sessionStarted:${activeRun.agent_id}::${activeRun.session_id}`
          ] = activeRun.started_at;
        }
      }
    }
    updateSubAgentRunStatuses(subAgentUpdates, { replaceActive: true });

    for (const activeRun of activeRuns) {
      if (!activeRun?.run_id || !activeRun.agent_id || !activeRun.session_id) {
        continue;
      }
      const agentAddress = formatAgentAddress(
        activeRun.agent_id,
        activeRun.project_id,
      );
      const sessionState = ensureSessionState(
        chatState,
        agentAddress,
        activeRun.session_id,
      );
      if (sessionState.currentRun?.runId !== activeRun.run_id) {
        startRun(sessionState, {
          run_id: activeRun.run_id,
          status: 'running',
          started_at: activeRun.started_at,
          sse_url: activeRun.sse_url,
          iteration_count: activeRun.iteration_count,
          ...(activeRun.contributes_to_agent_activity === false
            ? { contributes_to_agent_activity: false }
            : {}),
          events: [],
        });
      }
      if (!isDisplayedSession(agentAddress, activeRun.session_id)) {
        continue;
      }
      attachRunStream(sessionState, {
        run_id: activeRun.run_id,
        status: 'running',
        started_at: activeRun.started_at,
        sse_url: activeRun.sse_url,
        iteration_count: activeRun.iteration_count,
        ...(activeRun.contributes_to_agent_activity === false
          ? { contributes_to_agent_activity: false }
          : {}),
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
    mergeRunResponse,
    subscribeToRun,
  };
}
