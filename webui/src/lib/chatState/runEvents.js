import {
  CHAT_STATUS_RUNNING,
  TERMINAL_RUN_EVENTS,
  updateSessionUsage,
  CHAT_STATUS_IDLE,
  TERMINAL_VISIBLE_DRAFT_EVENT_TYPES,
  CHAT_STATUS_COMPLETED,
  CHAT_STATUS_FAILED,
  CHAT_STATUS_CANCELLED,
  CHAT_STATUS_INTERRUPTED,
} from './sessionState.js';
import { t } from '../i18n.js';
import {
  RUN_EVENT_STREAM_ATTEMPT_RESTARTED,
  RUN_EVENT_TOOL_CALL_DELTA,
} from '../api.js';
import {
  isStreamingDeltaRunEvent,
  appendCompressedStreamingRunEvent,
  streamingDeltaEventKey,
  advanceStreamingPhase,
  discardStreamingAttempt,
} from './streamingEvents.js';

export function startRun(sessionState, run) {
  sessionState.currentRun = {
    runId: run.run_id,
    controls: run.controls ?? {},
    controlsSequence: run.controls_sequence ?? 0,
    sseUrl: run.sse_url,
    status: run.status ?? CHAT_STATUS_RUNNING,
    startedAt:
      run.started_at ??
      (run.events ?? []).find((event) => event?.type === 'run_started')
        ?.timestamp ??
      null,
    iterationCount:
      Number.isInteger(run.iteration_count) && run.iteration_count >= 0
        ? run.iteration_count
        : 0,
    ...(run.contributes_to_agent_activity === false ||
    run.contributesToAgentActivity === false
      ? { contributesToAgentActivity: false }
      : {}),
  };
  sessionState.status = CHAT_STATUS_RUNNING;
  sessionState.error = null;
  sessionState.streamError = '';
  sessionState.streamStatus = CHAT_STATUS_RUNNING;
  sessionState.streamingRunEvents = [];
  sessionState.streamingPhase = 0;
  sessionState.seenStreamingEventKeys = new Set();
  appendRunEvents(sessionState, run.events ?? []);
  return sessionState.currentRun;
}

export function appendRunEvent(sessionState, event) {
  const normalizedEvent = normalizeRunEvent(event);
  if (!normalizedEvent) {
    return null;
  }
  if (isStreamingDeltaRunEvent(normalizedEvent.type)) {
    const eventKey = streamingDeltaEventKey(normalizedEvent);
    if (eventKey && sessionState.seenStreamingEventKeys.has(eventKey)) {
      return normalizedEvent;
    }
    if (eventKey) {
      sessionState.seenStreamingEventKeys.add(eventKey);
    }
    appendCompressedStreamingRunEvent(sessionState, normalizedEvent);
    return normalizedEvent;
  }
  if (
    sessionState.runEvents.some(
      (existingEvent) =>
        existingEvent.sequence === normalizedEvent.sequence &&
        existingEvent.run_id === normalizedEvent.run_id,
    )
  ) {
    return normalizedEvent;
  }

  sessionState.runEvents = [...sessionState.runEvents, normalizedEvent];
  if (normalizedEvent.payload?.context_usage) {
    sessionState.contextUsage = normalizedEvent.payload.context_usage;
  }
  if (normalizedEvent.type === RUN_EVENT_STREAM_ATTEMPT_RESTARTED) {
    discardStreamingAttempt(sessionState, normalizedEvent.run_id);
  }
  if (normalizedEvent.type === 'run_started') {
    beginRunFromEvent(sessionState, normalizedEvent);
  }
  if (normalizedEvent.type === 'run_controls_changed') {
    applyRunControls(sessionState, {
      run_id: normalizedEvent.run_id,
      controls: normalizedEvent.payload,
      controls_sequence: normalizedEvent.sequence,
    });
  }
  if (
    normalizedEvent.type === 'compaction_aborted' &&
    normalizedEvent.payload?.requested_by_user &&
    sessionState.currentRun?.runId === normalizedEvent.run_id
  ) {
    sessionState.actionError = t(
      'chat.compactionNotApplied',
      'Compaction was not applied. The Run continues with its current context.',
    );
  }
  if (normalizedEvent.type === 'model_step_usage') {
    applyModelStepUsage(sessionState, normalizedEvent.payload);
  }
  advanceStreamingPhase(sessionState, normalizedEvent);
  if (TERMINAL_RUN_EVENTS.has(normalizedEvent.type)) {
    finishRun(sessionState, normalizedEvent);
  }
  return normalizedEvent;
}

export function applyRunControls(sessionState, run) {
  const current = sessionState?.currentRun;
  if (
    !current ||
    current.runId !== run?.run_id ||
    !run.controls ||
    (run.controls_sequence ?? 0) < (current.controlsSequence ?? 0)
  )
    return;
  current.controls = run.controls;
  current.controlsSequence = run.controls_sequence ?? 0;
}

function applyModelStepUsage(sessionState, payload) {
  if (payload?.usage) {
    updateSessionUsage(sessionState, payload.usage);
  }
  if (payload?.session_usage) {
    sessionState.sessionUsage = payload.session_usage;
  }
  if (
    sessionState.currentRun &&
    Number.isInteger(payload?.iteration_count) &&
    payload.iteration_count >= 0
  ) {
    sessionState.currentRun.iterationCount = payload.iteration_count;
  }
}

