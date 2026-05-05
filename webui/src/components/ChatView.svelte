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
    <div>
      <p class="chat-view__eyebrow">{t('app.ready', 'Ready')}</p>
      <h2 id="chat-title">{t('chat.title', 'Chat')}</h2>
      <p>
        {t('chat.subtitle', 'Select an agent and continue its active session.')}
      </p>
    </div>
    <button
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
      {creatingSession
        ? t('common.loading', 'Loading…')
        : t('chat.newSession', 'New Session')}
    </button>
  </header>

  <div class="chat-view__agent-bar">
    <label for="chat-agent-select"
      >{t('chat.selectAgent', 'Select agent')}</label
    >
    <select
      id="chat-agent-select"
      disabled={chatState.loadingAgents || chatState.agents.length === 0}
      value={chatState.selectedAgentId}
      onchange={(event) => handleSelectAgent(event.currentTarget.value)}
    >
      {#each chatState.agents as agent (agent.id)}
        <option value={agent.id}>{agent.name}</option>
      {/each}
    </select>
    <button
      type="button"
      onclick={loadAgents}
      disabled={chatState.loadingAgents}
    >
      {t('common.refresh', 'Refresh')}
    </button>
    {#if activeSessionState && isRunActive(activeSessionState)}
      <button type="button" disabled={cancellingRun} onclick={handleCancelRun}>
        {cancellingRun
          ? t('cancel.cancelling', 'Cancelling run…')
          : t('chat.cancelRun', 'Cancel run')}
      </button>
    {/if}
  </div>

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
    display: grid;
    width: min(100%, 68rem);
    max-height: calc(100vh - 4rem);
    overflow: hidden;
    border: 1px solid var(--color-border);
    border-radius: var(--radius-lg);
    background:
      linear-gradient(135deg, rgba(33, 29, 23, 0.96), rgba(20, 23, 27, 0.9)),
      var(--color-panel);
    box-shadow: 0 2rem 5rem rgba(0, 0, 0, 0.38);
  }

  .chat-view__header,
  .chat-view__agent-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-md);
    padding: var(--space-lg);
  }

  .chat-view__header {
    border-bottom: 1px solid var(--color-border);
  }

  .chat-view__agent-bar {
    justify-content: flex-start;
    border-bottom: 1px solid rgba(240, 164, 58, 0.14);
    background: rgba(21, 19, 15, 0.36);
  }

  .chat-view__eyebrow {
    margin: 0 0 var(--space-xs);
    color: var(--color-accent);
    font-family: 'Trebuchet MS', Verdana, sans-serif;
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  .chat-view h2,
  .chat-view p {
    margin: 0;
  }

  .chat-view h2 {
    color: var(--color-text);
    font-size: clamp(2.4rem, 8vw, 5rem);
    line-height: 0.95;
  }

  .chat-view p,
  .chat-view label,
  .chat-view select,
  .chat-view button {
    font-family: 'Trebuchet MS', Verdana, sans-serif;
  }

  .chat-view p,
  .chat-view label {
    color: var(--color-muted);
  }

  .chat-view select,
  .chat-view button {
    border: 1px solid var(--color-border);
    border-radius: var(--radius-md);
    padding: 0.75rem 0.9rem;
    color: var(--color-text);
    background: rgba(21, 19, 15, 0.82);
  }

  .chat-view button {
    cursor: pointer;
  }

  .chat-view button:disabled,
  .chat-view select:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }

  .chat-view__notice,
  .chat-view__error {
    padding: var(--space-md) var(--space-lg);
  }

  .chat-view__error {
    color: var(--color-accent-strong) !important;
  }

  .chat-view__surface {
    display: grid;
    min-height: 0;
  }

  @media (max-width: 760px) {
    .chat-view {
      max-height: none;
    }

    .chat-view__header,
    .chat-view__agent-bar {
      align-items: stretch;
      flex-direction: column;
    }
  }
</style>
