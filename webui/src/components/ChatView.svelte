<script>
  import { onMount } from 'svelte';

  import { rpc, subscribeRunEvents } from '$lib/api.js';
  import { t } from '$lib/i18n.js';

  import {
    appendRunEvent,
    canCreateNewSession,
    createChatState,
    currentSessionState,
    dequeueMessage,
    enqueueMessage,
    ensureSessionState,
    highestRunEventSequence,
    isRunActive,
    loadHistory,
    markSessionError,
    removeQueuedMessage,
    restoreDequeuedMessage,
    selectAgent,
    selectedAgent,
    setAgents,
    startRun,
  } from '../lib/chatState.js';
  import ChatComposer from './ChatComposer.svelte';
  import ChatTimeline from './ChatTimeline.svelte';
  import QueuedMessages from './QueuedMessages.svelte';

  let {
    sharedAgents = [],
    sharedSelectedAgentId = '',
    agentsRefreshToken = 0,
    onAgentsChanged,
    onAgentSelected,
  } = $props();

  const chatState = $state(createChatState());
  let loadingHistory = $state(false);
  let creatingSession = $state(false);
  let cancellingRun = $state(false);
  let historyError = $state('');
  let actionError = $state('');
  const activeSubscriptions = {};

  let activeAgent = $derived(selectedAgent(chatState));
  let activeSessionState = $derived(currentSessionState(chatState));
  let newSessionBlocked = $derived(!canCreateNewSession(activeSessionState));
  let composerDisabled = $derived(!activeAgent || loadingHistory);
  let lastSharedSelectedAgentId = $state('');
  let lastAgentsRefreshToken = $state(null);
  let tokenBadgeText = $derived.by(() => {
    const usage = activeSessionState?.usage;
    const contextWindow = activeAgent?.context_window;
    const numberFormat = new Intl.NumberFormat();

    if (usage) {
      const inputFormatted = numberFormat.format(usage.input_tokens);
      const estimated = usage.estimated === true;

      if (contextWindow != null) {
        const contextFormatted = numberFormat.format(contextWindow);
        return estimated
          ? t('chat.tokenBadgeEstimated', '~{input} / {context} tok', {
              input: inputFormatted,
              context: contextFormatted,
            })
          : t('chat.tokenBadge', '{input} / {context} tok', {
              input: inputFormatted,
              context: contextFormatted,
            });
      }
      return estimated
        ? t('chat.tokenBadgeEstimatedNoContext', '~{input} tok', {
            input: inputFormatted,
          })
        : t('chat.tokenBadgeNoContext', '{input} tok', {
            input: inputFormatted,
          });
    }
    if (contextWindow != null) {
      return t('chat.tokenBadgeNoUsage', '— / {context} tok', {
        context: numberFormat.format(contextWindow),
      });
    }
    return '';
  });

  $effect(() => {
    if (sharedAgents.length > 0) {
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
    if (lastAgentsRefreshToken === null) {
      lastAgentsRefreshToken = agentsRefreshToken;
      return;
    }
    if (agentsRefreshToken !== lastAgentsRefreshToken) {
      lastAgentsRefreshToken = agentsRefreshToken;
      loadAgents({ preferredAgentId: sharedSelectedAgentId });
    }
  });

  onMount(() => {
    loadAgents({ preferredAgentId: sharedSelectedAgentId });
    return () => closeSubscriptions();
  });

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
    loadingHistory = true;
    historyError = '';
    const sessionState = ensureSessionState(
      chatState,
      agent.id,
      agent.current_session_id,
    );
    try {
      const history = await rpc('chat.history', { agent_id: agent.id });
      loadHistory(sessionState, history.messages ?? []);
    } catch (error) {
      historyError = error.message;
      markSessionError(sessionState, error);
    } finally {
      loadingHistory = false;
    }
  };

  const handleSelectAgent = async (agentId) => {
    if (agentId === chatState.selectedAgentId) {
      return;
    }
    selectAgent(chatState, agentId);
    onAgentSelected?.(agentId);
    await loadCurrentHistory();
  };

  const handleNewSession = async () => {
    const agent = selectedAgent(chatState);
    if (!agent || newSessionBlocked) {
      return;
    }
    creatingSession = true;
    actionError = '';
    try {
      const session = await rpc('session.create', {
        agent_id: agent.id,
        make_current: true,
      });
      const updatedAgents = chatState.agents.map((candidate) =>
        candidate.id === agent.id
          ? { ...candidate, current_session_id: session.session_id }
          : candidate,
      );
      setAgents(chatState, updatedAgents);
      onAgentsChanged?.(updatedAgents);
      onAgentSelected?.(agent.id);
      ensureSessionState(chatState, agent.id, session.session_id);
      await loadCurrentHistory();
    } catch (error) {
      actionError = `${t('chat.sessionCreateError', 'New session could not be created.')} ${error.message}`;
    } finally {
      creatingSession = false;
    }
  };

  const handleSendMessage = async (content) => {
    const agent = selectedAgent(chatState);
    const sessionState = currentSessionState(chatState);
    if (!agent || !sessionState) {
      return;
    }
    if (isRunActive(sessionState)) {
      enqueueMessage(sessionState, content);
      return;
    }
    await sendStream(agent, sessionState, content);
  };

  const sendStream = async (agent, sessionState, content) => {
    actionError = '';
    try {
      const run = await rpc('chat.stream', {
        agent_id: agent.id,
        session_id: sessionState.sessionId,
        content,
      });
      startRun(sessionState, run);
      subscribeToRun(sessionState, run.sse_url, { afterSequence: 0 });
      return true;
    } catch (error) {
      actionError = `${t('chat.sendError', 'Message could not be sent.')} ${error.message}`;
      markSessionError(sessionState, error);
      return false;
    }
  };

  const subscribeToRun = (sessionState, sseUrl, options = {}) => {
    const existingSubscription = activeSubscriptions[sessionState.key];
    existingSubscription?.close();
    const afterSequence =
      options.afterSequence ?? highestRunEventSequence(sessionState);
    const subscription = subscribeRunEvents(
      sseUrl,
      {
        onEvent: ({ data }) => {
          const event = appendRunEvent(sessionState, data);
          if (event && event.type.startsWith('run_')) {
            sendNextQueuedMessage(sessionState);
          }
        },
        onError: (error) => {
          actionError = `${t('errors.streamClosed', 'The live stream closed before the run finished.')} ${error.message ?? ''}`;
        },
      },
      {
        afterSequence,
      },
    );
    activeSubscriptions[sessionState.key] = subscription;
  };

  const closeSubscriptions = () => {
    for (const subscription of Object.values(activeSubscriptions)) {
      subscription.close();
    }
    for (const key of Object.keys(activeSubscriptions)) {
      delete activeSubscriptions[key];
    }
  };

  const sendNextQueuedMessage = async (sessionState) => {
    if (isRunActive(sessionState)) {
      return;
    }
    const queuedMessage = dequeueMessage(sessionState);
    if (!queuedMessage) {
      return;
    }
    const agent = chatState.agents.find(
      (candidate) => candidate.id === sessionState.agentId,
    );
    if (agent) {
      const streamStarted = await sendStream(
        agent,
        sessionState,
        queuedMessage.content,
      );
      if (!streamStarted) {
        restoreDequeuedMessage(sessionState, queuedMessage);
      }
    } else {
      restoreDequeuedMessage(sessionState, queuedMessage);
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
      await rpc('chat.cancel', { run_id: runId });
    } catch (error) {
      actionError = `${t('chat.cancelError', 'Run could not be cancelled.')} ${error.message}`;
    } finally {
      cancellingRun = false;
    }
  };

  const handleRemoveQueuedMessage = (queuedMessageId) => {
    if (activeSessionState) {
      removeQueuedMessage(activeSessionState, queuedMessageId);
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
        class="btn-outline chat-refresh"
        onclick={loadAgents}
        disabled={chatState.loadingAgents}
      >
        {t('common.refresh', 'Refresh')}
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
    <div class="chat-view__surface">
      {#if loadingHistory || historyError || actionError}
        <div class="chat-view__notice-stack" aria-live="polite">
          {#if loadingHistory}
            <p class="chat-view__notice">
              {t('loading.history', 'Loading chat history…')}
            </p>
          {/if}
          {#if historyError}
            <p class="chat-view__error">
              {t('chat.historyLoadError', 'Chat history could not be loaded.')}
              {historyError}
            </p>
          {/if}
          {#if actionError}
            <p class="chat-view__error">{actionError}</p>
          {/if}
        </div>
      {/if}
      <div class="chat-view__timeline-shell">
        <ChatTimeline
          sessionState={activeSessionState}
          agentName={activeAgent.name}
        />
      </div>
      <div class="chat-view__footer-stack">
        <QueuedMessages
          queuedMessages={activeSessionState?.queue ?? []}
          onRemoveQueuedMessage={handleRemoveQueuedMessage}
        />
        <ChatComposer
          disabled={composerDisabled}
          isRunning={isRunActive(activeSessionState)}
          onSendMessage={handleSendMessage}
        />
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
  .chat-view__error {
    margin: 0;
    color: var(--text-med);
    font-size: 12.5px;
  }

  .chat-view__error {
    color: var(--red);
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

    .chat-refresh,
    .token-badge {
      display: none;
    }

    .chat-view__notice-stack {
      padding: 10px 14px;
    }
  }
</style>