function appendRunEvents(sessionState, events) {
  for (const event of events) {
    appendRunEvent(sessionState, event);
  }
  return sessionState.runEvents;
}

function beginRunFromEvent(sessionState, event) {
  const currentRun = sessionState.currentRun;
  const isSameRun = currentRun?.runId === event.run_id;
  const currentSseUrl = isSameRun ? currentRun.sseUrl : '';
  sessionState.currentRun = {
    runId: event.run_id,
    controls: isSameRun ? currentRun.controls : {},
    controlsSequence: isSameRun ? currentRun.controlsSequence : 0,
    sseUrl: currentSseUrl,
    status: CHAT_STATUS_RUNNING,
    startedAt:
      event.timestamp ?? (isSameRun ? currentRun?.startedAt : null) ?? null,
    iterationCount:
      isSameRun && Number.isInteger(currentRun?.iterationCount)
        ? currentRun.iterationCount
        : 0,
    ...(event.contributes_to_agent_activity === false
      ? { contributesToAgentActivity: false }
      : {}),
  };
  sessionState.status = CHAT_STATUS_RUNNING;
  sessionState.error = null;
  sessionState.streamStatus = CHAT_STATUS_RUNNING;
  if (isSameRun) {
    return;
  }
  sessionState.streamingRunEvents = [];
  sessionState.streamingPhase = 0;
  sessionState.seenStreamingEventKeys = new Set();
}

export function finishRun(sessionState, event) {
  const type = event?.type;
  const status = event?.payload?.status;
  const completedRunId = event?.run_id ?? '';
  const contributesToAgentActivity =
    event?.contributes_to_agent_activity !== false;
  if (sessionState.currentRun) {
    sessionState.currentRun.status = status ?? terminalStatus(type);
    if (
      Number.isInteger(event?.payload?.iteration_count) &&
      event.payload.iteration_count >= 0
    ) {
      sessionState.currentRun.iterationCount = event.payload.iteration_count;
    }
  }
  sessionState.status = status ?? terminalStatus(type);
  sessionState.streamStatus = CHAT_STATUS_IDLE;
  sessionState.streamError = '';
  const cancelledToolPreviews =
    type === 'run_cancelled'
      ? sessionState.streamingRunEvents.filter(
          (streamingEvent) => streamingEvent.type === RUN_EVENT_TOOL_CALL_DELTA,
        )
      : [];
  if (cancelledToolPreviews.length > 0) {
    sessionState.runEvents = [
      ...sessionState.runEvents,
      ...cancelledToolPreviews,
    ];
  }
  sessionState.streamingRunEvents = sessionState.streamingRunEvents.filter(
    (streamingEvent) =>
      TERMINAL_VISIBLE_DRAFT_EVENT_TYPES.has(streamingEvent.type),
  );
  // The terminal lifecycle summary can arrive over WebSocket before the
  // canonical Assistant output reaches this client over SSE. Keep the
  // compressed text deltas as the visible fallback until stable output,
  // History, or the next Run replaces them. A cancelled Run promotes Tool
  // previews into the stable projection: sibling calls can be persisted before
  // the execution limit has dispatched each one, so dropping those previews
  // makes known calls vanish until History is reloaded. Promotion also keeps
  // them visible if a queued follow-up Run starts before that reconciliation.
  if (type === 'run_failed') {
    sessionState.error = event?.payload?.error ?? 'Run failed';
  }
  if (type === 'run_completed' && event?.payload?.usage) {
    updateSessionUsage(sessionState, event.payload.usage);
  }
  if (event?.payload?.session_usage) {
    sessionState.sessionUsage = event.payload.session_usage;
  }
  const completionAlreadyRead =
    completedRunId &&
    sessionState.latestCompletionRunId === completedRunId &&
    !sessionState.hasUnreadCompletion;
  if (contributesToAgentActivity && !completionAlreadyRead) {
    sessionState.hasUnreadCompletion = true;
    sessionState.latestCompletionRunId = completedRunId;
    sessionState.unreadRunId = completedRunId;
    sessionState.unreadRunStatus = status ?? terminalStatus(type);
    sessionState.unreadRunAt = event?.timestamp ?? '';
  }
  sessionState.lastActiveAt = event?.timestamp ?? sessionState.lastActiveAt;
  return sessionState;
}

function normalizeRunEvent(event) {
  if (!event || typeof event !== 'object') {
    return null;
  }
  if (event.data && typeof event.data === 'object') {
    return normalizeRunEvent(event.data);
  }
  if (!event.type) {
    return null;
  }
  return {
    sequence: event.sequence,
    run_id: event.run_id,
    agent_id: event.agent_id,
    session_id: event.session_id,
    ...(event.contributes_to_agent_activity === false
      ? { contributes_to_agent_activity: false }
      : {}),
    type: event.type,
    payload: event.payload ?? {},
    timestamp: event.timestamp,
  };
}

function terminalStatus(eventType) {
  if (eventType === 'run_failed') {
    return CHAT_STATUS_FAILED;
  }
  if (eventType === 'run_cancelled') {
    return CHAT_STATUS_CANCELLED;
  }
  if (eventType === 'run_interrupted') {
    return CHAT_STATUS_INTERRUPTED;
  }
  return CHAT_STATUS_COMPLETED;
}
