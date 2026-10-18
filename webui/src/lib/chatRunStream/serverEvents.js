import { isRunServerEvent } from '../api.js';
import { formatAgentAddress } from '../agentAddress.js';
import { isReflectionRunKind } from '../chatTimelinePresentation.js';

// The Run event a WebSocket Run server event mirrors, addressed like Session
// state; null for an event that names no Run event, Run or sequence.
export function runEventFromServerEvent(serverEvent) {
  const payload = serverEvent?.payload ?? {};
  const runEventType = payload.run_event_type;
  if (
    !isRunServerEvent(serverEvent?.type) ||
    !runEventType ||
    !payload.run_id ||
    (payload.run_event_sequence !== 0 && !payload.run_event_sequence)
  ) {
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
  if (payload.change_stats) {
    runPayload.change_stats = payload.change_stats;
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
