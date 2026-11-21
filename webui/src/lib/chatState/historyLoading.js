import { t } from '../i18n.js';
import { mergeTimelineItems, mergeRunChildPage } from '../chatTimeline.js';
import {
  ensureSessionState,
  supersedeRenamedSessionKey,
  isRecord,
  isRunActive,
  resetStaleRun,
} from './sessionState.js';
import {
  loadHistory,
  hasRetainedTerminalRunProjection,
  attachableHistoryRun,
  prependHistory,
} from './history.js';

const HISTORY_INITIAL_LIMIT = 100;

const HISTORY_OLDER_LIMIT = 50;

// `chat.history` omits what an unchanged read did not recompute; an absent
// field keeps the Session's current value. `context_usage` is present (possibly
// null) whenever it was read.
function historyLoadOptions(history) {
  return {
    agentName: history?.agent_name,
    sessionModel: history?.session_model,
    items: history?.items,
    replaceRunIds: history?.replace_run_ids,
    timelineSequence: history?.timeline_sequence,
    hasMore: history?.has_more === true,
    nextBefore: history?.next_before,
    nextAfter: history?.next_after,
    generation: history?.history_generation,
    runs: history?.runs,
    incremental: history?.incremental,
    reset: history?.history_reset,
    activeRunId: history?.active_run?.run_id,
    sessionUsage: history?.session_usage,
    ...(isRecord(history) && Object.hasOwn(history, 'last_usage')
      ? { lastUsage: history.last_usage }
      : {}),
    ...(isRecord(history) && Object.hasOwn(history, 'output_speed')
      ? { output_speed: history.output_speed }
      : {}),
    ...(isRecord(history) && Object.hasOwn(history, 'output_latency')
      ? { output_latency: history.output_latency }
      : {}),
    ...(isRecord(history) && Object.hasOwn(history, 'context_usage')
      ? { contextUsage: history.context_usage }
      : {}),
    compactionPolicy: history?.compaction_policy,
    backgroundCommandStatuses: history?.background_command_statuses,
    backgroundCommandsStillRunning: history?.background_commands_still_running,
    backgroundCommandProcesses: history?.background_command_processes,
    subAgentBackgroundCommands: history?.subagent_background_commands,
  };
}

// Each page reports the Runs of its own messages; a later record wins.
function mergeHistoryRuns(earlier, later) {
  const runs = new Map();
  for (const run of [...(earlier ?? []), ...(later ?? [])]) {
    runs.set(run.run_id, run);
  }
  return [...runs.values()];
}

// Background status deltas fold in order; a later status wins.
function mergeBackgroundStatuses(earlier, later) {
  if (!isRecord(later)) {
    return earlier;
  }
  return { ...(isRecord(earlier) ? earlier : {}), ...later };
}

