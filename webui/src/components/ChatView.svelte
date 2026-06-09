<script>
  import { onDestroy, onMount } from 'svelte';

  import {
    cancelRun,
    cancelToolCall,
    listQueue,
    removeFromQueue,
    rpc,
    subscribeRunEvents,
    updateQueueItem,
  } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import {
    subAgentResultData,
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
    agentsRefreshToken = 0,
    onAgentsChanged,
    onAgentSelected,
    navigateToSubAgent = () => {},
    pendingSubAgentNavigation = null,
    runServerEvent = null,
    runServerEvents = [],
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
  let handledSubAgentNavigationKey = '';
  const ACTION_INFO_TIMEOUT_MS = 4000;
  const HISTORY_INITIAL_LIMIT = 100;
  const HISTORY_OLDER_LIMIT = 50;
  const SUBAGENT_RESULT_HISTORY_LIMIT = 20;
  let actionInfoTimeoutId = null;

  let activeAgent = $derived(getActiveAgent());
  let activeSessionState = $derived(getActiveSessionState());
  let subAgentSessionActive = $derived(
    Boolean(viewingSessionId) && viewingSubAgentSession,
  );
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

  $effect(() => {
    const agentId = pendingSubAgentNavigation?.agentId;
    const sessionId = pendingSubAgentNavigation?.sessionId;
    const requestId = pendingSubAgentNavigation?.requestId ?? '';
    const navigationKey =
      agentId && sessionId ? `${agentId}::${sessionId}::${requestId}` : '';
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
    runStream.closeSubscriptionsExcept(sessionState.key);
    try {
      const history = await rpc('chat.history', {
        agent_id: agentId,
        session_id: sessionId,
        limit: HISTORY_INITIAL_LIMIT,
      });
      loadHistory(sessionState, history.messages ?? [], {
        hasMore: history.has_more === true,
      });
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
  // child session's last assistant message once per (agent, session) and cache it.
  const requestSubAgentResult = async (agentId, sessionId) => {
    if (!agentId || !sessionId) {
      return;
    }
    const key = `${agentId}::${sessionId}`;
    if (subAgentResults[key]) {
      return;
    }
    subAgentResults = {
      ...subAgentResults,
      [key]: { loading: true, result: '' },
    };
    try {
      const history = await rpc('chat.history', {
        agent_id: agentId,
        session_id: sessionId,
        limit: SUBAGENT_RESULT_HISTORY_LIMIT,
      });
      const result = subAgentResultTextFromMessages(history.messages ?? []);
      subAgentResults = {
        ...subAgentResults,
        [key]: { loading: false, result },
      };
    } catch {
      // Non-critical: the user can still open the sub-agent session directly.
      subAgentResults = {
        ...subAgentResults,
        [key]: { loading: false, result: '' },
      };
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

    viewingSessionAgentId = agentId;
    viewingSessionId = sessionId;
    viewingSubAgentSession = true;
    await loadHistoryForSession(agentId, sessionId);
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
    await loadHistoryForSession(agent.id, normalizedSessionId);
  };

  const clearSessionOverride = () => {
    viewingSessionId = '';
    viewingSessionAgentId = '';
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
    updateSubAgentRunStatuses: (updates) => {
      subAgentRunStatuses = { ...subAgentRunStatuses, ...updates };
    },
  });
</script>

<section class="view view-chat active chat-view" aria-labelledby="chat-title">
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
            subAgentStatuses={subAgentRunStatuses}
            {subAgentResults}
            onLoadOlder={loadOlderHistory}
            onNavigateToSubAgent={navigateToSubAgent}
            onRequestSubAgentResult={requestSubAgentResult}
            onRetry={handleRetry}
            onCancelToolCall={handleCancelToolCall}
            onCancelSubAgent={handleCancelSubAgent}
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

  @media (max-width: 760px) {
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
