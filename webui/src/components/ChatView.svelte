<script>
  import ChatHeader from './chat/ChatHeader.svelte';
  import {
    isProjectSelected,
    isRunActive,
    agentActivityStatus,
    createChatController,
    createChatState,
    isSessionEmpty,
    selectAgent,
    sessionHasTerminalRun,
    setAgents,
    visibleTimelineItemsForRender,
  } from '../lib/chatState.js';
  import ProjectScanBanner from './chat/ProjectScanBanner.svelte';
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import Banner from './ui/Banner.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import Button from './ui/Button.svelte';
  import SessionListDrawer from './SessionListDrawer.svelte';
  import ChatTimeline from './ChatTimeline.svelte';
  import QueuedMessages from './QueuedMessages.svelte';
  import ChatComposer from './ChatComposer.svelte';
  import ComputerUseControl from './ComputerUseControl.svelte';
  import ChatActivityPanel from './chat/ChatActivityPanel.svelte';
  import { reflectionTaskRows } from '../lib/chatTimelinePresentation.js';
  import { onMount, untrack } from 'svelte';
  import { listConnections, listModels, subscribeRunEvents } from '$lib/api.js';
  import { getDraft } from '$lib/composerMemory.js';
  import { agentNeedsModel } from '$lib/onboarding.js';
  import { formatAgentAddress } from '$lib/agentAddress.js';
  import { createChatRunStream } from '../lib/chatRunStream.js';
  import { createChatViewTarget } from './chat/view/target.svelte.js';
  import { createChatViewNavigation } from './chat/view/navigation.svelte.js';
  import { createChatViewActions } from './chat/view/actions.svelte.js';
  import { createChatViewLayout } from './chat/view/layout.svelte.js';
  import './chat/view/chatView.css';

  let {
    active = true,
    workspaceActions,
    interactive = true,
    composerAvailable = true,
    preserveSessionSelection = false,
    initialSessionFilters = null,
    onSessionFiltersChange = () => {},
    onDisplayedSession = () => {},
    sharedAgents = [],
    sharedSelectedAgentId = '',
    // Chat reading-column width preference: 'comfortable' | 'wide' | 'full'.
    // Phase 3 seeds the persisted value from App; the default keeps the chat
    // self-contained (centered, capped at the comfortable measure).
    chatWidth = 'comfortable',
    // Normal renders Thinking and Tool rows inline. Compact groups each
    // contiguous work span behind a Working disclosure.
    chatWorkingMode = 'normal',
    // Project context (two-bar chat). `projects` feeds the project dropdown;
    // `selectedProjectId` is the chosen project (empty = Personal). App owns
    // the persisted selection; ChatView reflects it back through
    // `onProjectSelected` so the localStorage mirror stays current.
    projects = [],
    selectedProjectId = '',
    onProjectSelected = () => {},
    // The agent to restore inside the selected project on the initial mount.
    // Tri-state: null/omitted = nothing remembered (the initial load picks the
    // default), '' = an identity agent was active alongside an open project
    // (restore it, open no team member), or a team member's bare id. Honored
    // only on the first project load after mount (the reload restore); later
    // changes are reported up through `onProjectAgentSelected`, and a genuine
    // project switch jumps to the project default instead.
    sharedSelectedProjectAgentId = null,
    onProjectAgentSelected = () => {},
    onNavigateToProjects = () => {},
    agentsRefreshToken = 0,
    onAgentsChanged,
    onAgentSelected,
    navigateToSubAgent = () => {},
    pendingSessionNavigation = null,
    onSessionNavigation = () => {},
    runServerEvent = null,
    runServerEvents = [],
    // Bounded list of `bash_process_status_changed` accessor events; the
    // controller re-applies the whole list (idempotent merge by process id).
    backgroundBashStatusEvents = [],
    connectionSnapshot = null,
    // Bumped by App on `resource_changed(kind:"sessions")`; forwarded to the
    // session drawer so a new/switched session in another window appears in the
    // list. It deliberately does NOT switch the viewed conversation.
    sessionsRefreshToken = 0,
    // Bumped when Extension lifecycle changes alter the live slash-command catalog.
    commandsRefreshToken = 0,
    // Scope object of the latest `resource_changed(kind:"queue")` (a fresh
    // object per signal); re-syncs the matching held session's queue live.
    queueInvalidation = null,
    // App supplies the server-backed operational state. `null` means Settings
    // are still loading, so Chat does not guess which prerequisite is missing.
    hasConnectedProvider = true,
    // Invoked from the setup notices. App routes Provider setup directly to
    // Settings and Model assignment to the current Agent.
    onConnectProvider = () => {},
    onPickModel = () => {},
  } = $props();

  const chatState = $state(createChatState());
  const target = createChatViewTarget({
    get selectedProjectId() {
      return selectedProjectId;
    },
    get projects() {
      return projects;
    },
    get chatState() {
      return chatState;
    },
    get sharedSelectedProjectAgentId() {
      return sharedSelectedProjectAgentId;
    },
    get onProjectAgentSelected() {
      return onProjectAgentSelected;
    },
    get chatController() {
      return chatController;
    },
    get loadHistoryForSession() {
      return actions.loadHistoryForSession;
    },
    get onProjectSelected() {
      return onProjectSelected;
    },
    get navigation() {
      return navigation;
    },
    get layout() {
      return layout;
    },
    get actions() {
      return actions;
    },
  });
  const navigation = createChatViewNavigation({
    get sessionsRefreshToken() {
      return sessionsRefreshToken;
    },
    get chatController() {
      return chatController;
    },
    get pendingSessionNavigation() {
      return pendingSessionNavigation;
    },
    get onProjectAgentSelected() {
      return onProjectAgentSelected;
    },
    get onAgentSelected() {
      return onAgentSelected;
    },
    get chatState() {
      return chatState;
    },
    get loadCurrentHistory() {
      return actions.loadCurrentHistory;
    },
    get loadHistoryForSession() {
      return actions.loadHistoryForSession;
    },
    get onProjectSelected() {
      return onProjectSelected;
    },
    get lastSharedSelectedAgentId() {
      return lastSharedSelectedAgentId;
    },
    set lastSharedSelectedAgentId(value) {
      lastSharedSelectedAgentId = value;
    },
    get onSessionNavigation() {
      return onSessionNavigation;
    },
    get selectedProjectId() {
      return selectedProjectId;
    },
    get composerAvailable() {
      return composerAvailable;
    },
    get displayedSessionIsEmpty() {
      return displayedSessionIsEmpty;
    },
    get onAgentsChanged() {
      return onAgentsChanged;
    },
    get navigateToSubAgent() {
      return navigateToSubAgent;
    },
    get target() {
      return target;
    },
    get layout() {
      return layout;
    },
    get actions() {
      return actions;
    },
  });
  const actions = createChatViewActions({
    get chatState() {
      return chatState;
    },
    get onAgentSelected() {
      return onAgentSelected;
    },
    get chatController() {
      return chatController;
    },
    get target() {
      return target;
    },
    get layout() {
      return layout;
    },
    get navigation() {
      return navigation;
    },
  });
  const layout = createChatViewLayout({
    get active() {
      return active;
    },
    get interactive() {
      return interactive;
    },
    get chatState() {
      return chatState;
    },
    get target() {
      return target;
    },
  });

  let showSessionDrawer = $state(false);
  const componentId = $props.id();
  const chatTitleId = `${componentId}-title`;

  let sessionFilters = $state(untrack(() => initialSessionFilters));

  // The address + invalidation token the command/skill suggestions were last
  // loaded for. `undefined` is distinct from every real key, so the first effect
  // always loads; Agent changes and Extension lifecycle events both refresh it.
  let lastCommandsAddress = undefined;

  let activeTimelineItems = $derived(
    visibleTimelineItemsForRender(target.activeSessionState),
  );
  let identityAgentStatuses = $derived.by(() =>
    Object.fromEntries(
      chatState.agents.map((agent) => [
        agent.id,
        agentActivityStatus(chatState, agent.id, target.displayedSessionKey()),
      ]),
    ),
  );

  let sessionDrawerActivity = $derived.by(() =>
    Object.values(chatState.sessions).map((sessionState) => ({
      agent_address: sessionState.agentId,
      session_id: sessionState.sessionId,
      has_active_run: isRunActive(sessionState),
      has_unread_completion: sessionState.hasUnreadCompletion === true,
      latest_completion_run_id: sessionState.latestCompletionRunId || null,
      unread_run_id: sessionState.unreadRunId || null,
      unread_run_status: sessionState.unreadRunStatus || null,
      unread_run_at: sessionState.unreadRunAt || null,
    })),
  );

  let composerDisabled = $derived(
    !target.activeAgent || chatState.loadingHistory,
  );
  // Provider availability is the first prerequisite for every current Agent.
  // Do not infer it from Models: App supplies Settings' authoritative usable-
  // connection state. A model-less Identity Agent becomes the second step once
  // at least one Provider is connected; Project Agents resolve their Model
  // through Project defaults, and override stand-ins carry no Model field.
  let providerSetupMissing = $derived(
    Boolean(target.activeAgent) && hasConnectedProvider === false,
  );
  let agentModelMissing = $derived(
    Boolean(target.activeAgent) &&
      !target.projectAgentActive &&
      !target.activeAgent?.__overrideAddress &&
      hasConnectedProvider === true &&
      agentNeedsModel(target.activeAgent),
  );
  // The composer's per-session draft is keyed by the full displayed-session key;
  // its per-agent input history is keyed by the agent part alone (bare id for an
  // identity agent, `agent@projekt` for a project agent), so sessions of the
  // same agent share one history.
  let composerDraftKey = $derived(target.displayedSessionKey());
  let composerHistoryKey = $derived.by(() => {
    const separator = composerDraftKey.indexOf('::');
    return separator >= 0 ? composerDraftKey.slice(0, separator) : '';
  });
  // Each callback closes over the displayed address at the moment Svelte hands
  // it to the Composer. The Composer snapshots that function before any async
  // @-mention lookup, so navigation cannot redirect an older submit.
  let composerSendMessage = $derived.by(() => {
    const agent = target.activeAgent;
    const sessionState = target.activeSessionState;
    if (!agent || !sessionState) {
      return null;
    }
    return async (content, options = {}) =>
      await actions.sendStream(agent, sessionState, content, options);
  });
  let composerListFiles = $derived.by(() => {
    const agentId = target.activeSessionState?.agentId ?? '';
    if (!agentId) {
      return null;
    }
    return async () => await chatController.listFiles(agentId);
  });
  // The model catalog is global (not agent/session-scoped), so the loader is
  // always available. The composer fetches on demand when `/model ` is typed.
  let composerLoadModelCatalog = $derived(async () => {
    const [modelsResult, connectionsResult] = await Promise.all([
      listModels(),
      listConnections(),
    ]);
    return {
      models: Array.isArray(modelsResult?.models) ? modelsResult.models : [],
      connections: Array.isArray(connectionsResult?.connections)
        ? connectionsResult.connections
        : [],
    };
  });
  const displayedSessionIsEmpty = () =>
    isSessionEmpty(target.activeSessionState) &&
    getDraft(composerDraftKey).trim().length === 0 &&
    actions.transientCards.length === 0;
  let lastSharedSelectedAgentId = '';
  let lastSharedAgents = null;
  let lastAgentsRefreshToken = null;

  $effect(() => {
    if (sharedAgents.length > 0 && sharedAgents !== lastSharedAgents) {
      lastSharedAgents = sharedAgents;
      setAgents(chatState, sharedAgents, { preserveSessionSelection });
    }
  });

  let initialSharedAgentSyncDone = false;
  $effect(() => {
    const firstSync = !initialSharedAgentSyncDone;
    initialSharedAgentSyncDone = true;
    if (
      sharedSelectedAgentId &&
      sharedSelectedAgentId !== lastSharedSelectedAgentId &&
      sharedSelectedAgentId !== chatState.selectedAgentId &&
      chatState.agents.some((agent) => agent.id === sharedSelectedAgentId)
    ) {
      lastSharedSelectedAgentId = sharedSelectedAgentId;
      if (firstSync) {
        // Mount-time prop reconciliation, not user navigation: sync silently.
        // Routing this through handleSelectAgent would clear a just-restored
        // override and report a session navigation during a history restore —
        // the report then pushes a phantom entry over the restored one (the
        // mount-path echo hole). The mount's loadAgents already loads the
        // current history when the current session is displayed.
        selectAgent(chatState, sharedSelectedAgentId);
        return;
      }
      navigation.handleSelectAgent(sharedSelectedAgentId, {
        focusComposer: false,
      });
    }
  });

  // The controller owns reconnect deduplication and reconciliation; the View
  // only forwards the latest reactive input.
  $effect(() => {
    chatController.applyConnectionSnapshot(connectionSnapshot);
  });

  $effect(() => {
    if (lastAgentsRefreshToken === null) {
      lastAgentsRefreshToken = agentsRefreshToken;
      return;
    }
    if (agentsRefreshToken !== lastAgentsRefreshToken) {
      lastAgentsRefreshToken = agentsRefreshToken;
      loadAgents({ preferredAgentId: sharedSelectedAgentId, silent: true });
    }
  });

  $effect(() => {
    chatController.handleServerEvents(runServerEvent, runServerEvents);
  });

  $effect(() => {
    const events = backgroundBashStatusEvents;
    untrack(() => chatController.applyBackgroundBashStatusEvents(events));
  });

  let lastActivityRefreshKey = '';
  $effect(() => {
    const addresses = [
      ...chatState.agents.map((agent) => agent.id),
      ...target.projectTeam.map((member) =>
        formatAgentAddress(member.agent_id, selectedProjectId),
      ),
    ];
    const reconnectRevision = connectionSnapshot
      ? `${connectionSnapshot.epoch ?? ''}:${connectionSnapshot.last_sequence ?? ''}`
      : '';
    const refreshKey = `${sessionsRefreshToken}:${reconnectRevision}:${addresses.join('|')}`;
    if (refreshKey === lastActivityRefreshKey) {
      return;
    }
    lastActivityRefreshKey = refreshKey;
    void chatController.refreshAgentActivity(addresses);
  });

  $effect(() => {
    const sessionState = target.activeSessionState;
    const unreadRunId = sessionState?.unreadRunId ?? '';
    if (
      !active ||
      !unreadRunId ||
      sessionState.markReadFailedRunId === unreadRunId ||
      !target.isDisplayedSession(
        sessionState.agentId,
        sessionState.sessionId,
      ) ||
      !sessionHasTerminalRun(sessionState, unreadRunId)
    ) {
      return;
    }
    void chatController.markSessionCompletionRead(sessionState);
  });

  // Re-sync a held session's queue when another window mutates it. App forwards
  // the generic `resource_changed(kind:"queue")` signal as a scope object (a
  // fresh object per signal, so this re-fires even for a repeat scope). Only
  // sessions we actually hold are synced — the queue RPC keys on the bare agent
  // id, so match the scope's bare agent id + session id against each held
  // session. The viewed conversation is never switched.
  $effect(() => {
    chatController.applyQueueInvalidation(queueInvalidation);
  });

  onMount(() => {
    loadAgents({ preferredAgentId: sharedSelectedAgentId });
    const onVisibilityChange = () => {
      if (document.visibilityState !== 'visible') {
        return;
      }
      const sessionState = target.activeSessionState;
      if (
        !sessionState?.currentRun?.runId ||
        sessionState.status !== 'running'
      ) {
        return;
      }
      if (sessionState.streamError) {
        void chatController.reconcileRunSession(
          sessionState,
          sessionState.currentRun.runId,
        );
      }
    };
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => {
      document.removeEventListener('visibilitychange', onVisibilityChange);
      chatController.destroy();
    };
  });

  // Reload command/skill suggestions whenever the active address or live command
  // catalog changes. The token does not disturb the draft or active selection.
  $effect(() => {
    const { agentAddress } = target.activeAddressing();
    const commandsKey = `${commandsRefreshToken}:${agentAddress}`;
    if (commandsKey === lastCommandsAddress) {
      return;
    }
    lastCommandsAddress = commandsKey;
    loadCommands(agentAddress);
  });

  const loadCommands = (agentAddress) =>
    chatController.loadCommands(agentAddress);

  const loadAgents = (options = {}) => chatController.loadAgents(options);

  // Exposed for focused controller-boundary tests. Normal rows are reconciled
  // in one batch from the displayed Timeline below.
  export async function verifySubAgentStatus(
    agentId,
    sessionId,
    runId,
    queueItemId = '',
  ) {
    await chatController.verifySubAgentStatus({
      agentId,
      sessionId,
      runId,
      queueItemId,
      projectId: target.displayedSessionProjectId(),
    });
  }

  // Exposed for tests (mirrors `verifySubAgentStatus`): drives the per-row
  // sub-agent cancel exactly as the timeline button's callback does.
  export async function cancelSubAgent(tool) {
    await actions.handleCancelSubAgent({ tool });
  }

  let chatController;
  const runStream = createChatRunStream({
    chatState,
    subscribeRunEvents,
    syncSessionQueue: (sessionState) =>
      chatController.syncSessionQueue(sessionState),
    reconcileRunSession: (sessionState, expectedRunId) =>
      chatController.reconcileRunSession(sessionState, expectedRunId),
    isDisplayedSession: target.isDisplayedSession,
    updateSubAgentRunStatuses: (updates, options) =>
      chatController.applySubAgentStatusUpdates(updates, options),
  });
  chatController = createChatController({
    chatState,
    preserveSessionSelection: untrack(() => preserveSessionSelection),
    runStream,
    translate: t,
    isDisplayedSession: target.isDisplayedSession,
    shouldLoadCurrentHistory: () =>
      !navigation.viewingSessionId && !target.projectAgentActive,
    onAgentsChanged: (agents) => onAgentsChanged?.(agents),
    onAgentSelected: (agentId) => onAgentSelected?.(agentId),
    onRestartQueueDiscarded: (count) => {
      actions.showChatToast(
        count === 1
          ? t(
              'queue.restartDiscardedOne',
              '1 queued message was discarded because the server restarted.',
            )
          : t(
              'queue.restartDiscardedMany',
              '{count} queued messages were discarded because the server restarted.',
              { count },
            ),
      );
    },
  });

  $effect(() => {
    chatController.reconcileSubAgentRows(activeTimelineItems, {
      projectId: target.displayedSessionProjectId(),
    });
  });

  $effect(() => {
    const session = target.activeSessionState;
    const agent = target.activeAgent;
    const selection = {
      agentId: session?.agentId || target.activeAgentAddress,
      sessionId: session?.sessionId || agent?.current_session_id || '',
    };
    untrack(() => onDisplayedSession(selection));
  });
