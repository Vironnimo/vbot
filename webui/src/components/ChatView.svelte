<script>
  import { onDestroy, onMount } from 'svelte';
  import { SvelteSet } from 'svelte/reactivity';

  import {
    cancelRun,
    cancelToolCall,
    listQueue,
    removeFromQueue,
    rpc,
    subscribeRunEvents,
    updateQueueItem,
  } from '$lib/api.js';
  import {
    mergeBoundedEntries,
    subAgentGuardKeysForEvictedStatuses,
  } from '$lib/clientCaches.js';
  import { t } from '$lib/i18n.js';
  import {
    subAgentResultData,
    subAgentResultEntryAllowsFetch,
    subAgentResultTextFromMessages,
  } from '$lib/chatTimelinePresentation.js';

  import { createChatRunStream } from '../lib/chatRunStream.js';
  import {
    addServerQueuedMessage,
    canCreateNewSession,
    createChatState,
    currentSessionState,
    ensureSessionState,
    isRunActive,
    loadHistory,
    markSessionError,
    prependHistory,
    removeQueuedMessage,
    resetStaleRun,
    selectAgent,
    selectedAgent,
    setAgents,
    startRun,
    syncQueueFromServer,
    updateQueuedMessageContent,
  } from '../lib/chatState.js';
  import ChatHeader from './chat/ChatHeader.svelte';
  import ChatComposer from './ChatComposer.svelte';
  import SessionListDrawer from './SessionListDrawer.svelte';
  import ChatTimeline from './ChatTimeline.svelte';
  import QueuedMessages from './QueuedMessages.svelte';

  let {
    sharedAgents = [],
    sharedSelectedAgentId = '',
    // Chat reading-column width preference: 'comfortable' | 'wide' | 'full'.
    // Phase 3 seeds the persisted value from App; the default keeps the chat
    // self-contained (centered, capped at the comfortable measure).
    chatWidth = 'comfortable',
    agentsRefreshToken = 0,
    onAgentsChanged,
    onAgentSelected,
    navigateToSubAgent = () => {},
    pendingSessionNavigation = null,
    onSessionNavigation = () => {},
    runServerEvent = null,
    runServerEvents = [],
    connectionSnapshot = null,
    wakewordStatus = { enabled: false, state: 'off' },
    desktopCapabilities = null,
    onNavigateToVoiceSettings = () => {},
  } = $props();

  const chatState = $state(createChatState());
  let loadingHistory = $state(false);
  let creatingSession = $state(false);
  let cancellingRun = $state(false);
  let historyError = $state('');
  let actionError = $state('');
  let actionInfo = $state('');
  let availableSkills = $state([]);
  let showSessionDrawer = $state(false);
  let viewingSessionId = $state('');
  let viewingSessionAgentId = $state('');
  let viewingSubAgentSession = $state(false);
  let submittedTurnScrollKey = $state(0);
  let submittedTurnScrollRunId = $state('');
  let subAgentRunStatuses = $state({});
  let subAgentResults = $state({});
  let handledSessionNavigationKey = '';
  let handledConnectionSnapshot = null;
  const ACTION_INFO_TIMEOUT_MS = 4000;
  const HISTORY_INITIAL_LIMIT = 100;
  const HISTORY_OLDER_LIMIT = 50;
  const SUBAGENT_RESULT_HISTORY_LIMIT = 20;
  // Both caches grow per run/spawn for the lifetime of the tab (handoff3
  // B10), so they are LRU-capped. Statuses are tiny strings — a generous cap
  // keeps every plausibly rendered row covered (~7 entries per run). Results
  // hold full child outputs, so the cap is much tighter; an evicted entry of
  // a still-rendered row simply refetches (missing entries allow fetch).
  const SUBAGENT_STATUS_CACHE_LIMIT = 2000;
  const SUBAGENT_RESULT_CACHE_LIMIT = 100;
  let actionInfoTimeoutId = null;

  let activeAgent = $derived(getActiveAgent());
  let activeSessionState = $derived(getActiveSessionState());
  let subAgentSessionActive = $derived(
    Boolean(viewingSessionId) && viewingSubAgentSession,
  );
  // Any local override away from the selected agent's current session — also
  // true for same-agent drawer selections, which must offer a return path too.
  let sessionOverrideActive = $derived(Boolean(viewingSessionId));
  let newSessionBlocked = $derived(!canCreateNewSession(activeSessionState));
  let composerDisabled = $derived(!activeAgent || loadingHistory);
  let lastSharedSelectedAgentId = '';
  let lastSharedAgents = null;
  let lastAgentsRefreshToken = null;

  function getActiveAgent() {
    if (viewingSessionAgentId) {
      return agentById(viewingSessionAgentId);
    }
    return selectedAgent(chatState);
  }

  function agentById(agentId) {
    return chatState.agents.find((agent) => agent.id === agentId) ?? null;
  }

  function getActiveSessionState() {
    const agent = getActiveAgent();
    if (agent && viewingSessionId) {
      return chatState.sessions[`${agent.id}::${viewingSessionId}`] ?? null;
    }
    return currentSessionState(chatState);
  }

  function displayedSessionKey() {
    const agent = getActiveAgent();
    const sessionId = viewingSessionId || agent?.current_session_id;
    return agent?.id && sessionId ? `${agent.id}::${sessionId}` : '';
  }

  function isDisplayedSession(agentId, sessionId) {
    return displayedSessionKey() === `${agentId}::${sessionId}`;
  }

  $effect(() => {
    if (sharedAgents.length > 0 && sharedAgents !== lastSharedAgents) {
      lastSharedAgents = sharedAgents;
      setAgents(chatState, sharedAgents);
    }
  });

  $effect(() => {
    if (
      sharedSelectedAgentId &&
      sharedSelectedAgentId !== lastSharedSelectedAgentId &&
      sharedSelectedAgentId !== chatState.selectedAgentId &&
      chatState.agents.some((agent) => agent.id === sharedSelectedAgentId)
    ) {
      lastSharedSelectedAgentId = sharedSelectedAgentId;
      handleSelectAgent(sharedSelectedAgentId);
    }
  });

  // App-driven session navigation: sub-agent link clicks routed through
  // `navigateToSubAgent` and browser-history restores. Both arrive here so
  // they never echo back through `onSessionNavigation` as a new history push.
  $effect(() => {
    const navigation = pendingSessionNavigation;
    const requestId = navigation?.requestId ?? '';
    const navigationKey = !navigation
      ? ''
      : navigation.returnToCurrent
        ? `::return::${requestId}`
        : navigation.agentId && navigation.sessionId
          ? `${navigation.agentId}::${navigation.sessionId}::${navigation.subAgent === true}::${requestId}`
          : '';
    if (!navigationKey || navigationKey === handledSessionNavigationKey) {
      return;
    }

    handledSessionNavigationKey = navigationKey;
    applySessionNavigation(navigation);
  });

  // Apply each distinct `/ws` `connection_ready` hello frame to the run stream
  // exactly once. The frame is the durable source of truth for active runs and
  // sub-agent statuses (see `chatRunStream.applyConnectionSnapshot`); the
  // local `handledConnectionSnapshot` reference is the dedup guard so a re-run
  // of this effect for the same snapshot object cannot re-trigger side effects
  // (same pattern as `pendingSessionNavigation` above).
  $effect(() => {
    if (
      !connectionSnapshot ||
      connectionSnapshot === handledConnectionSnapshot
    ) {
      return;
    }

    handledConnectionSnapshot = connectionSnapshot;
    runStream.applyConnectionSnapshot(connectionSnapshot);
  });

  $effect(() => {
    if (lastAgentsRefreshToken === null) {
      lastAgentsRefreshToken = agentsRefreshToken;
      return;
    }
    if (agentsRefreshToken !== lastAgentsRefreshToken) {
      lastAgentsRefreshToken = agentsRefreshToken;
      loadAgents({ preferredAgentId: sharedSelectedAgentId });
    }
  });

  $effect(() => {
    runStream.handleServerEvents(runServerEvent, runServerEvents);
  });

  onMount(() => {
    loadAgents({ preferredAgentId: sharedSelectedAgentId });
    loadCommands();
    return () => runStream.closeSubscriptions();
  });

  onDestroy(() => {
    if (actionInfoTimeoutId !== null) {
      clearTimeout(actionInfoTimeoutId);
      actionInfoTimeoutId = null;
    }
  });

  const setActionInfo = (message) => {
    if (actionInfoTimeoutId !== null) {
      clearTimeout(actionInfoTimeoutId);
      actionInfoTimeoutId = null;
    }

    actionInfo = typeof message === 'string' ? message : '';

    if (!actionInfo) {
      return;
    }

    actionInfoTimeoutId = setTimeout(() => {
      actionInfo = '';
      actionInfoTimeoutId = null;
    }, ACTION_INFO_TIMEOUT_MS);
  };

  const normalizedBuiltInCommandName = (value) => {
    if (typeof value !== 'string') {
      return '';
    }

    return value.trim().replace(/^\/+/, '').toLowerCase();
  };

  const isCompactCommand = (content) => {
    if (typeof content !== 'string') {
      return false;
    }

    const trimmed = content.trim();
    if (!trimmed.startsWith('/')) {
      return false;
    }

    return normalizedBuiltInCommandName(trimmed) === 'compact';
  };

  // Extract a session-switch target from a handled command response.
  // `/new` returns `{ data: { command: "new", session_id } }` and stays on the
  // current agent. `/handoff` returns `{ data: { command: "handoff", session_id,
  // agent_id } }` and may target a different agent — that is the cross-agent
  // switch path. Returns `{ sessionId, agentId }` when the response names a new
  // session, otherwise null.
  const commandSwitchFromResponse = (response) => {
    const data = response?.data;
    if (!data || typeof data.session_id !== 'string') {
      return null;
    }
    const sessionId = data.session_id.trim();
    if (!sessionId) {
      return null;
    }
    if (data.command === 'new' || data.command === 'handoff') {
      const targetAgentId =
        typeof data.agent_id === 'string' ? data.agent_id.trim() : '';
      return { sessionId, targetAgentId };
    }
    return null;
  };

  const loadCommands = async () => {
    try {
      const result = await rpc('chat.commands');
      const items = Array.isArray(result?.items) ? result.items : [];
      availableSkills = items
        .filter(
          (item) => typeof item?.name === 'string' && item.name.length > 0,
        )
        .map((item) => ({
          name:
            item.type === 'command'
              ? normalizedBuiltInCommandName(item.name)
              : item.name,
          description: item.description ?? '',
          type: item.type,
        }))
        .filter((item) => item.name.length > 0);
    } catch (error) {
      actionError = `${t('chat.skillsLoadError', 'Command and skill suggestions could not be loaded.')} ${error.message}`;
      availableSkills = [];
    }
  };

  // Sub-agent status self-heal lookup. When a sub-agent tool row's dot shows
  // "running" but no live status has been recorded in `subAgentRunStatuses`,
  // the row's "running" belief comes from a frozen persisted descriptor alone
  // (typical after a page refresh, a missed terminal event, a rolled replay
  // buffer, or a server restart that killed the child). This path asks the
  // server for the child's durable truth (`chat.history` → `active_run` or the
  // last `run_summary`) and projects it into the same `run:`/`session:` keys
  // the run stream would have written, so the dot settles correctly without
  // depending on event replay. The once-per-key guard prevents re-verification
  // churn across re-renders; the error path releases the guard so a later
  // attempt can retry.
  const SUBAGENT_STATUS_VERIFICATION_HISTORY_LIMIT = 20;
  const subAgentStatusVerificationKeys = new SvelteSet();
  const subAgentStatusInflightKeys = new SvelteSet();

  // Single write path for the status projection: LRU-merge under the cap and
  // release the verification guards of evicted `run:`/`session:` keys, so a
  // still-rendered row whose status entry aged out can self-heal again
  // instead of showing a frozen "running" dot behind a spent guard.
  const applySubAgentRunStatusUpdates = (updates) => {
    const { entries, evictedKeys } = mergeBoundedEntries(
      subAgentRunStatuses,
      updates,
      SUBAGENT_STATUS_CACHE_LIMIT,
    );
    subAgentRunStatuses = entries;
    for (const guardKey of subAgentGuardKeysForEvictedStatuses(evictedKeys)) {
      subAgentStatusVerificationKeys.delete(guardKey);
    }
  };

  const setSubAgentResultEntry = (key, entry) => {
    subAgentResults = mergeBoundedEntries(
      subAgentResults,
      { [key]: entry },
      SUBAGENT_RESULT_CACHE_LIMIT,
    ).entries;
  };
  const handleVerifySubAgentStatus = async (agentId, sessionId, runId) => {
    if (!agentId || !sessionId) {
      return;
    }
    const trimmedRunId = typeof runId === 'string' ? runId.trim() : '';
    const key = trimmedRunId || `${agentId}::${sessionId}`;
    if (
      subAgentStatusVerificationKeys.has(key) ||
      subAgentStatusInflightKeys.has(key)
    ) {
      return;
    }
    subAgentStatusInflightKeys.add(key);
    try {
      const history = await rpc('chat.history', {
        agent_id: agentId,
        session_id: sessionId,
        limit: SUBAGENT_STATUS_VERIFICATION_HISTORY_LIMIT,
      });
      const updates = {};
      const activeRunId =
        typeof history?.active_run?.run_id === 'string'
          ? history.active_run.run_id.trim()
          : '';
      // With a verified run id, only run-scoped keys are written: session-level
      // keys would bleed this run's state into other spawn rows that reuse the
      // same child session (handoff3 B6). A different run being active means
      // the verified run itself is over, so fall through to the summary scan.
      if (
        history?.active_run &&
        (!trimmedRunId || activeRunId === trimmedRunId)
      ) {
        if (activeRunId) {
          updates[`run:${activeRunId}`] = 'running';
        }
        if (!trimmedRunId) {
          updates[`session:${agentId}::${sessionId}`] = 'running';
        }
      } else {
        const messages = Array.isArray(history?.messages)
          ? history.messages
          : [];
        let summary = null;
        for (let index = messages.length - 1; index >= 0; index -= 1) {
          const message = messages[index];
          if (!message || message.role !== 'run_summary') {
            continue;
          }
          if (trimmedRunId) {
            const summaryRunId =
              typeof message.run_id === 'string' ? message.run_id.trim() : '';
            if (summaryRunId !== trimmedRunId) {
              continue;
            }
          }
          summary = message;
          break;
        }
        const status = summary
          ? normalizeSubAgentRunSummaryStatus(summary.status)
          : 'completed';
        const summaryRunId = summary
          ? typeof summary.run_id === 'string'
            ? summary.run_id.trim()
            : ''
          : '';
        const runKey = trimmedRunId || summaryRunId;
        if (runKey) {
          updates[`run:${runKey}`] = status;
        }
        if (!trimmedRunId) {
          updates[`session:${agentId}::${sessionId}`] = status;
        }
        const durationMs = summary?.timing?.duration_ms;
        if (Number.isFinite(durationMs) && durationMs >= 0) {
          if (runKey) {
            updates[`runDuration:${runKey}`] = durationMs;
          }
          if (!trimmedRunId) {
            updates[`sessionDuration:${agentId}::${sessionId}`] = durationMs;
          }
        }
      }
      if (Object.keys(updates).length > 0) {
        applySubAgentRunStatusUpdates(updates);
      }
      subAgentStatusVerificationKeys.add(key);
    } catch {
      // Release the guard so a later attempt can retry; verification
      // failures are never cached (contrast with `subAgentResults`).
    } finally {
      subAgentStatusInflightKeys.delete(key);
    }
  };

  // Normalizes a `run_summary` message's terminal `status` into one of the
  // status values `statusFromRunEvent` produces (`completed`/`failed`/
  // `cancelled`). Anything unrecognised falls back to `completed` so the dot
  // settles to success and the row can fetch its result instead of staying
  // stuck on `running` forever.
  function normalizeSubAgentRunSummaryStatus(value) {
    const status = typeof value === 'string' ? value.trim().toLowerCase() : '';
    if (status === 'failed' || status === 'error') {
      return 'failed';
    }
    if (status === 'cancelled' || status === 'canceled') {
      return 'cancelled';
    }
    return 'completed';
  }

  const loadAgents = async (options = {}) => {
    chatState.loadingAgents = true;
    chatState.agentsError = null;
    try {
      const result = await rpc('agent.list');
      const preferredAgentId =
        options.preferredAgentId ?? chatState.selectedAgentId;
      if (preferredAgentId) {
        selectAgent(chatState, preferredAgentId);
      }
      const selectedAgentId = setAgents(chatState, result.agents ?? []);
      onAgentsChanged?.(chatState.agents);
      if (selectedAgentId) {
        onAgentSelected?.(selectedAgentId);
      }
      if (selectedAgentId) {
        await loadCurrentHistory();
      }
    } catch (error) {
      chatState.agentsError = error.message;
    } finally {
      chatState.loadingAgents = false;
    }
  };

  const loadCurrentHistory = async () => {
    const agent = selectedAgent(chatState);
    if (!agent?.current_session_id) {
      return;
    }
    await loadHistoryForSession(agent.id, agent.current_session_id);
  };

  const syncSessionQueue = async (sessionState) => {
    if (!sessionState?.agentId || !sessionState?.sessionId) {
      return;
    }
    try {
      const result = await listQueue(
        sessionState.agentId,
        sessionState.sessionId,
      );
      syncQueueFromServer(sessionState, result?.items ?? []);
    } catch (error) {
      actionError = `${t('queue.syncError', 'Queued messages could not be synced.')} ${error.message}`;
    }
  };

  const loadHistoryForSession = async (agentId, sessionId) => {
    loadingHistory = true;
    historyError = '';
    const sessionState = ensureSessionState(chatState, agentId, sessionId);
    runStream.closeSubscriptionsExcept(sessionState.key);
    // Snapshot the run id we are about to ask the server about so the
    // reconcile step below can distinguish a *stale* run (terminal event was
    // missed, SSE gave up, bus buffer rolled, or the server restarted and
    // the run is gone) from a *genuinely new* run that started between
    // request and response — losing the latter would clobber state that
    // the next WS `run_started` is about to re-establish. See plan
    // `run-lifecycle-truth.md` Phase 2.1 "ChatView reconcile".
    const staleRunId = sessionState.currentRun?.runId ?? '';
    try {
      const history = await rpc('chat.history', {
        agent_id: agentId,
        session_id: sessionId,
        limit: HISTORY_INITIAL_LIMIT,
      });
      loadHistory(sessionState, history.messages ?? [], {
        hasMore: history.has_more === true,
      });
      // History is the durable source of truth for which run is active. If
      // it says "no active run" but the local state still claims a run is
      // running *with the same run id we had before the request*, that run
      // is dead — reset the live state and drop the SSE subscription so
      // `canCreateNewSession(...)` unblocks and the timeline falls back to
      // the just-loaded history. The `staleRunId === currentRun.runId`
      // guard prevents a race where a new run legitimately started
      // between request and response (a WS `run_started` will reassert
      // running state for the new run).
      if (
        !history.active_run &&
        isRunActive(sessionState) &&
        sessionState.currentRun?.runId === staleRunId
      ) {
        resetStaleRun(sessionState);
        runStream.closeSubscriptionFor(sessionState.key);
      }
      runStream.attachRunStream(sessionState, history.active_run);
      await syncSessionQueue(sessionState);
    } catch (error) {
      historyError = error.message;
      markSessionError(sessionState, error);
    } finally {
      loadingHistory = false;
    }
  };

  // Non-blocking sub-agent spawns only return a "running" descriptor, so once the
  // child run finishes the timeline asks for its final output here. We fetch the
  // child session's last assistant message and cache it under the row's cache
  // key (run-scoped when the child run id is known, so repeated spawns into the
  // same child session each get their own result — see handoff3 B6).
  const requestSubAgentResult = async (agentId, sessionId, cacheKey = '') => {
    if (!agentId || !sessionId) {
      return;
    }
    const key = cacheKey || `${agentId}::${sessionId}`;
    if (!subAgentResultEntryAllowsFetch(subAgentResults[key])) {
      return;
    }
    setSubAgentResultEntry(key, { loading: true, result: '' });
    try {
      const history = await rpc('chat.history', {
        agent_id: agentId,
        session_id: sessionId,
        limit: SUBAGENT_RESULT_HISTORY_LIMIT,
      });
      const result = subAgentResultTextFromMessages(history.messages ?? []);
      setSubAgentResultEntry(key, { loading: false, result });
    } catch {
      // Non-critical: the user can still open the sub-agent session directly.
      // Marked as a retryable failure instead of a permanent empty result, so
      // a transient chat.history error does not blank the row forever.
      setSubAgentResultEntry(key, {
        loading: false,
        result: '',
        error: true,
        failedAt: Date.now(),
      });
    }
  };

  const loadOlderHistory = async () => {
    const sessionState = activeSessionState;
    if (
      !sessionState ||
      !sessionState.agentId ||
      !sessionState.hasOlderHistory ||
      sessionState.loadingOlderHistory ||
      sessionState.messages.length === 0
    ) {
      return false;
    }

    const before = oldestLoadedMessageId(sessionState);
    if (!before) {
      sessionState.hasOlderHistory = false;
      return false;
    }

    sessionState.loadingOlderHistory = true;
    actionError = '';
    try {
      const history = await rpc('chat.history', {
        agent_id: sessionState.agentId,
        session_id: sessionState.sessionId,
        limit: HISTORY_OLDER_LIMIT,
        before,
      });
      prependHistory(sessionState, history.messages ?? [], {
        hasMore: history.has_more === true,
      });
      return true;
    } catch (error) {
      actionError = `${t('chat.historyOlderLoadError', 'Older chat history could not be loaded.')} ${error.message}`;
      return false;
    } finally {
      sessionState.loadingOlderHistory = false;
    }
  };

  function oldestLoadedMessageId(sessionState) {
    return (
      (sessionState.messages ?? []).find(
        (message) => typeof message?.id === 'string' && message.id.length > 0,
      )?.id ?? ''
    );
  }

  const handleSelectAgent = async (agentId) => {
    if (agentId === chatState.selectedAgentId) {
      if (sessionOverrideActive) {
        clearSessionOverride();
        reportSessionNavigation();
        await loadCurrentHistory();
      }
      return;
    }
    clearSessionOverride();
    selectAgent(chatState, agentId);
    onAgentSelected?.(agentId);
    reportSessionNavigation();
    await loadCurrentHistory();
  };

  const handleSubAgentNavigation = async (agentId, sessionId) => {
    if (!agentId || !sessionId) {
      return;
    }

    viewingSessionAgentId = agentId;
    viewingSessionId = sessionId;
    viewingSubAgentSession = true;
    await loadHistoryForSession(agentId, sessionId);
  };

  // Apply an App-driven navigation request: a sub-agent link click or a
  // browser-history restore. Restores re-enter past overrides (or return to
  // the current session) without creating new history entries.
  const applySessionNavigation = async (navigation) => {
    if (navigation.returnToCurrent) {
      if (sessionOverrideActive) {
        clearSessionOverride();
        await loadCurrentHistory();
      }
      return;
    }

    if (navigation.subAgent === true) {
      await handleSubAgentNavigation(navigation.agentId, navigation.sessionId);
      return;
    }

    viewingSessionAgentId =
      navigation.agentId === chatState.selectedAgentId
        ? ''
        : navigation.agentId;
    viewingSubAgentSession = false;
    viewingSessionId = navigation.sessionId;
    await loadHistoryForSession(navigation.agentId, navigation.sessionId);
  };

  const handleSessionSelected = async (sessionId) => {
    const agent = activeAgent;
    const normalizedSessionId = String(sessionId ?? '').trim();
    if (!agent || !normalizedSessionId) {
      return;
    }

    const isSelectedAgent = agent.id === chatState.selectedAgentId;
    viewingSessionAgentId = isSelectedAgent ? '' : agent.id;
    viewingSubAgentSession = !isSelectedAgent;
    viewingSessionId =
      isSelectedAgent && normalizedSessionId === agent.current_session_id
        ? ''
        : normalizedSessionId;
    reportSessionNavigation();
    await loadHistoryForSession(agent.id, normalizedSessionId);
  };

  const clearSessionOverride = () => {
    viewingSessionId = '';
    viewingSessionAgentId = '';
    viewingSubAgentSession = false;
  };

  // Report the (possibly cleared) session override to App so it becomes a
  // browser-history entry. Only user-initiated navigation calls this —
  // App-driven navigation through `pendingSessionNavigation` must not.
  const reportSessionNavigation = () => {
    onSessionNavigation?.(
      viewingSessionId
        ? {
            agentId: viewingSessionAgentId || chatState.selectedAgentId,
            sessionId: viewingSessionId,
            subAgent: viewingSubAgentSession,
          }
        : null,
    );
  };

  const handleReturnToCurrentSession = async () => {
    if (!sessionOverrideActive || loadingHistory) {
      return;
    }

    clearSessionOverride();
    reportSessionNavigation();
    await loadCurrentHistory();
  };

  const handleNewSession = async () => {
    const agent = selectedAgent(chatState);
    if (!agent || newSessionBlocked) {
      return;
    }
    clearSessionOverride();
    creatingSession = true;
    actionError = '';
    try {
      const session = await rpc('session.create', {
        agent_id: agent.id,
        make_current: true,
      });
      await switchToCurrentSession(agent.id, session.session_id);
    } catch (error) {
      actionError = `${t('chat.sessionCreateError', 'New session could not be created.')} ${error.message}`;
    } finally {
      creatingSession = false;
    }
  };

  const switchToCurrentSession = async (agentId, sessionId) => {
    const normalizedSessionId = String(sessionId ?? '').trim();
    if (!agentId || !normalizedSessionId) {
      return;
    }

    clearSessionOverride();
    const updatedAgents = chatState.agents.map((candidate) =>
      candidate.id === agentId
        ? { ...candidate, current_session_id: normalizedSessionId }
        : candidate,
    );
    setAgents(chatState, updatedAgents);
    onAgentsChanged?.(updatedAgents);
    onAgentSelected?.(agentId);
    reportSessionNavigation();
    ensureSessionState(chatState, agentId, normalizedSessionId);
    await loadHistoryForSession(agentId, normalizedSessionId);
  };

  const handleSendMessage = async (content, options = {}) => {
    const agent = activeAgent;
    const sessionState = activeSessionState;
    if (!agent || !sessionState) {
      return;
    }
    await sendStream(agent, sessionState, content, options);
  };

  const handleTranscriptionError = (message) => {
    actionError = message;
  };

  const sendStream = async (agent, sessionState, content, options = {}) => {
    actionError = '';
    actionInfo = '';
    try {
      const params = {
        agent_id: agent.id,
        session_id: sessionState.sessionId,
        content,
      };
      if (options.inputOrigin) {
        params.input_origin = options.inputOrigin;
      }
      const run = await rpc('chat.stream', params);
      if (run?.command_handled) {
        setActionInfo(run.reply);
        const commandSwitch = commandSwitchFromResponse(run);
        if (commandSwitch) {
          const targetAgentId = commandSwitch.targetAgentId || agent.id;
          // When the switch targets a different agent than the one currently
          // selected, update the agent-selection state first so the shared
          // selection flow observes the new agent before the session switch
          // lands. `switchToCurrentSession` then updates the target agent's
          // `current_session_id` and loads the new session.
          if (targetAgentId !== chatState.selectedAgentId) {
            selectAgent(chatState, targetAgentId);
            onAgentSelected?.(targetAgentId);
          }
          await switchToCurrentSession(targetAgentId, commandSwitch.sessionId);
        } else if (isCompactCommand(content)) {
          await loadHistoryForSession(agent.id, sessionState.sessionId);
        }
        return true;
      }

      if (run?.queued === true) {
        addServerQueuedMessage(sessionState, run.item);
        return true;
      }

      startRun(sessionState, run);
      submittedTurnScrollRunId = run.run_id ?? '';
      submittedTurnScrollKey += 1;
      runStream.subscribeToRun(sessionState, run.sse_url, {
        afterSequence: 0,
      });
      return true;
    } catch (error) {
      actionError = `${t('chat.sendError', 'Message could not be sent.')} ${error.message}`;
      markSessionError(sessionState, error);
      return false;
    }
  };

  const handleCancelRun = async () => {
    const sessionState = activeSessionState;
    const runId = sessionState?.currentRun?.runId;
    if (!runId) {
      return;
    }
    cancellingRun = true;
    actionError = '';
    try {
      await cancelRun(runId);
    } catch (error) {
      actionError = `${t('chat.cancelError', 'Run could not be cancelled.')} ${error.message}`;
    } finally {
      cancellingRun = false;
    }
  };

  // Per-tool-call cancel: cancel the bash without aborting the owning run.
  const handleCancelToolCall = async ({ runId, toolCallId } = {}) => {
    const agent = activeAgent;
    if (!runId || !toolCallId) {
      return;
    }
    actionError = '';
    try {
      await cancelToolCall({
        agentId: agent?.id ?? '',
        runId,
        toolCallId,
      });
    } catch (error) {
      actionError = `${t('chat.cancelError', 'Run could not be cancelled.')} ${error.message}`;
    }
  };

  // Per-sub-agent cancel: a running sub-agent is itself a Run, so route through
  // chat.cancel with reason="user". A queued sub-agent (no run_id yet) falls
  // back to chat.queue_remove.
  const handleCancelSubAgent = async ({ tool } = {}) => {
    const sessionState = activeSessionState;
    const agent = activeAgent;
    if (!tool || !sessionState) {
      return;
    }
    const data = subAgentResultData(tool);
    const childRunId =
      typeof data.run_id === 'string' ? data.run_id.trim() : '';
    const childAgentId =
      typeof data.agent_id === 'string' ? data.agent_id.trim() : '';
    const childSessionId =
      typeof data.session_id === 'string' ? data.session_id.trim() : '';
    const queueItemId =
      typeof data.queue_item_id === 'string' ? data.queue_item_id.trim() : '';

    actionError = '';
    try {
      if (childRunId) {
        await cancelRun(childRunId, { reason: 'user' });
        return;
      }
      if (queueItemId && agent && childAgentId && childSessionId) {
        await removeFromQueue(childAgentId, childSessionId, queueItemId);
      }
    } catch (error) {
      actionError = `${t('chat.cancelError', 'Run could not be cancelled.')} ${error.message}`;
    }
  };

  const handleRetry = async () => {
    const agent = activeAgent;
    const sessionState = activeSessionState;
    if (!agent || !sessionState || isRunActive(sessionState)) {
      return;
    }
    actionError = '';
    try {
      const run = await rpc('chat.retry_last_turn', {
        agent_id: agent.id,
        session_id: sessionState.sessionId,
      });
      startRun(sessionState, run);
      runStream.subscribeToRun(sessionState, run.sse_url, {
        afterSequence: 0,
      });
    } catch (error) {
      actionError = `${t('chat.retryError', 'Retry failed.')} ${error.message}`;
    }
  };

  export async function retryLastTurn() {
    await handleRetry();
  }

  // Exposed for tests and for the run-component verification wiring
  // (`onVerifySubAgentStatus` callback chain → ChatTimeline → ChatAssistantRun
  // → subAgentNeedsStatusVerification). Returns a promise that resolves when
  // the verification round-trip finishes.
  export async function verifySubAgentStatus(agentId, sessionId, runId) {
    await handleVerifySubAgentStatus(agentId, sessionId, runId);
  }

  const handleRemoveQueuedMessage = async (queuedMessageId) => {
    const sessionState = activeSessionState;
    const agent = activeAgent;
    if (!sessionState || !agent) {
      return;
    }

    actionError = '';
    try {
      await removeFromQueue(agent.id, sessionState.sessionId, queuedMessageId);
      removeQueuedMessage(sessionState, queuedMessageId);
    } catch (error) {
      actionError = `${t('queue.removeError', 'Queued message could not be removed.')} ${error.message}`;
    }
  };

  const handleEditQueuedMessage = async (queuedMessageId, newContent) => {
    const sessionState = activeSessionState;
    const agent = activeAgent;
    if (!sessionState || !agent) {
      return;
    }

    actionError = '';
    try {
      await updateQueueItem(
        agent.id,
        sessionState.sessionId,
        queuedMessageId,
        newContent,
      );
      updateQueuedMessageContent(sessionState, queuedMessageId, newContent);
    } catch (error) {
      actionError = `${t('queue.editError', 'Queued message could not be edited.')} ${error.message}`;
    }
  };

  const runStream = createChatRunStream({
    chatState,
    subscribeRunEvents,
    syncSessionQueue,
    isDisplayedSession,
    setActionError: (message) => {
      actionError = message;
    },
    updateSubAgentRunStatuses: applySubAgentRunStatusUpdates,
  });