// History requests, paging and retention share one versioned Session snapshot.
export function createChatHistory({
  chatState,
  runStream,
  operations,
  isDisplayedSession,
  reflections,
  syncSessionQueue,
  errorMessage,
}) {
  const historyLoadVersions = new Map();
  const historyCache = new Map();
  const runHistoryLoads = new Map();
  const olderHistoryLoads = new Map();
  let displayedHistoryRequest = null;
  let preparedHistory = null;

  // Unchanged immutable records survive page loads, so charge them at their
  // last measured size without serializing the entire transcript again.
  const recordSizes = new WeakMap();
  function arrayBytes(records) {
    let characters = 2 + Math.max(0, records.length - 1);
    for (const record of records) {
      let size = recordSizes.get(record);
      if (size === undefined) {
        size = JSON.stringify(record).length;
        recordSizes.set(record, size);
      }
      characters += size;
    }
    return 2 * characters;
  }

  function retainHistory(session) {
    historyCache.delete(session);
    historyCache.set(
      session,
      6 + arrayBytes(session.messages) + arrayBytes(session.timelineItems),
    );
    let bytes = [...historyCache.values()].reduce((sum, size) => sum + size, 0);
    for (const [cached, size] of historyCache) {
      if (historyCache.size <= 8 && bytes <= 16 * 1024 * 1024) break;
      if (
        cached === session ||
        isDisplayedSession(cached.agentId, cached.sessionId)
      )
        continue;
      runStream.closeSubscriptionFor(cached.key);
      historyLoadVersions.set(
        cached.key,
        (historyLoadVersions.get(cached.key) ?? 0) + 1,
      );
      cached.messages = [];
      cached.timelineItems = [];
      cached.historyRuns = {};
      cached.historyLoaded = false;
      cached.historyAfter = '';
      cached.historyBefore = '';
      historyCache.delete(cached);
      bytes -= size;
    }
  }
  function syncDisplayedHistoryLoad() {
    if (displayedHistoryRequest && !displayedHistoryRequest.isDisplayed()) {
      displayedHistoryRequest = null;
      chatState.loadingHistory = false;
      chatState.historyError = '';
    }
  }

  function beginHistoryRequest(sessionState) {
    const version = (historyLoadVersions.get(sessionState.key) ?? 0) + 1;
    historyLoadVersions.set(sessionState.key, version);
    const snapshotVersion = sessionState.historySnapshotVersion;
    const compactionPolicy = sessionState.compactionPolicy;
    const usage = sessionState.usage;
    const sessionUsage = sessionState.sessionUsage;
    const contextUsage = sessionState.contextUsage;
    const outputSpeed = sessionState.outputSpeed;
    const outputLatency = sessionState.outputLatency;
    return {
      snapshotVersion,
      loadOptions(history) {
        const options = historyLoadOptions(history);
        // A settings read or local save accepted while History was in flight
        // supersedes its policy snapshot without discarding its new messages.
        if (sessionState.compactionPolicy !== compactionPolicy) {
          delete options.compactionPolicy;
        }
        // Output from the live Run may have advanced since the read began.
        if (sessionState.usage !== usage) options.preserveUsage = true;
        if (sessionState.sessionUsage !== sessionUsage)
          options.preserveSessionUsage = true;
        if (sessionState.contextUsage !== contextUsage)
          options.preserveContextUsage = true;
        if (sessionState.outputSpeed !== outputSpeed)
          options.preserveOutputSpeed = true;
        if (sessionState.outputLatency !== outputLatency)
          options.preserveOutputLatency = true;
        return options;
      },
      isLatest: () => historyLoadVersions.get(sessionState.key) === version,
      isDisplayed: () =>
        isDisplayedSession(sessionState.agentId, sessionState.sessionId),
    };
  }

  async function readCurrentHistory(sessionState) {
    let after = sessionState.historyAfter;
    let result = null;
    do {
      const page = await operations.loadChatHistory({
        agent_id: sessionState.agentId,
        session_id: sessionState.sessionId,
        limit: after ? 500 : HISTORY_INITIAL_LIMIT,
        child_limit: 100,
        ...(after ? { after } : {}),
      });
      if (!result || !page.incremental) result = page;
      else
        result = {
          ...page,
          session_model: Object.hasOwn(page, 'session_model')
            ? page.session_model
            : result.session_model,
          agent_name: Object.hasOwn(page, 'agent_name')
            ? page.agent_name
            : result.agent_name,
          incremental: result.incremental,
          has_more: result.has_more,
          next_before: result.next_before,
          history_reset: result.history_reset,
          messages: [...(result.messages ?? []), ...(page.messages ?? [])],
          items: mergeTimelineItems(result.items, page.items),
          replace_run_ids: [
            ...new Set([
              ...(result.replace_run_ids ?? []),
              ...(page.replace_run_ids ?? []),
            ]),
          ],
          runs: mergeHistoryRuns(result.runs, page.runs),
          background_command_statuses: mergeBackgroundStatuses(
            result.background_command_statuses,
            page.background_command_statuses,
          ),
          // Each list is complete; the newest one wins.
          background_commands_still_running:
            page.background_commands_still_running ??
            result.background_commands_still_running,
          background_command_processes:
            page.background_command_processes ??
            result.background_command_processes,
          subagent_background_commands:
            page.subagent_background_commands ??
            result.subagent_background_commands,
          ...(Object.hasOwn(page, 'last_usage')
            ? { last_usage: page.last_usage }
            : Object.hasOwn(result, 'last_usage')
              ? { last_usage: result.last_usage }
              : {}),
          ...Object.fromEntries(
            ['output_speed', 'output_latency'].flatMap((key) =>
              Object.hasOwn(page, key)
                ? [[key, page[key]]]
                : Object.hasOwn(result, key)
                  ? [[key, result[key]]]
                  : [],
            ),
          ),
        };
      after = page.next_after;
    } while (result?.has_newer);
    return result;
  }

  function startHistoryRead(sessionState) {
    const request = beginHistoryRequest(sessionState);
    // Only a read without `after` carries reflection Runs.
    const reflectionRequest = sessionState.historyAfter
      ? null
      : reflections.beginRequest(sessionState);
    return {
      sessionState,
      request,
      reflectionRequest,
      runId: sessionState.currentRun?.runId ?? '',
      result: readCurrentHistory(sessionState),
    };
  }

  // The URL already names the exact Session. Read its first page while the
  // shell resolves the selectable owner. Only the eventual navigation can
  // display the result, surface its errors, or attach a Run stream.
  function prepareLinkedHistory(link) {
    if (
      link &&
      preparedHistory?.sessionState.agentId === link.agentId &&
      preparedHistory.sessionState.sessionId === link.sessionId
    )
      return;
    preparedHistory = null;
    if (!link?.preload) return;
    const session = ensureSessionState(chatState, link.agentId, link.sessionId);
    if (session.historyLoaded || historyLoadVersions.has(session.key)) return;
    preparedHistory = startHistoryRead(session);
    // The destination may be abandoned before the promise is consumed.
    preparedHistory.result.catch(() => {});
  }

  // A change to the destination before its owner has been resolved makes the
  // earlier page obsolete. The eventual navigation reads it again.
  function invalidatePreparedHistory(target) {
    if (!preparedHistory) return;
    const { agentId, sessionId } = preparedHistory.sessionState;
    if (
      !target ||
      target.all ||
      (target.agentAddress === agentId && target.sessionId === sessionId)
    ) {
      preparedHistory = null;
    }
  }

  async function loadHistoryForSession(agentId, sessionId) {
    const sessionState = ensureSessionState(chatState, agentId, sessionId);
    let read;
    if (preparedHistory?.sessionState === sessionState) {
      if (
        preparedHistory.request.isLatest() &&
        sessionState.historySnapshotVersion ===
          preparedHistory.request.snapshotVersion
      ) {
        read = preparedHistory;
      }
      preparedHistory = null;
    }
    read ??= startHistoryRead(sessionState);
    const { request, reflectionRequest } = read;
    const isLatestRequest = request.isLatest;
    const isDisplayed = () => isDisplayedSession(agentId, sessionId);
    const startedDisplayed = isDisplayed();
    if (startedDisplayed) {
      displayedHistoryRequest = request;
      chatState.loadingHistory = true;
      chatState.historyError = '';
      runStream.closeSubscriptionsExcept(sessionState.key);
    }
    // The Queue owns its freshness and errors. Reading it must not hold the composer.
    void syncSessionQueue(sessionState);
    try {
      let history;
      let staleRunId = read.runId;
      let result = read.result;
      do {
        if (!result) {
          staleRunId = sessionState.currentRun?.runId ?? '';
          result = readCurrentHistory(sessionState);
        }
        history = await result;
        result = null;
        if (
          !isLatestRequest() ||
          sessionState.historySnapshotVersion !== request.snapshotVersion
        ) {
          return false;
        }
        // Run admission can overtake a History response, including while the
        // composer is disabled. Read again rather than reattach the old Run
        // or install the lineage from before an accepted edit.
      } while (
        sessionState.currentRun?.runId &&
        sessionState.currentRun.runId !== staleRunId &&
        history?.active_run?.run_id !== sessionState.currentRun.runId
      );
      loadHistory(
        sessionState,
        history?.messages ?? [],
        request.loadOptions(history),
      );
      retainHistory(sessionState);
      reflectionRequest?.apply(history?.reflection_runs);
      sessionState.markReadFailedRunId = '';
      if (
        !history?.active_run &&
        isRunActive(sessionState) &&
        sessionState.currentRun?.runId === staleRunId
      ) {
        resetStaleRun(sessionState);
        runStream.closeSubscriptionFor(sessionState.key);
      }
      // A loaded result is acknowledged by the Chat view, which knows whether
      // the user can see it.
      if (isDisplayed()) {
        runStream.attachRunStream(
          sessionState,
          attachableHistoryRun(sessionState, history?.active_run),
        );
      }
      return true;
    } catch (error) {
      if (isLatestRequest() && isDisplayed()) {
        displayedHistoryRequest = request;
        chatState.historyError = errorMessage(error);
      }
      return false;
    } finally {
      if (displayedHistoryRequest === request) {
        chatState.loadingHistory = false;
        // A visible error belongs to this Session even after loading ends.
        if (!chatState.historyError) displayedHistoryRequest = null;
      }
    }
  }

  async function reconcileRunSession(sessionState, expectedRunId) {
    if (!sessionState?.agentId || !sessionState?.sessionId || !expectedRunId) {
      return false;
    }

    const request = beginHistoryRequest(sessionState);
    const reflectionRequest = sessionState.historyAfter
      ? null
      : reflections.beginRequest(sessionState);
    const isLatestRequest = request.isLatest;
    try {
      const history = await readCurrentHistory(sessionState);
      // History may settle an older Run while its successor owns the stream.
      // Keep the current Run's projection and subscription in that case.
      if (
        !isLatestRequest() ||
        sessionState.historySnapshotVersion !== request.snapshotVersion
      ) {
        // Keeping the same Run id does not prove the newer read succeeded:
        // it may have failed while this recovery's stream was already closed.
        // The stream owner can independently recognize a restored subscription.
        return sessionState.historyRuns?.[expectedRunId]?.complete === true;
      }

      const successor = sessionState.currentRun?.runId !== expectedRunId;
      const currentRunId = sessionState.currentRun?.runId;

      loadHistory(sessionState, history?.messages ?? [], {
        ...request.loadOptions(history),
        ...(successor ? { activeRunId: currentRunId, reset: false } : {}),
      });
      retainHistory(sessionState);
      reflectionRequest?.apply(history?.reflection_runs);
      sessionState.markReadFailedRunId = '';
      if (successor) {
        await syncSessionQueue(sessionState);
        return sessionState.historyRuns?.[expectedRunId]?.complete === true;
      }
      const activeRun = attachableHistoryRun(sessionState, history?.active_run);
      if (activeRun) {
        if (isDisplayedSession(sessionState.agentId, sessionState.sessionId)) {
          runStream.attachRunStream(sessionState, activeRun);
        }
      } else if (
        !history?.active_run &&
        !hasRetainedTerminalRunProjection(sessionState)
      ) {
        resetStaleRun(sessionState);
        runStream.closeSubscriptionFor(sessionState.key);
      }
      await syncSessionQueue(sessionState);
      return true;
    } catch {
      // Recovery is deliberately silent and retried by chatRunStream. A
      // transient history failure must not replace a real Run failure or the
      // user's current global history error.
      return false;
    }
  }

  function loadOlderHistory(sessionState, limit = HISTORY_OLDER_LIMIT) {
    if (!sessionState) return Promise.resolve(false);
    if (!olderHistoryLoads.has(sessionState.key)) {
      const key = sessionState.key;
      olderHistoryLoads.set(
        key,
        readOlderHistory(sessionState, limit).finally(() =>
          olderHistoryLoads.delete(key),
        ),
      );
    }
    return olderHistoryLoads.get(sessionState.key);
  }

  async function readOlderHistory(sessionState, limit) {
    if (
      !sessionState?.agentId ||
      !sessionState.hasOlderHistory ||
      sessionState.loadingOlderHistory ||
      (!sessionState.historyBefore && sessionState.messages.length === 0)
    ) {
      return false;
    }
    const pageBefore = () =>
      sessionState.historyBefore ||
      (sessionState.messages ?? []).find(
        (message) => typeof message?.id === 'string' && message.id.length > 0,
      )?.id ||
      '';
    if (!pageBefore()) {
      sessionState.hasOlderHistory = false;
      return false;
    }
    sessionState.loadingOlderHistory = true;
    sessionState.actionError = '';
    const editVersion = sessionState.historyEditVersion;
    try {
      while (sessionState.hasOlderHistory && pageBefore()) {
        const before = pageBefore();
        const generation = sessionState.historyGeneration;
        const isCurrentBoundary = () =>
          pageBefore() === before &&
          sessionState.historyGeneration === generation;
        let history;
        try {
          history = await operations.loadChatHistory({
            agent_id: sessionState.agentId,
            session_id: sessionState.sessionId,
            limit,
            child_limit: 100,
            before,
          });
        } catch (error) {
          if (sessionState.historyEditVersion !== editVersion) return false;
          if (!isCurrentBoundary()) continue;
          sessionState.actionError = `${t('chat.historyOlderLoadError')} ${errorMessage(error)}`;
          return false;
        }
        if (sessionState.historyEditVersion !== editVersion) return false;
        // Appends leave this boundary valid. A replacement that moved it
        // needs a fresh page, without waiting for another scroll gesture.
        if (!isCurrentBoundary()) continue;
        prependHistory(sessionState, history?.messages ?? [], {
          items: history?.items,
          replaceRunIds: history?.replace_run_ids,
          runs: history?.runs,
          hasMore: history?.has_more === true,
          nextBefore: history?.next_before,
        });
        retainHistory(sessionState);
        return true;
      }
      return false;
    } finally {
      sessionState.loadingOlderHistory = false;
    }
  }

  // Loads the steps before one Run's history gap. Concurrent requests for the
  // same gap share one read; different Runs load independently.
  function loadRunHistory(sessionState, gap) {
    if (!sessionState) return Promise.resolve(false);
    const key = `${sessionState.key}\n${gap.run_id}\n${gap.before}`;
    if (!runHistoryLoads.has(key)) {
      runHistoryLoads.set(
        key,
        readRunHistory(sessionState, gap).finally(() =>
          runHistoryLoads.delete(key),
        ),
      );
    }
    return runHistoryLoads.get(key);
  }

  async function readRunHistory(sessionState, gap) {
    const generation = sessionState.historyGeneration;
    try {
      const page = await operations.loadTimelineChildren({
        agent_id: sessionState.agentId,
        session_id: sessionState.sessionId,
        run_id: gap.run_id,
        before: gap.before,
        history_generation: generation,
        limit: 100,
      });
      // Newer snapshots may have arrived meanwhile; the page still applies
      // while the Run shows the same gap.
      const merged = mergeRunChildPage(sessionState, page);
      if (merged) retainHistory(sessionState);
      return merged;
    } catch (error) {
      if (sessionState.historyGeneration === generation)
        sessionState.actionError = errorMessage(error);
      return false;
    }
  }

  return {
    prepareLinkedHistory,
    invalidatePreparedHistory,
    loadHistoryForSession,
    reconcileRunSession,
    loadOlderHistory,
    loadRunHistory,
    syncDisplayedHistoryLoad,
    renameSession(oldKey, newKey) {
      supersedeRenamedSessionKey(historyLoadVersions, oldKey, newKey);
    },
    destroy() {
      preparedHistory = null;
      historyLoadVersions.clear();
      displayedHistoryRequest = null;
      chatState.loadingHistory = false;
    },
  };
}
