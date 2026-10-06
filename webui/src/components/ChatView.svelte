<script>
  import ChatHeader from './chat/ChatHeader.svelte';
  import {
    contextCompactionState,
    isRunActive,
    agentActivityStatus,
    agentUnreadResults,
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
  import Banner from './ui/Banner.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import Button from './ui/Button.svelte';
  import SessionListDrawer from './SessionListDrawer.svelte';
  import ChatTimeline from './ChatTimeline.svelte';
  import QueuedMessages from './QueuedMessages.svelte';
  import ChatComposer from './ChatComposer.svelte';
  import ComposerSessionSettings from './chat/ComposerSessionSettings.svelte';
  import ComputerUseControl from './ComputerUseControl.svelte';
  import ChatActivityPanel from './chat/ChatActivityPanel.svelte';
  import {
    backgroundTasks,
    reflectionTaskRows,
  } from '../lib/chatTimelinePresentation.js';
  import { onMount, untrack } from 'svelte';
  import { listConnections, listModels, subscribeRunEvents } from '$lib/api.js';
  import { getDraft } from '$lib/composerMemory.js';
  import { agentNeedsModel } from '$lib/onboarding.js';
  import { formatAgentAddress, parseAgentAddress } from '$lib/agentAddress.js';
  import { createChatRunStream } from '../lib/chatRunStream.js';
  import { createChatViewTarget } from './chat/view/target.svelte.js';
  import { createChatViewNavigation } from './chat/view/navigation.svelte.js';
  import { createChatViewActions } from './chat/view/actions.svelte.js';
  import { createChatViewLayout } from './chat/view/layout.svelte.js';
  import { createSessionChanges } from './chat/view/sessionChanges.svelte.js';
  import { createSessionSettings } from './chat/view/sessionSettings.svelte.js';
  import './chat/view/chatView.css';

  let {
    active = true,
    workspaceActions,
    interactive = true,
    composerAvailable = true,
    preserveSessionSelection = false,
    // Keeps this Chat area's unsaved drafts apart from another area's: each
    // area remembers its own draft per Agent.
    draftScope = 0,
    initialSessionFilters = null,
    onSessionFiltersChange,
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
    // Project context. `projects` (with cached Team scans) feeds the Agent
    // picker's Project groups; `selectedProjectId` is the Project of the
    // active Project Agent (empty = an Identity Agent is active). App owns
    // the persisted selection; ChatView reflects it back through
    // `onProjectSelected` so the localStorage mirror stays current.
    projects = [],
    // Whether `projects` has been read: until then a Project missing from it
    // is unknown, not unregistered.
    projectsLoaded = false,
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
    // Workspace coordination between retained Chat areas: this area reports
    // Sessions deleted from its drawer, and releases its own pointer to a
    // Session the other area deleted (`{ requestId, deletedSessionId,
    // nextSessionId, agentAddress }`).
    onSessionDeleted = () => {},
    siblingSessionDeletion = null,
    runServerEvent = null,
    runServerEvents = [],
    // App's bounded live map of handed-off shell command statuses by terminal
    // id (`command_status_changed`); the controller re-applies the whole map.
    commandStatuses = {},
    connectionSnapshot = null,
    // App's live list of the Runs active now (the snapshot's list advanced by
    // later lifecycle events). An owner mounted after the app connected starts
    // from it instead of replaying the retained snapshot and event window.
    activeRuns = null,
    // Bumped by App when Session-list continuity is uncertain (replay gap or
    // server restart): activity, the drawer, and the Parent Session link
    // re-read everything. It deliberately does NOT switch the viewed
    // conversation.
    sessionsRefreshToken = 0,
    // App's bounded window of `resource_changed(kind:"sessions")` scopes
    // (`{ id, scope }`); each consumer refreshes only what a scope names.
    sessionInvalidations = [],
    // Bumped when Extension lifecycle changes alter the live slash-command catalog.
    commandsRefreshToken = 0,
    // App's Extension invalidations (`{ owner, change }`); the composer's
    // Computer Use control refreshes its status from them.
    subscribeExtensionInvalidations = null,
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
    get draftScope() {
      return draftScope;
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
    get active() {
      return active;
    },
    get sessionsRefreshToken() {
      return sessionsRefreshToken;
    },
    get sessionInvalidations() {
      return sessionInvalidations;
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
      return reportAgentSelected;
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
    get onSessionDeleted() {
      return onSessionDeleted;
    },
    get siblingSessionDeletion() {
      return siblingSessionDeletion;
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
      return reportAgentSelected;
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
    get sessionSettings() {
      return sessionSettings;
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

  const sessionChanges = createSessionChanges({
    get target() {
      return target;
    },
    get chatController() {
      return chatController;
    },
    get sessionsRefreshToken() {
      return sessionsRefreshToken;
    },
    get timelineItems() {
      return activeTimelineItems;
    },
  });

  // The composer footer's Project, Model and thinking effort.
  const sessionSettings = createSessionSettings({
    get active() {
      return active;
    },
    get target() {
      return target;
    },
    get chatController() {
      return chatController;
    },
    get selectedProjectId() {
      return selectedProjectId;
    },
    get sessionsRefreshToken() {
      return sessionsRefreshToken;
    },
    get sessionInvalidations() {
      return sessionInvalidations;
    },
    loadModelCatalog: () => loadModelCatalog(),
    onError: (message, sessionKey) =>
      actions.setSessionActionError(
        message,
        chatState.sessions[sessionKey] ?? null,
      ),
    // `/model` changed the Agent's Model: read its defaults again.
    refreshAgentDefaults: (agentAddress) => {
      if (parseAgentAddress(agentAddress).projectId) {
        void target.refreshProjectTeam();
        return;
      }
      void chatController.loadAgents({ silent: true });
    },
  });

  let showSessionDrawer = $state(false);
  // Counts the Sessions this area's drafts created; each one refreshes the
  // Session list.
  let createdSessions = $state(0);
  const componentId = $props.id();
  const chatTitleId = `${componentId}-title`;

  let sessionFilters = $state(untrack(() => initialSessionFilters));

  // The address + invalidation token the command/skill suggestions were last
  // loaded for. `undefined` is distinct from every real key, so the first effect
  // always loads; Agent changes and Extension lifecycle events both refresh it.
  let lastCommandsAddress = undefined;

  // The displayed Session's rendered rows, projected once per update and
  // shared by the timeline, the Activity panel and Sub-Agent reconciliation.
  let activeTimelineItems = $derived(
    visibleTimelineItemsForRender(target.activeSessionState),
  );
  // Known work that outlives the Run: a running Sub-Agent or handed-off
  // command the loaded rows started. It keeps Stop all offered once the Run
  // ended.
  let backgroundWorkRunning = $derived(
    backgroundTasks(
      activeTimelineItems,
      chatState.subAgentStatuses,
      target.activeSessionState?.backgroundCommandStatuses,
      chatState.commandStatuses,
    ).some((task) => task.dotStatus === 'running'),
  );
  // Every Agent the picker offers: Identity Agents, then each Project Team.
  let pickerAgentAddresses = $derived([
    ...chatState.agents.map((agent) => agent.id),
    ...target.projectGroups.flatMap((group) =>
      group.members.map((member) =>
        formatAgentAddress(member.agent_id, group.projectId),
      ),
    ),
  ]);

  let pickerAgentActivity = $derived.by(() => {
    const displayedSessionKey = target.displayedSessionKey();
    return Object.fromEntries(
      pickerAgentAddresses.map((address) => {
        const unreadResults = agentUnreadResults(
          chatState,
          address,
          displayedSessionKey,
        );
        return [
          address,
          {
            status: agentActivityStatus(
              chatState,
              address,
              displayedSessionKey,
            ),
            unreadCount: unreadResults.count,
            latestUnreadAt: unreadResults.latestAt,
          },
        ];
      }),
    );
  });

  // A picker choice: an Identity Agent by its id, a Project Agent by its
  // `agent@project` address.
  function handleSelectPickerAgent(address) {
    const { agentId, projectId } = parseAgentAddress(address);
    return projectId
      ? target.handleSelectProjectAgent(agentId, projectId)
      : navigation.handleSelectAgent(agentId);
  }

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
  // The composer's per-session draft is keyed by the full displayed-session key
  // (a draft's own key while no Session exists); its per-agent input history is
  // keyed by the agent part alone (bare id for an identity agent,
  // `agent@projekt` for a project agent), so sessions of the same agent share
  // one history.
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
    if (!agent) {
      return null;
    }
    const draft = target.activeDraft();
    if (draft) {
      // The draft's Project and overrides as they are when it is sent.
      return async (content, options = {}) =>
        await actions.sendDraft(
          agent,
          sessionSettings.draftWithSettings(draft),
          content,
          options,
        );
    }
    const sessionState = target.activeSessionState;
    if (!sessionState) {
      return null;
    }
    return async (content, options = {}) =>
      await actions.sendStream(agent, sessionState, content, options);
  });
  let composerListFiles = $derived.by(() => {
    const agentId =
      target.activeSessionState?.agentId ??
      target.activeDraft()?.agentAddress ??
      '';
    if (!agentId) {
      return null;
    }
    // A Session lists the Project it works in; a draft the Project it chose.
    const scope = sessionSettings.readScope();
    return async () => await chatController.listFiles(agentId, scope);
  });
  // The model catalog is global (not agent/session-scoped). The composer
  // fetches it when `/model ` is typed, the footer when it first shows and
  // whenever its Model picker opens.
  async function loadModelCatalog() {
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
  }
  const composerLoadModelCatalog = () => sessionSettings.loadCatalog();
  const displayedSessionIsEmpty = () =>
    isSessionEmpty(target.activeSessionState) &&
    getDraft(composerDraftKey).trim().length === 0 &&
    actions.transientCards.length === 0;
  let lastSharedSelectedAgentId = '';
  let lastSharedAgents = null;
  let lastAgentsRefreshToken = null;

  // Chat stays mounted while another primary view is visible so Runs and
  // history can keep reconciling. Only the visible Chat surface may publish
  // navigation into App's shared selection; an async navigation that finishes
  // after Chat is hidden must not overwrite the active view's choice.
  function reportAgentSelected(agentId) {
    if (active) {
      onAgentSelected?.(agentId);
    }
  }

  $effect(() => {
    if (sharedAgents.length > 0 && sharedAgents !== lastSharedAgents) {
      lastSharedAgents = sharedAgents;
      setAgents(chatState, sharedAgents, { preserveSessionSelection });
    }
  });

  let initialSharedAgentSyncDone = false;
  $effect(() => {
    if (!active) {
      return;
    }
    const firstSync = !initialSharedAgentSyncDone;
    initialSharedAgentSyncDone = true;
    if (
      sharedSelectedAgentId &&
      sharedSelectedAgentId !== chatState.selectedAgentId &&
      chatState.agents.some((agent) => agent.id === sharedSelectedAgentId)
    ) {
      lastSharedSelectedAgentId = sharedSelectedAgentId;
      if (firstSync) {
        // Mount-time prop reconciliation, not user navigation: sync silently.
        // Routing this through handleSelectAgent would clear a just-restored
        // override and report a session navigation during a history restore —
        // the report then pushes a phantom entry over the restored one (the
        // mount-path echo hole). The controller still loads the adopted
        // Agent's current History when Chat was mounted hidden.
        selectAgent(chatState, sharedSelectedAgentId);
        void chatController.loadAdoptedSelectionHistory();
        return;
      }
      // Following the shared selection corrects Chat's place; the choice was
      // made (and recorded) where the Agent was selected.
      navigation.handleSelectAgent(sharedSelectedAgentId, {
        focusComposer: false,
        step: false,
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
    const statuses = commandStatuses;
    untrack(() => chatController.applyCommandStatuses(statuses));
  });

  // Durable completion activity for the displayed Agent addresses. A reconnect
  // or a full Session refresh re-reads every address; an address joining the
  // set is read once. Scoped Session invalidations are applied separately.
  $effect(() => {
    const addresses = pickerAgentAddresses;
    const reconnectRevision = connectionSnapshot
      ? `${connectionSnapshot.epoch ?? ''}:${connectionSnapshot.last_sequence ?? ''}`
      : '';
    const refreshKey = `${sessionsRefreshToken}:${reconnectRevision}`;
    untrack(() => chatController.syncAgentActivity(addresses, refreshKey));
  });

  $effect(() => {
    const entries = sessionInvalidations;
    untrack(() =>
      chatController.applySessionInvalidations(entries, runServerEvents),
    );
  });

  // A read acknowledgement tells every connected app, including the vBot tray,
  // that the user has seen the result, so only a page the user is looking at
  // (visible and focused) sends it. Attention is read live; regaining focus or
  // visibility bumps the revision so the displayed Session is checked again.
  let pageAttentionRevision = $state(0);
  const pageAttended = () =>
    document.visibilityState === 'visible' && document.hasFocus();

  $effect(() => {
    void pageAttentionRevision;
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
      !sessionHasTerminalRun(sessionState, unreadRunId) ||
      !pageAttended()
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
    const onPageAttentionChange = () => {
      pageAttentionRevision += 1;
    };
    const onVisibilityChange = () => {
      onPageAttentionChange();
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
    window.addEventListener('focus', onPageAttentionChange);
    return () => {
      document.removeEventListener('visibilitychange', onVisibilityChange);
      window.removeEventListener('focus', onPageAttentionChange);
      chatController.destroy();
    };
  });

  // Reload command/skill suggestions whenever the active address, the
  // Project its Skills come from (the Session's, or the one a draft chose) or
  // the live command catalog changes. The token does not disturb the draft or
  // active selection.
  $effect(() => {
    const { agentAddress } = target.activeAddressing();
    const scope = sessionSettings.readScope();
    const commandsKey = `${commandsRefreshToken}:${agentAddress}:${JSON.stringify(scope)}`;
    if (commandsKey === lastCommandsAddress) {
      return;
    }
    lastCommandsAddress = commandsKey;
    chatController.loadCommands(agentAddress, scope);
  });

  const loadAgents = (options = {}) => chatController.loadAgents(options);

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
    // A finished review's row reports what it changed, displayed or not.
    onReflectionFinished: (sourceState) =>
      chatController.refreshReflections(sourceState),
  });
  chatController = createChatController({
    chatState,
    preserveSessionSelection: untrack(() => preserveSessionSelection),
    runStream,
    isDisplayedSession: target.isDisplayedSession,
    shouldLoadCurrentHistory: () =>
      !navigation.viewingSessionId && !target.projectAgentActive,
    onAgentsChanged: (agents) => onAgentsChanged?.(agents),
    onAgentSelected: reportAgentSelected,
    onSessionCreated: (sessionState) => {
      navigation.adoptCreatedSession(
        sessionState.agentId,
        sessionState.sessionId,
      );
      createdSessions += 1;
    },
    onRestartQueueDiscarded: (count) => {
      actions.showChatToast(
        count === 1
          ? t('queue.restartDiscardedOne')
          : t('queue.restartDiscardedMany', { count }),
      );
    },
  });
  // Before the snapshot/event effects run: a fresh owner adopts the current
  // Run state instead of replaying App's retained buffers.
  untrack(() =>
    chatController.startFromServerState({
      connectionSnapshot,
      activeRuns,
      runServerEvent,
      runServerEvents,
    }),
  );

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
    projectGroups={target.projectGroups}
    agentActivity={pickerAgentActivity}
    selectedAddress={target.activeAgentAddress}
    displayedAgentName={target.activeAgent?.__overrideAddress
      ? target.activeAgent.name
      : ''}
    loadingAgents={chatState.loadingAgents}
    onSelectAgent={handleSelectPickerAgent}
    sessionListOpen={showSessionDrawer}
    sessionListDisabled={!target.activeAgent}
    onToggleSessionList={() => (showSessionDrawer = !showSessionDrawer)}
    newSessionDisabled={!target.activeAgent || chatState.loadingHistory}
    onNewSession={navigation.handleNewSession}
    actions={workspaceActions}
  />

  {#if target.projectAgentActive}
    <ProjectScanBanner report={target.projectReport} {onNavigateToProjects} />
    {#if target.projectScanError}
      <Banner variant="error" class="chat-view__state-banner">
        {target.projectScanError}
      </Banner>
    {/if}
  {/if}

  {#if chatState.loadingAgents}
    <Banner variant="neutral" class="chat-view__state-banner">
      {t('loading.agents')}
    </Banner>
  {:else if chatState.agents.length === 0 && target.projectGroups.length === 0}
    <EmptyState
      fill
      title={t('chat.noAgents')}
      description={chatState.agentsError}
    />
  {:else if !target.activeAgent}
    <EmptyState fill title={t('chat.noAgentSelected')} />
  {:else}
    <div class="chat-view__content-shell">
      {#if showSessionDrawer}
        <SessionListDrawer
          agentId={target.activeAgentAddress}
          currentSessionId={navigation.viewingSessionId ||
            target.activeAgent.current_session_id}
          reloadToken={`${sessionsRefreshToken}:${createdSessions}`}
          invalidations={sessionInvalidations}
          agents={target.sessionDrawerAgents}
          liveActivity={sessionDrawerActivity}
          initialFilters={sessionFilters}
          onFiltersChange={(next) => {
            sessionFilters = next;
            onSessionFiltersChange(next);
          }}
          onSessionSelected={navigation.handleSessionSelected}
          onSessionDeleted={navigation.handleSessionDeleted}
          onCompactionPolicyChange={chatController.applySessionCompactionPolicy}
        />
      {/if}
      <div
        class="chat-view__surface"
        style={`--chat-overlay-height: ${layout.footerOverlayHeight}px; --chat-scrollbar-width: ${layout.chatScrollbarWidth}px`}
      >
        <div class="chat-view__timeline-shell">
          <ChatTimeline
            timelineItems={activeTimelineItems}
            sessionKey={target.activeSessionState?.key ?? ''}
            currentRun={target.activeSessionState?.currentRun ?? null}
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
            backgroundCommandStatuses={target.activeSessionState
              ?.backgroundCommandStatuses}
            commandStatuses={chatState.commandStatuses}
            onLoadOlder={actions.loadOlderHistory}
            onNavigateToSubAgent={navigation.handleNavigateToSubAgentLink}
            onCancelToolCall={actions.handleCancelToolCall}
            onBackgroundToolCall={({ runId, toolCallId }) =>
              chatController.controlRun(
                target.activeSessionState,
                'background_tool',
                { runId, toolCallId },
              )}
            onCancelSubAgent={actions.handleCancelSubAgent}
            messageEditingDisabledReason={chatState.loadingHistory
              ? t('chat.editUnavailableLoading')
              : isRunActive(target.activeSessionState)
                ? t('chat.editUnavailableRunning')
                : (target.activeSessionState?.queue?.length ?? 0) > 0
                  ? t('chat.editUnavailableQueued')
                  : ''}
            onEditMessage={actions.handleEditMessage}
          />
        </div>
        <div
          class="chat-view__footer-stack"
          bind:this={layout.footerStackElement}
        >
          {#if navigation.subAgentSessionActive}
            <Banner
              appearance="compact"
              class="chat-view__footer-banner"
              aria-live="polite"
            >
              <strong>
                {t('chat.subagentSessionNotice')}
              </strong>
              <Button
                variant="tertiary"
                class="chat-view__subagent-session-return"
                disabled={chatState.loadingHistory}
                onClick={navigation.handleReturnToCurrentSession}
              >
                {navigation.subAgentParentTarget
                  ? t('chat.returnToParentSession')
                  : t('chat.returnToCurrentSession')}
              </Button>
            </Banner>
          {/if}
          {#if providerSetupMissing}
            <Banner
              appearance="compact"
              class="chat-view__footer-banner"
              aria-live="polite"
            >
              <strong>
                {t('chat.noProvider.title')}
              </strong>
              <Button
                variant="tertiary"
                class="chat-view__no-provider-action"
                onClick={onConnectProvider}
              >
                {t('chat.noProvider.action')}
              </Button>
            </Banner>
          {:else if agentModelMissing}
            <Banner
              appearance="compact"
              class="chat-view__footer-banner"
              aria-live="polite"
            >
              <strong>
                {t('chat.noModel.title')}
              </strong>
              <Button
                variant="tertiary"
                class="chat-view__no-model-action"
                onClick={onPickModel}
              >
                {t('chat.noModel.action')}
              </Button>
            </Banner>
          {/if}
          <div class="chat-view__measure">
            <QueuedMessages
              queuedMessages={target.activeSessionState?.queue ?? []}
              onRemoveQueuedMessage={actions.handleRemoveQueuedMessage}
              onSteerQueuedMessage={actions.handleSteerQueuedMessage}
              canSteer={target.activeSessionState?.status === 'running'}
              onEditQueuedMessage={actions.handleEditQueuedMessage}
            />
          </div>
          {#if chatState.historyError || chatState.actionError || chatState.commandsError || target.activeSessionState?.actionError || target.activeSessionState?.streamError}
            <div class="chat-view__composer-feedback" aria-live="polite">
              <div class="chat-view__measure chat-view__feedback-inner">
                {#if chatState.historyError}
                  <Banner variant="error">
                    {t('chat.historyLoadError')}
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
              </div>
            </div>
          {/if}
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
                cancelling={target.activeSessionState?.cancellingRunIds?.includes(
                  target.activeSessionState?.currentRun?.runId,
                ) ?? false}
                draftKey={composerDraftKey}
                historyKey={composerHistoryKey}
                focusRequest={layout.composerFocusRequest}
                availableSkills={chatState.availableSkills}
                contextUsage={target.activeSessionState?.contextUsage}
                compactionState={composerSendMessage &&
                target.activeSessionState
                  ? contextCompactionState(target.activeSessionState)
                  : 'unavailable'}
                compactionSubmitting={actions.isCompactionSubmitting(
                  target.activeSessionState,
                )}
                onForceCompaction={actions.handleCompactContext}
                contextWindow={target.activeSessionState?.contextUsage
                  ?.context_window}
                compactionPolicy={target.activeSessionState?.compactionPolicy}
                usage={target.activeSessionState?.usage}
                sessionUsage={target.activeSessionState?.sessionUsage}
                onSendMessage={composerSendMessage}
                onCancelRun={actions.handleCancelRun}
                {backgroundWorkRunning}
                stoppingAll={target.activeSessionState?.stoppingAll ?? false}
                onStopAll={actions.handleStopAll}
                onTranscriptionError={actions.handleTranscriptionError}
                onListFiles={composerListFiles}
                onLoadModelCatalog={composerLoadModelCatalog}
              >
                {#snippet computerControl()}
                  <ComputerUseControl
                    onError={actions.showChatToast}
                    subscribeInvalidations={subscribeExtensionInvalidations}
                  />
                {/snippet}
                {#snippet footer()}
                  {#if sessionSettings.view}
                    <ComposerSessionSettings
                      view={sessionSettings.view}
                      {projects}
                      {projectsLoaded}
                      onSelectProject={sessionSettings.selectProject}
                      onSelectModel={sessionSettings.selectModel}
                      onSelectThinkingEffort={sessionSettings.selectThinkingEffort}
                      onModelPickerOpen={() =>
                        void sessionSettings.loadCatalog().catch(() => {})}
                    />
                  {/if}
                {/snippet}
              </ChatComposer>
            {:else}
              <Banner appearance="compact" class="chat-view__footer-banner">
                <p>
                  {t('split.sameSession')}
                </p>
              </Banner>
            {/if}
          </div>
        </div>
      </div>
      <ChatActivityPanel
        timelineItems={activeTimelineItems}
        subAgentStatuses={chatState.subAgentStatuses}
        subAgentWork={chatState.subAgentWork}
        onSubAgentWorkWanted={actions.handleSubAgentWorkWanted}
        backgroundCommandStatuses={target.activeSessionState
          ?.backgroundCommandStatuses}
        commandStatuses={chatState.commandStatuses}
        reflectionTasks={reflectionTaskRows(target.activeSessionState)}
        sessionStats={sessionChanges.stats}
        onSessionStatsWanted={sessionChanges.setWanted}
        parentSession={navigation.sessionParentLink}
        onNavigateToSubAgent={navigation.handleNavigateToSubAgentLink}
        onNavigateToParentSession={navigation.navigateToParentSession}
        onOpenReflection={navigation.handleOpenReflection}
        onLoadReflectionChanges={(row) =>
          chatController.loadReflectionChanges(
            target.activeSessionState,
            row.runId,
          )}
        onUndoReflection={(row) =>
          chatController.undoReflection(target.activeSessionState, row.runId)}
        onCancelSubAgent={actions.handleCancelSubAgent}
        onCancelBackgroundCommand={actions.handleCancelBackgroundCommand}
      />
    </div>
  {/if}
</section>