</script>

<section
  class="view view-chat active chat-view"
  data-chat-width={chatWidth}
  aria-labelledby="chat-title"
>
  <ChatHeader
    agents={chatState.agents}
    selectedAgentId={chatState.selectedAgentId}
    loadingAgents={chatState.loadingAgents}
    {activeAgent}
    {activeSessionState}
    {showSessionDrawer}
    {cancellingRun}
    {creatingSession}
    {newSessionBlocked}
    {wakewordStatus}
    {desktopCapabilities}
    onSelectAgent={handleSelectAgent}
    onToggleSessionDrawer={() => {
      showSessionDrawer = !showSessionDrawer;
    }}
    onCancelRun={handleCancelRun}
    onNewSession={handleNewSession}
    {onNavigateToVoiceSettings}
  />

  {#if chatState.loadingAgents}
    <div class="empty-state chat-view__state">
      <p class="empty-state-title">{t('loading.agents', 'Loading agents…')}</p>
    </div>
  {:else if chatState.agents.length === 0}
    <div class="empty-state chat-view__state">
      <p class="empty-state-title">
        {t('chat.noAgents', 'No agents are available yet.')}
      </p>
      {#if chatState.agentsError}
        <p class="empty-state-sub">{chatState.agentsError}</p>
      {/if}
    </div>
  {:else if !activeAgent}
    <div class="empty-state chat-view__state">
      <p class="empty-state-title">
        {t('chat.noAgentSelected', 'Choose an agent to start chatting.')}
      </p>
    </div>
  {:else}
    <div class="chat-view__content-shell">
      {#if showSessionDrawer}
        <SessionListDrawer
          agentId={activeAgent.id}
          currentSessionId={viewingSessionId || activeAgent.current_session_id}
          agentCurrentSessionId={activeAgent.current_session_id}
          onSessionSelected={handleSessionSelected}
        />
      {/if}
      <div class="chat-view__surface">
        {#if loadingHistory || historyError || actionError || actionInfo || activeSessionState?.error}
          <div class="chat-view__notice-stack" aria-live="polite">
            <div class="chat-view__measure chat-view__notice-inner">
              {#if loadingHistory}
                <p class="chat-view__notice">
                  {t('loading.history', 'Loading chat history…')}
                </p>
              {/if}
              {#if historyError}
                <p class="chat-view__error">
                  {t(
                    'chat.historyLoadError',
                    'Chat history could not be loaded.',
                  )}
                  {historyError}
                </p>
              {/if}
              {#if actionError}
                <p class="chat-view__error">{actionError}</p>
              {/if}
              {#if actionInfo}
                <p class="chat-view__info">{actionInfo}</p>
              {/if}
              {#if activeSessionState?.error}
                <p class="chat-view__error">
                  {t('chat.runError', 'Run failed.')}
                  {activeSessionState.error}
                </p>
              {/if}
            </div>
          </div>
        {/if}
        <div class="chat-view__timeline-shell">
          <ChatTimeline
            sessionState={activeSessionState}
            agentName={activeAgent.name}
            {submittedTurnScrollKey}
            {submittedTurnScrollRunId}
            hasOlderHistory={activeSessionState?.hasOlderHistory === true}
            loadingOlderHistory={activeSessionState?.loadingOlderHistory ===
              true}
            subAgentStatuses={subAgentRunStatuses}
            {subAgentResults}
            onLoadOlder={loadOlderHistory}
            onNavigateToSubAgent={navigateToSubAgent}
            onRequestSubAgentResult={requestSubAgentResult}
            onVerifySubAgentStatus={verifySubAgentStatus}
            onRetry={handleRetry}
            onCancelToolCall={handleCancelToolCall}
            onCancelSubAgent={handleCancelSubAgent}
          />
        </div>
        <div class="chat-view__footer-stack">
          {#if sessionOverrideActive}
            <div class="chat-view__subagent-session-notice" aria-live="polite">
              <div class="chat-view__subagent-session-copy">
                <p class="chat-view__subagent-session-title">
                  {subAgentSessionActive
                    ? t(
                        'chat.subagentSessionNotice',
                        'Viewing a sub-agent session',
                      )
                    : t('chat.pastSessionNotice', 'Viewing a past session')}
                </p>
                <p class="chat-view__subagent-session-hint">
                  {subAgentSessionActive
                    ? t(
                        'chat.subagentSessionHint',
                        'Messages here continue this sub-agent session. Return to the current agent session when you are done.',
                      )
                    : t(
                        'chat.pastSessionHint',
                        'This is not the agent’s current session. Messages sent here continue this past session.',
                      )}
                </p>
              </div>
              <button
                type="button"
                class="btn-outline chat-view__subagent-session-return"
                disabled={loadingHistory}
                onclick={handleReturnToCurrentSession}
              >
                {t('chat.returnToCurrentSession', 'Return to current session')}
              </button>
            </div>
          {/if}
          <div class="chat-view__measure">
            <QueuedMessages
              queuedMessages={activeSessionState?.queue ?? []}
              onRemoveQueuedMessage={handleRemoveQueuedMessage}
              onEditQueuedMessage={handleEditQueuedMessage}
            />
          </div>
          <ChatComposer
            disabled={composerDisabled}
            isRunning={isRunActive(activeSessionState)}
            {availableSkills}
            onSendMessage={handleSendMessage}
            onTranscriptionError={handleTranscriptionError}
          />
        </div>
      </div>
    </div>
  {/if}
</section>

<style>
  .chat-view {
    display: flex;
    width: 100%;
    height: 100%;
    min-height: 0;
    flex-direction: column;
    overflow: hidden;
    background: var(--bg);
  }

  .chat-view__surface {
    display: flex;
    min-height: 0;
    flex: 1;
    flex-direction: column;
    overflow: hidden;
    background: var(--bg);
  }

  .chat-view__content-shell {
    display: flex;
    min-height: 0;
    flex: 1;
    overflow: hidden;
  }

  .chat-view__timeline-shell {
    display: flex;
    min-height: 0;
    flex: 1;
    overflow: hidden;
  }

  .chat-view__footer-stack {
    display: flex;
    flex-shrink: 0;
    flex-direction: column;
    min-height: 0;
    background: var(--surface);
  }

  .chat-view__state {
    flex: 1;
  }

  .chat-view__notice-stack {
    flex-shrink: 0;
    padding: 10px 20px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }

  /* Center inner content on the same axis as the capped message column. Bars
     (notice stack, composer) stay full-width; their content is capped to
     `--chat-measure` and centered. `full` disables the cap (measure: none). */
  .chat-view__measure {
    width: 100%;
    max-width: var(--chat-measure);
    margin-inline: auto;
  }

  .chat-view__notice-inner {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .chat-view__notice,
  .chat-view__info,
  .chat-view__error {
    margin: 0;
    color: var(--text-med);
    font-size: 12.5px;
  }

  .chat-view__info {
    color: var(--text-med);
    white-space: pre-wrap;
  }

  .chat-view__error {
    color: var(--red);
  }

  .chat-view__subagent-session-notice {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    flex-shrink: 0;
    width: 100%;
    max-width: var(--chat-measure);
    margin-inline: auto;
    border-left: 3px solid var(--accent);
    padding: 9px 20px 9px 12px;
    border-top: 1px solid var(--border);
    background: linear-gradient(
      90deg,
      rgba(232, 135, 10, 0.08),
      transparent 72%
    );
  }

  .chat-view__subagent-session-copy {
    min-width: 0;
  }

  .chat-view__subagent-session-title,
  .chat-view__subagent-session-hint {
    margin: 0;
  }

  .chat-view__subagent-session-title {
    color: var(--accent);
    font-family: var(--font-mono);
    font-size: 10.5px;
    font-weight: 500;
    letter-spacing: 0.07em;
    text-transform: uppercase;
  }

  .chat-view__subagent-session-hint {
    margin-top: 4px;
    color: var(--text-med);
    font-size: 12.5px;
  }

  .chat-view__subagent-session-return {
    flex-shrink: 0;
  }

  @media (max-width: 640px) {
    .chat-view__notice-stack {
      padding: 10px 14px;
    }

    .chat-view__content-shell {
      flex-direction: column;
    }

    .chat-view__subagent-session-notice {
      align-items: flex-start;
      flex-direction: column;
    }

    .chat-view__subagent-session-return {
      margin-right: 0;
    }
  }
</style>
