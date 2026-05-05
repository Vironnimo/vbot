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
  let activeRunStatus = $derived(
    activeSessionState && isRunActive(activeSessionState)
      ? t('chat.runStatus.running', 'Running')
      : t('chat.runStatus.idle', 'Idle'),
  );
  let lastSharedSelectedAgentId = $state('');
  let lastAgentsRefreshToken = $state(agentsRefreshToken);

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
      subscribeToRun(sessionState, run.sse_url);
      return true;
    } catch (error) {
      actionError = `${t('chat.sendError', 'Message could not be sent.')} ${error.message}`;
      markSessionError(sessionState, error);
      return false;
    }
  };

  const subscribeToRun = (sessionState, sseUrl) => {
    const existingSubscription = activeSubscriptions[sessionState.key];
    existingSubscription?.close();
    const subscription = subscribeRunEvents(sseUrl, {
      onEvent: ({ data }) => {
        const event = appendRunEvent(sessionState, data);
        if (event && event.type.startsWith('run_')) {
          sendNextQueuedMessage(sessionState);
        }
      },
      onError: (error) => {
        actionError = `${t('errors.streamClosed', 'The live stream closed before the run finished.')} ${error.message ?? ''}`;
      },
    });
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

<section class="chat-view" aria-labelledby="chat-title">
  <header class="chat-view__header">
    <div
      class="chat-view__agent-tabs"
      aria-label={t('chat.selectAgent', 'Select agent')}
    >
      {#if chatState.loadingAgents}
        <span class="chat-view__status-line"
          >{t('loading.agents', 'Loading agents…')}</span
        >
      {:else if chatState.agents.length === 0}
        <span class="chat-view__status-line"
          >{t('chat.noAgents', 'No agents are available yet.')}</span
        >
      {:else}
        {#each chatState.agents as agent (agent.id)}
          <button
            type="button"
            class:chat-view__agent-tab--active={agent.id ===
              chatState.selectedAgentId}
            class="chat-view__agent-tab"
            aria-current={agent.id === chatState.selectedAgentId
              ? 'true'
              : undefined}
            onclick={() => handleSelectAgent(agent.id)}
          >
            <span class="chat-view__tab-indicator" aria-hidden="true"></span>
            <span>{agent.name}</span>
          </button>
        {/each}
      {/if}
    </div>

    <h2 id="chat-title" class="chat-view__title">{t('chat.title', 'Chat')}</h2>

    <div class="chat-view__header-actions">
      {#if activeAgent?.model}
        <span class="chat-view__model-badge">{activeAgent.model}</span>
      {/if}
      <span
        class:chat-view__run-badge--running={activeSessionState &&
          isRunActive(activeSessionState)}
        class="chat-view__run-badge"
      >
        {activeRunStatus}
      </span>
      <button
        class="btn-secondary chat-view__refresh-button"
        type="button"
        onclick={loadAgents}
        disabled={chatState.loadingAgents}
      >
        {t('common.refresh', 'Refresh')}
      </button>
      {#if activeSessionState && isRunActive(activeSessionState)}
        <button
          class="btn-secondary btn-danger"
          type="button"
          disabled={cancellingRun}
          onclick={handleCancelRun}
        >
          {cancellingRun
            ? t('cancel.cancelling', 'Cancelling run…')
            : t('chat.cancelRun', 'Cancel run')}
        </button>
      {/if}
      <button
        class="btn-new"
        type="button"
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
    <p class="chat-view__notice">{t('loading.agents', 'Loading agents…')}</p>
  {:else if chatState.agents.length === 0}
    <p class="chat-view__notice">
      {t('chat.noAgents', 'No agents are available yet.')}
    </p>
  {:else if !activeAgent}
    <p class="chat-view__notice">
      {t('chat.noAgentSelected', 'Choose an agent to start chatting.')}
    </p>
  {:else}
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

    <div class="chat-view__session-strip">
      <span class="chat-view__session-label"
        >{t('chat.session', 'Session')}</span
      >
      <span class="chat-view__session-value">
        {activeAgent.current_session_id ?? t('common.unknown', 'Unknown')}
      </span>
    </div>

    <div class="chat-view__surface">
      <ChatTimeline sessionState={activeSessionState} />
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
  {/if}
</section>

<style>
  .chat-view {
    display: flex;
    width: 100%;
    height: 100%;
    min-height: 0;
    flex: 1;
    flex-direction: column;
    overflow: hidden;
    background: var(--bg);
  }

  .chat-view__header {
    display: flex;
    height: 50px;
    flex-shrink: 0;
    align-items: center;
    gap: var(--space-sm);
    padding: 0 var(--space-lg);
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }

  .chat-view__agent-tabs {
    display: flex;
    height: 100%;
    min-width: 0;
    flex: 1;
    align-items: stretch;
    gap: 2px;
    overflow-x: auto;
    scrollbar-width: none;
  }

  .chat-view__agent-tabs::-webkit-scrollbar {
    display: none;
  }

  .chat-view__agent-tab {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 0 14px;
    border-bottom: 2px solid transparent;
    background: transparent;
    color: var(--text-lo);
    font-family: var(--font-ui);
    font-size: 13px;
    font-weight: 500;
    white-space: nowrap;
    transition:
      border-color 150ms ease,
      color 150ms ease;
  }

  .chat-view__agent-tab:hover {
    color: var(--text-med);
  }

  .chat-view__agent-tab--active,
  .chat-view__agent-tab--active:hover {
    border-bottom-color: var(--accent);
    color: var(--accent);
  }

  .chat-view__tab-indicator {
    width: 5px;
    height: 5px;
    flex-shrink: 0;
    border-radius: 50%;
    background: var(--text-lo);
    transition: background 150ms ease;
  }

  .chat-view__agent-tab--active .chat-view__tab-indicator {
    background: var(--green);
  }

  .chat-view__title {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
  }

  .chat-view__header-actions {
    display: flex;
    flex-shrink: 0;
    align-items: center;
    gap: 10px;
  }

  .chat-view__model-badge,
  .chat-view__run-badge,
  .chat-view__session-value {
    overflow: hidden;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 11px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .chat-view__model-badge,
  .chat-view__run-badge {
    max-width: 220px;
    padding: 3px 8px;
    border: 1px solid var(--border);
    border-radius: var(--r-sm);
    background: var(--surface-2);
  }

  .chat-view__run-badge--running {
    color: var(--amber);
  }

  .chat-view__status-line {
    align-self: center;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 11px;
  }

  .chat-view__notice,
  .chat-view__error {
    margin: 0;
    padding: var(--space-md) var(--space-lg);
    border-bottom: 1px solid var(--border);
    background: var(--surface);
    color: var(--text-med);
    font-size: 12.5px;
  }

  .chat-view__error {
    color: var(--red);
  }

  .chat-view__session-strip {
    display: flex;
    flex-shrink: 0;
    align-items: center;
    gap: var(--space-sm);
    padding: 7px var(--space-lg);
    border-bottom: 1px solid var(--border);
    background: var(--bg);
  }

  .chat-view__session-label {
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 0.07em;
    text-transform: uppercase;
  }

  .chat-view__surface {
    display: flex;
    min-height: 0;
    flex: 1;
    flex-direction: column;
    overflow: hidden;
  }

  .chat-view svg {
    width: 12px;
    height: 12px;
  }

  @media (max-width: 760px) {
    .chat-view__model-badge,
    .chat-view__refresh-button {
      display: none;
    }
  }
</style>