</script>

<section
  class="view view-chat active chat-view"
  data-chat-width={chatWidth}
  aria-labelledby={chatTitleId}
  hidden={!active}
>
  <ChatHeader
    titleId={chatTitleId}
    agents={chatState.agents}
    agentStatuses={identityAgentStatuses}
    selectedAgentId={target.displayedIdentityAgentId}
    loadingAgents={chatState.loadingAgents}
    {projects}
    {selectedProjectId}
    onSelectProject={target.handleSelectProject}
    onSelectAgent={navigation.handleSelectAgent}
  />

  {#if isProjectSelected(selectedProjectId)}
    <ProjectScanBanner report={target.projectReport} {onNavigateToProjects} />
    <!-- Second bar: the project's scanned team, shown only while a project is
         chosen in the header picker. Left-aligned like the identity agent bar
         above and prefixed with the project name so the team's ownership is
         clear. Empty team renders an empty bar (no error); a config agent is
         selected and chatted just like an identity agent. -->
    <div
      class="chat-view__project-team"
      aria-label={t('chat.project.teamLabel', 'Project team')}
    >
      <div class="chat-view__project-team-inner">
        <span
          class="chat-view__project-team-name"
          use:tooltip={t(
            'chat.project.teamBarHint',
            'Agents discovered in this project’s repository.',
          )}>{target.selectedProjectName}</span
        >
        {#if target.loadingProjectTeam}
          <span class="chat-view__project-team-empty">
            {t('loading.agents', 'Loading agents…')}
          </span>
        {:else if target.projectScanError}
          <span class="chat-view__project-team-error"
            >{target.projectScanError}</span
          >
        {:else if target.projectTeam.length === 0}
          <span class="chat-view__project-team-empty">
            {t('chat.project.teamEmpty', 'This project has no agents yet.')}
          </span>
        {:else}
          {#each target.projectTeam as member (member.agent_id)}
            {@const memberName = member.display_name || member.agent_id}
            {@const memberStatus =
              target.projectAgentStatuses[member.agent_id] ?? 'idle'}
            {@const memberActivityLabel =
              memberStatus === 'running'
                ? t('chat.agentActivity.running', '{name}: Running', {
                    name: memberName,
                  })
                : memberStatus === 'unread'
                  ? t('chat.agentActivity.unread', '{name}: Unread result', {
                      name: memberName,
                    })
                  : t('chat.agentActivity.idle', '{name}: Idle', {
                      name: memberName,
                    })}
            {@const memberActivityTooltip = target.agentActivityTooltip(
              memberActivityLabel,
              member.effective?.model?.value,
            )}
            <button
              type="button"
              class="agent-tab chat-view__project-tab"
              class:active={member.agent_id === target.displayedProjectAgentId}
              aria-label={memberActivityLabel}
              use:tooltip={memberActivityTooltip}
              onclick={() => target.handleSelectProjectAgent(member.agent_id)}
            >
              <span
                class="tab-indicator tab-indicator--{memberStatus}"
                aria-hidden="true"
              ></span>
              <span>{memberName}</span>
            </button>
          {/each}
        {/if}
      </div>
    </div>
  {/if}

  {#if chatState.loadingAgents}
    <Banner variant="neutral" class="chat-view__state-banner">
      {t('loading.agents', 'Loading agents…')}
    </Banner>
  {:else if chatState.agents.length === 0}
    <EmptyState
      fill
      title={t('chat.noAgents', 'No agents are available yet.')}
      description={chatState.agentsError}
    />
  {:else if !target.activeAgent}
    <EmptyState
      fill
      title={t('chat.noAgentSelected', 'Choose an agent to start chatting.')}
    />
  {:else}
    <div class="chat-view__content-shell">
      <div
        class="chat-view__surface"
        style={`--chat-overlay-height: ${layout.footerOverlayHeight}px; --chat-scrollbar-width: ${layout.chatScrollbarWidth}px`}
      >
        <div class="chat-view__session-bar">
          <Button
            variant="secondary"
            class={`chat-view__session-toggle${
              showSessionDrawer ? ' chat-view__session-toggle--active' : ''
            }`}
            disabled={!target.activeAgent}
            onClick={() => {
              showSessionDrawer = !showSessionDrawer;
            }}
          >
            {t('sessions.title', 'Sessions')}
          </Button>
          <span class="chat-view__session-bar-divider" aria-hidden="true"
          ></span>
          <Button
            variant="secondary"
            icon
            class="chat-view__new-session-fab"
            ariaLabel={t('chat.newSession', 'New session')}
            tooltip={t('chat.newSession', 'New session')}
            disabled={chatState.loadingHistory}
            loading={navigation.creatingSession}
            onClick={navigation.handleNewSession}
          >
            <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
              <path d="M12 5v14M5 12h14" />
            </svg>
          </Button>
          {#if workspaceActions}
            <span class="chat-view__session-bar-divider" aria-hidden="true"
            ></span>
            {@render workspaceActions()}
          {/if}
        </div>
        {#if showSessionDrawer}
          <SessionListDrawer
            agentId={target.activeAgentAddress}
            currentSessionId={navigation.viewingSessionId ||
              target.activeAgent.current_session_id}
            reloadToken={sessionsRefreshToken}
            agents={target.sessionDrawerAgents}
            liveActivity={sessionDrawerActivity}
            initialFilters={sessionFilters}
            onFiltersChange={(next) => {
              sessionFilters = next;
              onSessionFiltersChange(next);
            }}
            onSessionSelected={navigation.handleSessionSelected}
            onSessionDeleted={navigation.handleSessionDeleted}
          />
        {/if}
        {#if chatState.historyError || chatState.actionError || chatState.commandsError || target.activeSessionState?.actionError || target.activeSessionState?.streamError || target.activeSessionState?.error}
          <div class="chat-view__notice-stack" aria-live="polite">
            <div class="chat-view__measure chat-view__notice-inner">
              {#if chatState.historyError}
                <Banner variant="error">
                  {t(
                    'chat.historyLoadError',
                    'Chat history could not be loaded.',
                  )}
                  {chatState.historyError}
                </Banner>
              {/if}
              {#if chatState.actionError}
                <Banner variant="error">{chatState.actionError}</Banner>
              {/if}
              {#if chatState.commandsError}
                <Banner variant="error">{chatState.commandsError}</Banner>
              {/if}
              {#if target.activeSessionState?.actionError}
                <Banner variant="error"
                  >{target.activeSessionState.actionError}</Banner
                >
              {/if}
              {#if target.activeSessionState?.streamError}
                <Banner variant="warn"
                  >{target.activeSessionState.streamError}</Banner
                >
              {/if}
              {#if target.activeSessionState?.error}
                <Banner variant="error">
                  {t('chat.runError', 'Run failed.')}
                  {target.activeSessionState.error}
                </Banner>
              {/if}
            </div>
          </div>
        {/if}
        <div class="chat-view__timeline-shell">
          <ChatTimeline
            sessionState={target.activeSessionState}
            agentName={target.activeAgent.name}
            {chatWorkingMode}
            loadingHistory={layout.historyLoadingFeedbackVisible}
            transientCards={actions.transientCards}
            submittedTurnScrollKey={actions.submittedTurnScrollKey}
            bottomOverlayHeight={layout.footerOverlayHeight}
            onScrollbarWidthChange={(width) => {
              layout.chatScrollbarWidth = width;
            }}
            followSessionRequest={navigation.subAgentLinkFollowRequest}
            hasOlderHistory={target.activeSessionState?.hasOlderHistory ===
              true}
            loadingOlderHistory={target.activeSessionState
              ?.loadingOlderHistory === true}
            subAgentStatuses={chatState.subAgentStatuses}
            subAgentResults={chatState.subAgentResults}
            backgroundBashStatuses={target.activeSessionState
              ?.backgroundBashStatuses}
            backgroundBashProcesses={chatState.backgroundBashProcesses}
            onLoadOlder={actions.loadOlderHistory}
            onNavigateToSubAgent={navigation.handleNavigateToSubAgentLink}
            onCancelToolCall={actions.handleCancelToolCall}
            onBackgroundToolCall={(target) =>
              chatController.controlRun(
                target.activeSessionState,
                'background_tool',
                target,
              )}
            onCancelSubAgent={actions.handleCancelSubAgent}
            messageEditingDisabled={chatState.loadingHistory ||
              isRunActive(target.activeSessionState) ||
              (target.activeSessionState?.queue?.length ?? 0) > 0}
            onEditMessage={actions.handleEditMessage}
          />
        </div>
        <div
          class="chat-view__footer-stack"
          bind:this={layout.footerStackElement}
        >
          {#if navigation.subAgentSessionActive}
            <Banner
              variant="info"
              class="chat-view__footer-banner"
              aria-live="polite"
            >
              <div class="chat-view__footer-banner-copy">
                <p class="chat-view__footer-banner-title">
                  {t(
                    'chat.subagentSessionNotice',
                    'Viewing a sub-agent session',
                  )}
                </p>
                <p class="chat-view__footer-banner-hint">
                  {navigation.subAgentParentTarget
                    ? t(
                        'chat.subagentSessionParentHint',
                        'Messages here continue this sub-agent session. Return to the parent session when you are done.',
                      )
                    : t(
                        'chat.subagentSessionHint',
                        'Messages here continue this sub-agent session. Return to the current agent session when you are done.',
                      )}
                </p>
              </div>
              <Button
                variant="secondary"
                class="chat-view__subagent-session-return"
                disabled={chatState.loadingHistory}
                onClick={navigation.handleReturnToCurrentSession}
              >
                {navigation.subAgentParentTarget
                  ? t('chat.returnToParentSession', 'Return to parent session')
                  : t(
                      'chat.returnToCurrentSession',
                      'Return to current session',
                    )}
              </Button>
            </Banner>
          {/if}
          {#if providerSetupMissing}
            <Banner
              variant="info"
              class="chat-view__footer-banner"
              aria-live="polite"
            >
              <div class="chat-view__footer-banner-copy">
                <p class="chat-view__footer-banner-title">
                  {t('chat.noProvider.title', 'Connect a provider to start')}
                </p>
                <p class="chat-view__footer-banner-hint">
                  {t(
                    'chat.noProvider.hint',
                    'No provider is connected yet. Connect one before choosing a model.',
                  )}
                </p>
              </div>
              <Button
                variant="primary"
                class="chat-view__no-provider-action"
                onClick={onConnectProvider}
              >
                {t('chat.noProvider.action', 'Connect a provider')}
              </Button>
            </Banner>
          {:else if agentModelMissing}
            <Banner
              variant="info"
              class="chat-view__footer-banner"
              aria-live="polite"
            >
              <div class="chat-view__footer-banner-copy">
                <p class="chat-view__footer-banner-title">
                  {t('chat.noModel.title', 'Pick a model to start')}
                </p>
                <p class="chat-view__footer-banner-hint">
                  {t(
                    'chat.noModel.hint',
                    'This agent has no model yet. Choose one to send messages.',
                  )}
                </p>
              </div>
              <Button
                variant="primary"
                class="chat-view__no-model-action"
                onClick={onPickModel}
              >
                {t('chat.noModel.action', 'Choose a model')}
              </Button>
            </Banner>
          {/if}
          <div class="chat-view__measure">
            <QueuedMessages
              queuedMessages={target.activeSessionState?.queue ?? []}
              onRemoveQueuedMessage={actions.handleRemoveQueuedMessage}
              onEditQueuedMessage={actions.handleEditQueuedMessage}
            />
          </div>
          <div class="chat-view__composer-shell">
            {#if actions.chatToast}
              <div
                class="chat-view__command-toast"
                role="status"
                aria-live="polite"
              >
                <p class="chat-view__command-toast-message">
                  {actions.chatToast}
                </p>
              </div>
            {/if}
            {#if composerAvailable}
              <ChatComposer
                disabled={composerDisabled}
                isRunning={isRunActive(target.activeSessionState)}
                cancelling={chatState.cancellingRun}
                draftKey={composerDraftKey}
                historyKey={composerHistoryKey}
                focusRequest={layout.composerFocusRequest}
                availableSkills={chatState.availableSkills}
                contextUsage={target.activeSessionState?.contextUsage}
                compactionState={target.activeSessionState?.currentRun
                  ?.status === 'running'
                  ? (target.activeSessionState.currentRun.controls
                      ?.compaction ?? 'unavailable')
                  : 'unavailable'}
                compactionSubmitting={Boolean(
                  target.activeSessionState?.pendingRunControls?.compact,
                )}
                onForceCompaction={() =>
                  chatController.controlRun(
                    target.activeSessionState,
                    'compact',
                  )}
                contextWindow={target.activeAgent?.context_window}
                usage={target.activeSessionState?.usage}
                sessionUsage={target.activeSessionState?.sessionUsage}
                onSendMessage={composerSendMessage}
                onCancelRun={actions.handleCancelRun}
                onTranscriptionError={actions.handleTranscriptionError}
                onListFiles={composerListFiles}
                onLoadModelCatalog={composerLoadModelCatalog}
              >
                {#snippet computerControl()}
                  <ComputerUseControl onError={actions.showChatToast} />
                {/snippet}
              </ChatComposer>
            {:else}
              <Banner class="chat-view__footer-banner">
                {t(
                  'split.sameSession',
                  'This Session is open in the other area. Choose another Session or start a new one to chat here.',
                )}
              </Banner>
            {/if}
          </div>
        </div>
      </div>
      <ChatActivityPanel
        timelineItems={activeTimelineItems}
        subAgentStatuses={chatState.subAgentStatuses}
        backgroundBashStatuses={target.activeSessionState
          ?.backgroundBashStatuses}
        backgroundBashProcesses={chatState.backgroundBashProcesses}
        reflectionTasks={reflectionTaskRows(target.activeSessionState)}
        parentSession={navigation.sessionParentLink}
        onNavigateToSubAgent={navigation.handleNavigateToSubAgentLink}
        onNavigateToParentSession={navigation.navigateToParentSession}
        onOpenReflection={navigation.handleOpenReflection}
        onCancelSubAgent={actions.handleCancelSubAgent}
        onCancelBackgroundProcess={actions.handleCancelBackgroundProcess}
      />
    </div>
  {/if}
</section>
