import { formatAgentAddress } from '../agentAddress.js';
import { isReflectionRunKind } from '../chatTimelinePresentation.js';

const RUN_SERVER_EVENT_TYPES = new Set([
  'run_started',
  'run_output',
  'run_completed',
  'run_cancelled',
  'run_failed',
  'run_interrupted',
]);

export function normalizedRunServerEvents(singleEvent, events) {
  const normalizedEvents = Array.isArray(events) ? events.filter(Boolean) : [];
  if (singleEvent) {
    normalizedEvents.push(singleEvent);
  }
  return normalizedEvents;
}

export function runEventFromServerEvent(serverEvent) {
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
    ...(isReflectionRunKind(payload.run_kind)
      ? { run_kind: payload.run_kind }
      : {}),
    ...(typeof payload.source_session_id === 'string' &&
    payload.source_session_id
      ? { source_session_id: payload.source_session_id }
      : {}),
    sequence: payload.run_event_sequence,
    timestamp: payload.run_event_timestamp,
    payload: runPayload,
  };
}

export function runServerEventKey(serverEvent) {
  const payload = serverEvent?.payload;
  if (
    !payload?.run_id ||
    (payload.run_event_sequence !== 0 && !payload.run_event_sequence)
  ) {
    return '';
  }
  return `${payload.run_id}:${payload.run_event_sequence}:${serverEvent.type}`;
}
