<script>
  import { onDestroy, onMount } from 'svelte';

  import {
    listQueue,
    removeFromQueue,
    rpc,
    subscribeRunEvents,
    updateQueueItem,
  } from '$lib/api.js';
  import { t } from '$lib/i18n.js';

  import {
    TERMINAL_RUN_EVENTS,
    addServerQueuedMessage,
    appendCompactionCheckpoint,
    appendRunEvent,
    canCreateNewSession,
    createChatState,
    currentSessionState,
    ensureSessionState,
    highestRunEventSequence,
    isRunActive,
    loadHistory,
    markSessionError,
    prependHistory,
    removeQueuedMessage,
    selectAgent,
    selectedAgent,
    setAgents,
    startRun,
    syncQueueFromServer,
    updateQueuedMessageContent,
  } from '../lib/chatState.js';
  import ChatComposer from './ChatComposer.svelte';
  import SessionListDrawer from './SessionListDrawer.svelte';
  import ChatTimeline from './ChatTimeline.svelte';
  import QueuedMessages from './QueuedMessages.svelte';

  let {
    sharedAgents = [],
    sharedSelectedAgentId = '',
    agentsRefreshToken = 0,
    onAgentsChanged,
    onAgentSelected,
    navigateToSubAgent = () => {},
    pendingSubAgentNavigation = null,
    runServerEvent = null,
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
  let viewingSubAgentSession = $state(false);
  let submittedTurnScrollKey = $state(0);
  let submittedTurnScrollRunId = $state('');
  let handledSubAgentNavigationKey = '';
  const activeSubscriptions = {};
  const pendingReconnects = {};
  const pendingRunEventQueues = {};
  const pendingRunEventFlushes = {};
  const ACTION_INFO_TIMEOUT_MS = 4000;
  const SSE_RECONNECT_DELAY_MS = 500;
  const MAX_SSE_RECONNECT_ATTEMPTS = 3;
  const RUN_EVENT_FLUSH_DELAY_MS = 33;
  const RUN_SERVER_EVENT_TYPES = new Set([
    'run_started',
    'run_output',
    'run_completed',
    'run_cancelled',
    'run_failed',
  ]);
  const HISTORY_INITIAL_LIMIT = 100;
  const HISTORY_OLDER_LIMIT = 50;
  let actionInfoTimeoutId = null;
  let lastRunServerEventKey = '';

  let activeAgent = $derived(selectedAgent(chatState));
  let activeSessionState = $derived(getActiveSessionState());
  let subAgentSessionActive = $derived(
    Boolean(viewingSessionId) && viewingSubAgentSession,
  );
  let newSessionBlocked = $derived(!canCreateNewSession(activeSessionState));
  let composerDisabled = $derived(!activeAgent || loadingHistory);
  let lastSharedSelectedAgentId = '';
  let lastSharedAgents = null;
  let lastAgentsRefreshToken = null;

  const usageTotalTokens = (usage) => {
    const inputTokens = Number.isFinite(usage?.input_tokens)
      ? usage.input_tokens
      : 0;
    const outputTokens = Number.isFinite(usage?.output_tokens)
      ? usage.output_tokens
      : 0;
    return inputTokens + outputTokens;
  };

  let tokenBadgeText = $derived.by(() => {
    const usage = activeSessionState?.usage;
    const contextWindow = activeAgent?.context_window;
    const numberFormat = new Intl.NumberFormat();

    if (usage) {
      const tokensFormatted = numberFormat.format(usageTotalTokens(usage));
      const estimated = usage.estimated === true;

      if (contextWindow != null) {
        const contextFormatted = numberFormat.format(contextWindow);
        return estimated
          ? t('chat.tokenBadgeEstimated', '~{tokens} / {context} tok', {
              tokens: tokensFormatted,
              context: contextFormatted,
            })
          : t('chat.tokenBadge', '{tokens} / {context} tok', {
              tokens: tokensFormatted,
              context: contextFormatted,
            });
      }
      return estimated
        ? t('chat.tokenBadgeEstimatedNoContext', '~{tokens} tok', {
            tokens: tokensFormatted,
          })
        : t('chat.tokenBadgeNoContext', '{tokens} tok', {
            tokens: tokensFormatted,
          });
    }
    if (contextWindow != null) {
      return t('chat.tokenBadgeNoUsage', '— / {context} tok', {
        context: numberFormat.format(contextWindow),
      });
    }
    return '';
  });

  function getActiveSessionState() {
    const agent = selectedAgent(chatState);
    if (agent && viewingSessionId) {
      return chatState.sessions[`${agent.id}::${viewingSessionId}`] ?? null;
    }
    return currentSessionState(chatState);
  }

  function displayedSessionKey() {
    const agent = selectedAgent(chatState);
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

  $effect(() => {
    const agentId = pendingSubAgentNavigation?.agentId;
    const sessionId = pendingSubAgentNavigation?.sessionId;
    const navigationKey =
      agentId && sessionId ? `${agentId}::${sessionId}` : '';
    if (!navigationKey || navigationKey === handledSubAgentNavigationKey) {
      return;
    }

    handledSubAgentNavigationKey = navigationKey;
    handleSubAgentNavigation(agentId, sessionId);
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
    const eventKey = runServerEventKey(runServerEvent);
    if (!eventKey || eventKey === lastRunServerEventKey) {
      return;
    }
    lastRunServerEventKey = eventKey;
    handleRunServerEvent(runServerEvent);
  });

  onMount(() => {
    loadAgents({ preferredAgentId: sharedSelectedAgentId });
    loadCommands();
    return () => closeSubscriptions();
  });

  onDestroy(() => {
    if (actionInfoTimeoutId !== null) {
      clearTimeout(actionInfoTimeoutId);
      actionInfoTimeoutId = null;
    }
    clearPendingReconnects();
    clearPendingRunEventFlushes();
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

  const newSessionIdFromCommandResponse = (response) => {
    const data = response?.data;
    if (data?.command !== 'new' || typeof data.session_id !== 'string') {
      return '';
    }
    return data.session_id.trim();
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
    closeSubscriptionsExcept(sessionState.key);
    try {
      const history = await rpc('chat.history', {
        agent_id: agentId,
        session_id: sessionId,
        limit: HISTORY_INITIAL_LIMIT,
      });
      loadHistory(sessionState, history.messages ?? [], {
        hasMore: history.has_more === true,
      });
      attachRunStream(sessionState, history.active_run);
      await syncSessionQueue(sessionState);
    } catch (error) {
      historyError = error.message;
      markSessionError(sessionState, error);
    } finally {
      loadingHistory = false;
    }
  };

  const loadOlderHistory = async () => {
    const agent = selectedAgent(chatState);
    const sessionState = activeSessionState;
    if (
      !agent ||
      !sessionState ||
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
        agent_id: agent.id,
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
      if (subAgentSessionActive) {
        clearSessionOverride();
        await loadCurrentHistory();
      }
      return;
    }
    clearSessionOverride();
    selectAgent(chatState, agentId);
    onAgentSelected?.(agentId);
    await loadCurrentHistory();
  };

  const handleSubAgentNavigation = async (agentId, sessionId) => {
    if (!agentId || !sessionId) {
      return;
    }

    if (agentId !== chatState.selectedAgentId) {
      selectAgent(chatState, agentId);
      onAgentSelected?.(agentId);
    }

    viewingSessionId = sessionId;
    viewingSubAgentSession = true;
    await loadHistoryForSession(agentId, sessionId);
  };

  const handleSessionSelected = async (sessionId) => {
    const agent = selectedAgent(chatState);
    const normalizedSessionId = String(sessionId ?? '').trim();
    if (!agent || !normalizedSessionId) {
      return;
    }

    viewingSubAgentSession = false;
    viewingSessionId =
      normalizedSessionId === agent.current_session_id
        ? ''
        : normalizedSessionId;
    await loadHistoryForSession(agent.id, normalizedSessionId);
  };

  const clearSessionOverride = () => {
    viewingSessionId = '';
    viewingSubAgentSession = false;
  };

  const handleReturnToCurrentSession = async () => {
    if (!subAgentSessionActive || loadingHistory) {
      return;
    }

    clearSessionOverride();
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
    ensureSessionState(chatState, agentId, normalizedSessionId);
    await loadHistoryForSession(agentId, normalizedSessionId);
  };

  const handleSendMessage = async (content) => {
    const agent = selectedAgent(chatState);
    const sessionState = activeSessionState;
    if (!agent || !sessionState) {
      return;
    }
    await sendStream(agent, sessionState, content);
  };

  const sendStream = async (agent, sessionState, content) => {
    actionError = '';
    actionInfo = '';
    try {
      const run = await rpc('chat.stream', {
        agent_id: agent.id,
        session_id: sessionState.sessionId,
        content,
      });
      if (run?.command_handled) {
        setActionInfo(run.reply);
        const newSessionId = newSessionIdFromCommandResponse(run);
        if (newSessionId) {
          await switchToCurrentSession(agent.id, newSessionId);
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
      subscribeToRun(sessionState, run.sse_url, { afterSequence: 0 });
      return true;
    } catch (error) {
      actionError = `${t('chat.sendError', 'Message could not be sent.')} ${error.message}`;
      markSessionError(sessionState, error);
      return false;
    }
  };

  const subscribeToRun = (sessionState, sseUrl, options = {}) => {
    if (!sseUrl) {
      return;
    }
    if (sessionState.currentRun) {
      sessionState.currentRun.sseUrl = sseUrl;
    }
    const existingSubscription = activeSubscriptions[sessionState.key];
    existingSubscription?.close();
    clearPendingReconnect(sessionState.key);
    const afterSequence =
      options.afterSequence ?? highestRunEventSequence(sessionState);
    const retryAttempt = options.retryAttempt ?? 0;
    const subscription = subscribeRunEvents(
      sseUrl,
      {
        onEvent: ({ data }) => {
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
  };

  const attachRunStream = (sessionState, run, options = {}) => {
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

    if (!alreadySubscribed) {
      subscribeToRun(sessionState, sseUrl, {
        afterSequence:
          options.afterSequence ?? highestRunEventSequence(sessionState),
      });
    }

    return true;
  };

  const queueRunEvent = (sessionState, eventData) => {
    const sessionKey = sessionState.key;
    pendingRunEventQueues[sessionKey] ??= [];
    pendingRunEventQueues[sessionKey].push(eventData);
    scheduleRunEventFlush(sessionKey);
  };

  const scheduleRunEventFlush = (sessionKey) => {
    if (pendingRunEventFlushes[sessionKey] !== undefined) {
      return;
    }
    pendingRunEventFlushes[sessionKey] = setTimeout(() => {
      delete pendingRunEventFlushes[sessionKey];
      flushPendingRunEvents(sessionKey);
    }, RUN_EVENT_FLUSH_DELAY_MS);
  };

  const flushPendingRunEvents = (sessionKey) => {
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
  };

  const handleAppendedRunEvent = (sessionState, event) => {
    if (!event) {
      return;
    }
    if (event.type === 'compaction_completed' && event.payload?.message) {
      appendCompactionCheckpoint(sessionState, event.payload.message);
    }
    if (TERMINAL_RUN_EVENTS.has(event.type)) {
      clearPendingReconnect(sessionState.key);
      closeRunSubscription(sessionState.key);
      if (event.type !== 'run_failed') {
        actionError = '';
      }
      void syncSessionQueue(sessionState);
    }
  };

  const recoverRunStream = (sessionState, sseUrl, retryAttempt, error) => {
    const sessionKey = sessionState.key;
    flushPendingRunEvents(sessionKey);
    const currentRun = sessionState.currentRun;
    if (!currentRun || currentRun.status !== 'running') {
      return;
    }

    if (retryAttempt < MAX_SSE_RECONNECT_ATTEMPTS) {
      actionError = t(
        'errors.streamReconnecting',
        'The live stream closed. Reconnecting...',
      );
      if (pendingReconnects[sessionKey] !== undefined) {
        return;
      }
      closeRunSubscription(sessionKey);
      pendingReconnects[sessionKey] = setTimeout(() => {
        delete pendingReconnects[sessionKey];
        if (sessionState.currentRun?.runId !== currentRun.runId) {
          return;
        }
        subscribeToRun(sessionState, currentRun.sseUrl || sseUrl, {
          afterSequence: highestRunEventSequence(sessionState),
          retryAttempt: retryAttempt + 1,
        });
      }, SSE_RECONNECT_DELAY_MS);
      return;
    }

    actionError = `${t('errors.streamClosed', 'The live stream closed before the run finished. Waiting for server status.')} ${error?.message ?? ''}`;
    closeRunSubscription(sessionState.key);
  };

  const handleRunServerEvent = (serverEvent) => {
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
    handleAppendedRunEvent(sessionState, appended);
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
        { afterSequence: highestRunEventSequence(sessionState) },
      );
    }
  };

  const runEventFromServerEvent = (serverEvent) => {
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

    return {
      type: runEventType,
      run_id: payload.run_id,
      agent_id: payload.agent_id,
      session_id: payload.session_id,
      sequence: payload.run_event_sequence,
      timestamp: payload.run_event_timestamp,
      payload: runPayload,
    };
  };

  const runServerEventKey = (serverEvent) => {
    const payload = serverEvent?.payload;
    if (
      !payload?.run_id ||
      (payload.run_event_sequence !== 0 && !payload.run_event_sequence)
    ) {
      return '';
    }
    return `${payload.run_id}:${payload.run_event_sequence}:${serverEvent.type}`;
  };

  const closeRunSubscription = (sessionKey) => {
    activeSubscriptions[sessionKey]?.close();
    delete activeSubscriptions[sessionKey];
  };

  const closeSubscriptionsExcept = (sessionKey) => {
    for (const key of Object.keys(activeSubscriptions)) {
      if (key === sessionKey) {
        continue;
      }
      closeRunSubscription(key);
      clearPendingReconnect(key);
    }
  };

  const sseUrlForRun = (runId) =>
    `/api/runs/${encodeURIComponent(String(runId))}/events`;

  const clearPendingReconnect = (sessionKey) => {
    const timeoutId = pendingReconnects[sessionKey];
    if (timeoutId !== undefined) {
      clearTimeout(timeoutId);
      delete pendingReconnects[sessionKey];
    }
  };

  const clearPendingReconnects = () => {
    for (const key of Object.keys(pendingReconnects)) {
      clearPendingReconnect(key);
    }
  };

  const clearPendingRunEventFlush = (sessionKey) => {
    const timeoutId = pendingRunEventFlushes[sessionKey];
    if (timeoutId !== undefined) {
      clearTimeout(timeoutId);
      delete pendingRunEventFlushes[sessionKey];
    }
  };

  const clearPendingRunEventFlushes = () => {
    for (const key of Object.keys(pendingRunEventFlushes)) {
      clearPendingRunEventFlush(key);
    }
    for (const key of Object.keys(pendingRunEventQueues)) {
      delete pendingRunEventQueues[key];
    }
  };

  const closeSubscriptions = () => {
    for (const subscription of Object.values(activeSubscriptions)) {
      subscription.close();
    }
    for (const key of Object.keys(activeSubscriptions)) {
      delete activeSubscriptions[key];
    }
    clearPendingReconnects();
    clearPendingRunEventFlushes();
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
      await rpc('chat.cancel', { run_id: runId });
    } catch (error) {
      actionError = `${t('chat.cancelError', 'Run could not be cancelled.')} ${error.message}`;
    } finally {
      cancellingRun = false;
    }
  };

  const handleRetry = async () => {
    const agent = selectedAgent(chatState);
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
      subscribeToRun(sessionState, run.sse_url, { afterSequence: 0 });
    } catch (error) {
      actionError = `${t('chat.retryError', 'Retry failed.')} ${error.message}`;
    }
  };

  export async function retryLastTurn() {
    await handleRetry();
  }

  const handleRemoveQueuedMessage = async (queuedMessageId) => {
    const sessionState = activeSessionState;
    const agent = selectedAgent(chatState);
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
    const agent = selectedAgent(chatState);
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
</script>

<section class="view view-chat active chat-view" aria-labelledby="chat-title">
  <header class="chat-header">
    <h2 id="chat-title" class="chat-title">{t('chat.title', 'Chat')}</h2>
    <div class="agent-tabs" aria-label={t('chat.selectAgent', 'Select agent')}>
      {#if chatState.agents.length > 0}
        {#each chatState.agents as agent (agent.id)}
          <button
            type="button"
            class:active={agent.id === chatState.selectedAgentId}
            class="agent-tab"
            disabled={chatState.loadingAgents}
            onclick={() => handleSelectAgent(agent.id)}
          >
            <span class="tab-indicator"></span>
            <span>{agent.name}</span>
          </button>
        {/each}
      {:else}
        <span class="agent-tab agent-tab--empty">
          <span class="tab-indicator"></span>
          {t('chat.noAgents', 'No agents are available yet.')}
        </span>
      {/if}
    </div>
    <div class="header-right">
      {#if tokenBadgeText}
        <span class="token-badge">{tokenBadgeText}</span>
      {/if}
      <button
        type="button"
        class:chat-sessions-toggle--active={showSessionDrawer}
        class="btn-outline chat-sessions-toggle"
        onclick={() => {
          showSessionDrawer = !showSessionDrawer;
        }}
        disabled={!activeAgent}
      >
        {showSessionDrawer
          ? t('sessions.hide', 'Hide sessions')
          : t('sessions.title', 'Sessions')}
      </button>
      {#if activeSessionState && isRunActive(activeSessionState)}
        <button
          type="button"
          class="btn-outline btn-dang"
          disabled={cancellingRun}
          onclick={handleCancelRun}
        >
          {cancellingRun
            ? t('cancel.cancelling', 'Cancelling run…')
            : t('chat.cancelRun', 'Cancel run')}
        </button>
      {/if}
      <button
        type="button"
        class="btn-new"
        disabled={!activeAgent || newSessionBlocked || creatingSession}
        title={newSessionBlocked
          ? t(
              'chat.newSessionBlocked',
              'A new session can be started after the current run finishes.',
            )
          : undefined}
        onclick={handleNewSession}
      >
        <svg viewBox="0 0 14 14" aria-hidden="true">
          <path d="M7 1v12M1 7h12" />
        </svg>
        {creatingSession
          ? t('common.loading', 'Loading…')
          : t('chat.newSession', 'New Session')}
      </button>
    </div>
  </header>

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
          onSessionSelected={handleSessionSelected}
        />
      {/if}
      <div class="chat-view__surface">
        {#if loadingHistory || historyError || actionError || actionInfo || activeSessionState?.error}
          <div class="chat-view__notice-stack" aria-live="polite">
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
            onLoadOlder={loadOlderHistory}
            onNavigateToSubAgent={navigateToSubAgent}
            onRetry={handleRetry}
          />
        </div>
        <div class="chat-view__footer-stack">
          {#if subAgentSessionActive}
            <div class="chat-view__subagent-session-notice" aria-live="polite">
              <div class="chat-view__subagent-session-copy">
                <p class="chat-view__subagent-session-title">
                  {t(
                    'chat.subagentSessionNotice',
                    'Viewing a sub-agent session',
                  )}
                </p>
                <p class="chat-view__subagent-session-hint">
                  {t(
                    'chat.subagentSessionHint',
                    'Messages here continue this sub-agent session. Return to the current agent session when you are done.',
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
          <QueuedMessages
            queuedMessages={activeSessionState?.queue ?? []}
            onRemoveQueuedMessage={handleRemoveQueuedMessage}
            onEditQueuedMessage={handleEditQueuedMessage}
          />
          <ChatComposer
            disabled={composerDisabled}
            isRunning={isRunActive(activeSessionState)}
            {availableSkills}
            onSendMessage={handleSendMessage}
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

  .chat-header {
    display: flex;
    align-items: center;
    gap: 8px;
    height: 50px;
    flex-shrink: 0;
    padding: 0 20px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }

  .chat-title {
    position: absolute;
    width: 1px;
    height: 1px;
    margin: 0;
    overflow: hidden;
    clip: rect(0 0 0 0);
  }

  .agent-tabs {
    display: flex;
    min-width: 0;
    height: 100%;
    flex: 1;
    align-items: stretch;
    gap: 2px;
    overflow-x: auto;
  }

  .agent-tab {
    display: flex;
    flex-shrink: 0;
    align-items: center;
    gap: 7px;
    padding: 0 14px;
    border: 0;
    border-bottom: 2px solid transparent;
    color: var(--text-lo);
    background: transparent;
    font-family: var(--font-ui);
    font-size: 13px;
    font-weight: 500;
    white-space: nowrap;
    transition:
      border-color 150ms ease,
      color 150ms ease;
  }

  .agent-tab:hover,
  .agent-tab:focus-visible {
    color: var(--text-med);
    outline: none;
  }

  .agent-tab.active {
    border-bottom-color: var(--accent);
    color: var(--accent);
  }

  .agent-tab--empty {
    cursor: default;
  }

  .tab-indicator {
    width: 5px;
    height: 5px;
  }

  .header-right {
    display: flex;
    flex-shrink: 0;
    align-items: center;
    gap: 10px;
  }

  .token-badge {
    padding: 3px 8px;
    border: 1px solid var(--border);
    border-radius: var(--r-sm);
    color: var(--text-lo);
    background: var(--surface-2);
    font-family: var(--font-mono);
    font-size: 11px;
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

  .chat-sessions-toggle--active {
    border-color: var(--accent);
    color: var(--accent);
    background: rgba(232, 135, 10, 0.08);
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
    display: flex;
    flex-shrink: 0;
    flex-direction: column;
    gap: 6px;
    padding: 10px 20px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
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

  .btn-new svg {
    width: 12px;
    height: 12px;
  }

  @media (max-width: 760px) {
    .chat-header {
      height: auto;
      flex-wrap: wrap;
      padding: 10px 14px;
    }

    .agent-tabs {
      order: 2;
      width: 100%;
      height: 38px;
      flex-basis: 100%;
    }

    .header-right {
      margin-left: auto;
      flex-wrap: wrap;
      justify-content: flex-end;
    }

    .token-badge {
      display: none;
    }

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
