import { createRunActivityProjection } from './chatRunStream/activity.js';
import { runEventFromServerEvent } from './chatRunStream/serverEvents.js';
import { formatAgentAddress } from './agentAddress.js';
import { reconnectBackoffDelay } from './backoff.js';
import { applyTimelineEvent } from './chatTimeline.js';
import { t } from './i18n.js';
import {
  TERMINAL_RUN_EVENTS,
  appendRunEvent,
  applyRunControls,
  ensureSessionState,
  isRunActive,
  isReleasedRun,
  releaseFinishedRunEvents,
  resetStaleRun,
  startRun,
} from './chatState.js';
import { nextContextUsage } from './chatState/sessionState.js';

const HEARTBEAT_TIMEOUT_MS = 25_000;

// One projection transport per displayed Session. The server resolves History,
// replay, partial output and terminal Tool state; this owner only applies keyed
// updates and recovers the connection with an authoritative snapshot.
export function createChatRunStream({
  chatState,
  subscribeRunEvents,
  refreshSessionQueue,
  reconcileRunSession = async () => false,
  isDisplayedSession,
  updateSubAgentRunStatuses,
  onReflectionFinished = () => {},
  reportStreamDiagnostic = () => {},
}) {
  const activity = createRunActivityProjection({
    chatState,
    updateSubAgentRunStatuses,
    onReflectionFinished,
  });
  const connections = new Map();
  const reconciliations = new Map();
  let destroyed = false;

  function closeSubscriptionFor(key) {
    const connection = connections.get(key);
    if (!connection) return;
    connections.delete(key);
    clearTimeout(connection.watchdog);
    clearTimeout(connection.retry);
    connection.stream?.close();
  }

  function reconcile(session, runId) {
    const key = `${session.key}:${runId}`;
    if (reconciliations.has(key)) return reconciliations.get(key);
    const pending = Promise.resolve(reconcileRunSession(session, runId))
      .catch(() => false)
      .finally(() => reconciliations.delete(key));
    reconciliations.set(key, pending);
    return pending;
  }

  function subscribeToRun(session, url) {
    if (destroyed || !session?.currentRun?.runId) return;
    const runId = session.currentRun.runId;
    const timelineUrl = url?.endsWith('/timeline')
      ? url
      : timelineUrlForRun(runId);
    const existing = connections.get(session.key);
    if (existing?.runId === runId && existing.url === timelineUrl) return;
    closeSubscriptionFor(session.key);
    session.currentRun.timelineUrl = timelineUrl;
    const connection = {
      runId,
      url: timelineUrl,
      stream: null,
      watchdog: null,
      retry: null,
      attempt: 0,
      generation: 0,
    };
    connections.set(session.key, connection);
    const current = (generation) =>
      !destroyed &&
      connections.get(session.key) === connection &&
      connection.generation === generation;
    const arm = (generation) => {
      clearTimeout(connection.watchdog);
      connection.watchdog = setTimeout(
        () => recover(generation),
        HEARTBEAT_TIMEOUT_MS,
      );
    };
    const recover = (generation) => {
      if (!current(generation)) return;
      connection.generation += 1;
      connection.stream?.close();
      clearTimeout(connection.watchdog);
      session.streamError = t('errors.streamClosed');
      reportStreamDiagnostic({ reason: 'timeline_reconnect', runId });
      void reconcile(session, runId);
      connection.retry = setTimeout(
        attach,
        reconnectBackoffDelay(connection.attempt++, {
          initialDelayMs: 500,
          maxDelayMs: 5000,
        }),
      );
    };
    const attach = () => {
      if (connections.get(session.key) !== connection || destroyed) return;
      const generation = ++connection.generation;
      let gotSnapshot = false;
      arm(generation);
      try {
        connection.stream = subscribeRunEvents(
          timelineUrl,
          {
            onOpen: () => {
              if (current(generation)) arm(generation);
            },
            onHeartbeat: () => {
              if (!current(generation)) return;
              connection.attempt = 0;
              arm(generation);
            },
            onError: () => recover(generation),
            onEvent: ({ type, data }) => {
              if (!current(generation) || data?.run_id !== runId) return;
              connection.attempt = 0;
              arm(generation);
              const eventType = type ?? data.type;
              if (!gotSnapshot && eventType !== 'timeline_snapshot') {
                recover(generation);
                return;
              }
              gotSnapshot ||= eventType === 'timeline_snapshot';
              if (!applyTimelineEvent(session, eventType, data)) {
                if (data.sequence > (session.timelineSequences?.[runId] ?? -1))
                  recover(generation);
                return;
              }
              session.streamError = '';
              applyState(session, data);
              if (data.terminal) {
                closeSubscriptionFor(session.key);
                void refreshSessionQueue(session);
                void reconcile(session, runId);
              }
            },
          },
          {
            eventTypes: ['timeline_snapshot', 'timeline_update'],
            closeOnTerminal: false,
          },
        );
      } catch {
        recover(generation);
      }
    };
    attach();
  }

  function applyState(session, data) {
    const state = data.state ?? {};
    const current = session.currentRun;
    if (current?.runId !== data.run_id) return;
    current.status = state.status ?? current.status;
    current.startedAt = state.started_at ?? current.startedAt;
    current.iterationCount = state.iteration_count ?? current.iterationCount;
    session.status = current.status;
    applyRunControls(session, { run_id: data.run_id, ...state });
    if (Object.hasOwn(state, 'context_usage'))
      session.contextUsage = nextContextUsage(
        session.contextUsage,
        state.context_usage,
      );
    if (state.session_usage) session.sessionUsage = state.session_usage;
    if (state.usage) session.usage = state.usage;
    if (data.terminal)
      appendRunEvent(session, {
        type: `run_${state.status ?? current.status}`,
        run_id: data.run_id,
        sequence: data.sequence,
        timestamp:
          state.completed_at ??
          data.run?.endTimestamp ??
          data.items?.find((item) => item.type === 'assistant_run')
            ?.endTimestamp,
        ...(current.contributesToAgentActivity === false
          ? { contributes_to_agent_activity: false }
          : {}),
        payload: state,
      });
  }

  function attachRunStream(session, run) {
    if (!session || !run?.run_id) return false;
    if (session.currentRun?.runId !== run.run_id) startRun(session, run);
    else {
      if (session.currentRun.status === 'running')
        session.currentRun.status = run.status ?? 'running';
      session.currentRun.startedAt ??= run.started_at ?? null;
      applyRunControls(session, run);
    }
    subscribeToRun(session, run.timeline_url);
    return true;
  }

  function mergeRunResponse(session, run) {
    if (!session || session.currentRun?.runId !== run?.run_id) return false;
    for (const event of run.events ?? []) {
      appendRunEvent(session, event);
      trackActivity(event);
    }
    applyRunControls(session, run);
    return true;
  }

  function trackActivity(event) {
    activity.trackReflectionTask(event);
    if (
      event.contributes_to_agent_activity !== false ||
      event.type === 'subagent_status_changed'
    )
      activity.trackSubAgentRunStatus(event);
  }

  function handleServerEvent(serverEvent) {
    const event = runEventFromServerEvent(serverEvent);
    if (!event?.agent_id || !event?.session_id) return;
    trackActivity(event);
    const session = ensureSessionState(
      chatState,
      event.agent_id,
      event.session_id,
    );
    if (
      session.historyRuns?.[event.run_id]?.complete === true ||
      isReleasedRun(session, event.run_id)
    )
      return;
    appendRunEvent(session, event);
    const displayed = isDisplayedSession(event.agent_id, event.session_id);
    if (
      event.type === 'run_started' &&
      displayed &&
      session.currentRun?.runId === event.run_id
    ) {
      attachRunStream(session, { run_id: event.run_id, status: 'running' });
    }
    if (!TERMINAL_RUN_EVENTS.has(event.type)) return;
    void refreshSessionQueue(session);
    // A WebSocket terminal may overtake the final projection batch. Never
    // close that transport until its own terminal snapshot/update arrives.
    if (displayed || session.historyLoaded) {
      void reconcile(session, event.run_id).then((settled) => {
        if (
          settled &&
          session.historyRuns?.[event.run_id]?.complete &&
          connections.get(session.key)?.runId === event.run_id
        )
          closeSubscriptionFor(session.key);
      });
    } else releaseFinishedRunEvents(session, event.run_id);
  }

  function applyConnectionSnapshot(snapshot) {
    if (!Array.isArray(snapshot?.active_runs)) return;
    const activeIds = new Set(snapshot.active_runs.map((run) => run.run_id));
    for (const session of Object.values(chatState.sessions)) {
      if (!isRunActive(session) || activeIds.has(session.currentRun?.runId))
        continue;
      closeSubscriptionFor(session.key);
      if (isDisplayedSession(session.agentId, session.sessionId)) {
        session.streamError = t('errors.streamClosed');
        void reconcile(session, session.currentRun.runId);
      } else resetStaleRun(session);
    }
    activity.applySnapshot(snapshot.active_runs);
    for (const run of snapshot.active_runs) {
      if (!run?.run_id || !run.agent_id || !run.session_id) continue;
      const address = formatAgentAddress(run.agent_id, run.project_id);
      const session = ensureSessionState(chatState, address, run.session_id);
      if (session.currentRun?.runId !== run.run_id) startRun(session, run);
      applyRunControls(session, run);
      if (isDisplayedSession(address, run.session_id))
        attachRunStream(session, run);
    }
  }

  return {
    applyConnectionSnapshot,
    attachRunStream,
    closeSubscriptionFor,
    closeSubscriptionsExcept(key) {
      for (const other of connections.keys())
        if (other !== key) closeSubscriptionFor(other);
    },
    closeSubscriptions() {
      destroyed = true;
      for (const key of connections.keys()) closeSubscriptionFor(key);
    },
    handleServerEvent,
    mergeRunResponse,
    subscribeToRun,
  };
}

function timelineUrlForRun(runId) {
  return `/api/runs/${encodeURIComponent(String(runId))}/timeline`;
}
