<script>
  import ChatHeader from './chat/ChatHeader.svelte';
  import {
    contextCompactionState,
    isRunActive,
    createChatState,
    isSessionEmpty,
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
  import ComposerGoal from './chat/ComposerGoal.svelte';
  import ComposerSessionSettings from './chat/ComposerSessionSettings.svelte';
  import ComputerUseControl from './ComputerUseControl.svelte';
  import SecretGiveForm from './chat/SecretGiveForm.svelte';
  import SecretRequests from './chat/SecretRequests.svelte';
  import ChatActivityPanel from './chat/ChatActivityPanel.svelte';
  import { reflectionTaskRows } from '../lib/chatTimelinePresentation.js';
  import { untrack } from 'svelte';
  import { listConnections, listModels } from '$lib/api.js';
  import { getDraft } from '$lib/composerMemory.js';
  import { agentNeedsModel } from '$lib/onboarding.js';
  import { parseAgentAddress } from '$lib/agentAddress.js';
  import { SESSION_WAITING } from '$lib/sessionActivity.js';
  import { createChatViewActivity } from './chat/view/activity.svelte.js';
  import { createChatViewLifecycle } from './chat/view/lifecycle.svelte.js';
  import { createChatViewTarget } from './chat/view/target.svelte.js';
  import { createChatViewNavigation } from './chat/view/navigation.svelte.js';
  import { createChatViewActions } from './chat/view/actions.svelte.js';
  import { createChatViewLayout } from './chat/view/layout.svelte.js';
  import { createSessionInfo } from './chat/view/sessionInfo.svelte.js';
  import { createSessionSettings } from './chat/view/sessionSettings.svelte.js';
  import './chat/view/chatView.css';

  let {
    active = true,
    workspaceActions,
    // Open in split view: `{ label, open(target) }` shows a Session or Agent
    // (`{ agentAddress, sessionId?, subAgent? }`) in the workspace's other
    // area; null offers no such action.
    otherArea = null,
    // The workspace's request to show a target chosen in the other area here
    // (`{ target, requestId }`).
    openRequest = null,
    interactive = true,
    composerAvailable = true,
    preserveSessionSelection = false,
    // Keeps this Chat area's unsaved drafts apart from another area's: each
    // area remembers its own draft per Agent.
    draftScope = 0,
    initialSessionFilters = null,
    onSessionFiltersChange,
    onDisplayedSession = () => {},
    // Reports this Chat's Main menu indicator: `{count, tone, label}` while
    // Agents hold unread results, otherwise null.
    onNavigationIndicator = () => {},
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
    // Stores the user's Project order: (projectIds) => Promise.
    onReorderProjects = async () => {},
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
    // App's Run event feed (`runEventFeed.svelte.js`): every Run server event
    // in arrival order, read from this Chat's own position, and the Runs
    // active now.
    runEvents = null,
    // App's bounded live map of background shell commands by command id
    // (`command_status_changed`: `{ status, stillRunning, sessionId }`); the
    // controller re-applies the whole map.
    commandStatuses = {},
    // App's Session activity (`sessionActivity.js`): every working or waiting
    // Session by Session key; null until the server first reported it.
    sessionActivity = null,
    terminalsRefreshToken = 0,
    // Bumped when Agent Secrets or Secret requests change; the displayed
    // Session's pending requests are read again.
    agentSecretsRefreshToken = 0,
    connectionSnapshot = null,
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
    // App's Identity Agent renames, delivered synchronously before the roster
    // is read again (`(oldAgentId, newAgentId)`); returns the unsubscribe.
    subscribeAgentRenames = null,
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
      return lifecycle.lastSharedSelectedAgentId;
    },
    set lastSharedSelectedAgentId(value) {
      lifecycle.lastSharedSelectedAgentId = value;
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

  const sessionInfo = createSessionInfo({
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
      return activity.timelineItems;
    },
    get sessionUsage() {
      return target.activeSessionState?.sessionUsage;
    },
    get subAgentStatuses() {
      return chatState.subAgentStatuses;
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
    get agentsRefreshToken() {
      return agentsRefreshToken;
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
        return target.refreshProjectTeam();
      }
      return chatController.loadAgents({ silent: true });
    },
  });

  let showSessionDrawer = $state(false);
  let activityPanelOpen = $state(false);
  let activityRunningCount = $state(0);
  // Counts the Sessions this area's drafts created; each one refreshes the
  // Session list.
  let createdSessions = $state(0);
  const componentId = $props.id();
  const chatTitleId = `${componentId}-title`;

  let sessionFilters = $state(untrack(() => initialSessionFilters));

  // The by-Project Session list starts each new Session in a Project: a
  // group's "+" with the Agent it chose, New Session with the displayed Agent
  // in the Project of the shown Session.
  const startSessionInProject = (projectId, agentAddress, options) =>
    navigation.handleNewSessionWith(
      agentAddress,
      (draft) => sessionSettings.startDraftIn(draft, projectId),
      options,
    );

  // The draft footer's Agent picker: the draft moves to the chosen Agent,
  // which becomes the active one. Its text, attachments and Project follow;
  // Model and effort become the new Agent's (user decision).
  let composer = $state(null);
  const moveDraftToAgent = (agentAddress) => {
    const from = target.activeDraft();
    const view = sessionSettings.view;
    if (!from || view?.kind !== 'draft' || agentAddress === from.agentAddress) {
      return;
    }
    const projectId = view.project.value ?? null;
    return navigation.handleNewSessionWith(agentAddress, (to) => {
      composer?.moveDraft(from.key, to.key);
      sessionSettings.moveDraft(from, to, projectId);
    });
  };

  const handleNewSession = () => {
    const next =
      showSessionDrawer && sessionFilters?.byProject
        ? target.projectNewSessionTarget(sessionSettings.view)
        : null;
    return next
      ? startSessionInProject(next.projectId, next.agentAddress, {
          keepEmptySession: true,
        })
      : navigation.handleNewSession();
  };

  // A picker choice: an Identity Agent by its id, a Project Agent by its
  // `agent@project` address.
  function handleSelectPickerAgent(address) {
    const { agentId, projectId } = parseAgentAddress(address);
    return projectId
      ? target.handleSelectProjectAgent(agentId, projectId)
      : navigation.handleSelectAgent(agentId);
  }

  // A target chosen for this area in the other one applies like a choice in
  // this area's own Session list or Agent bar, once the area shows and knows
  // its Agents.
  let handledOpenRequestId = 0;
  $effect(() => {
    const request = openRequest;
    if (!request || request.requestId === handledOpenRequestId) return;
    if (!active || chatState.agents.length === 0) return;
    handledOpenRequestId = request.requestId;
    untrack(() => {
      const { agentAddress, sessionId, subAgent } = request.target;
      if (sessionId) {
        void navigation.handleSessionSelected(
          sessionId,
          agentAddress,
          subAgent === true,
        );
      } else {
        void handleSelectPickerAgent(agentAddress);
      }
    });
  });

  // Sub-Agent links carry the child's bare id, qualified like a click on
  // the link.
  let subAgentOtherArea = $derived(
    otherArea && {
      label: otherArea.label,
      open: (link) =>
        otherArea.open({
          agentAddress: target.qualifiedChildAgentAddress(link.agentId),
          sessionId: link.sessionId,
          subAgent: true,
        }),
    },
  );

  let composerDisabled = $derived(
    !target.activeAgent || chatState.loadingHistory,
  );
  // A Session of a Live voice call is written only by the call, so it shows a
  // notice in place of the composer.
  let liveCallSession = $derived(Boolean(target.activeAgent?.__liveCall));
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

  $effect(() => {
    target.displayedSessionKey();
    untrack(() => chatController.syncDisplayedHistoryLoad());
  });
  let composerHistoryKey = $derived.by(() => {
    const separator = composerDraftKey.indexOf('::');
    return separator >= 0 ? composerDraftKey.slice(0, separator) : '';
  });
  // Each callback closes over the displayed address and draft settings when
  // Svelte hands it to the Composer. The Composer snapshots that function
  // before any async @-mention lookup, so later choices cannot redirect it.
  let composerSendMessage = $derived.by(() => {
    const agent = target.activeAgent;
    if (!agent) {
      return null;
    }
    const draft = target.activeDraft();
    if (draft) {
      const submissionDraft = sessionSettings.draftWithSettings(draft);
      return async (content, options = {}) => {
        if (
          sessionSettings.hasPending(submissionDraft.key) &&
          !(await sessionSettings.flush(submissionDraft.key))
        )
          return false;
        return await actions.sendDraft(
          agent,
          submissionDraft,
          content,
          options,
        );
      };
    }
    const sessionState = target.activeSessionState;
    if (!sessionState) {
      return null;
    }
    return async (content, options = {}) => {
      if (
        sessionSettings.hasPending(sessionState.key) &&
        !(await sessionSettings.flush(sessionState.key))
      )
        return false;
      return await actions.sendStream(agent, sessionState, content, options);
    };
  });
  // The Secret form a bare `/secret` opened, for the Agent and Session (or
  // draft) displayed then: `{ agentId, projectId, sessionId, send }`.
  let secretForm = $state(null);
  function openSecretForm() {
    const sessionState = target.activeSessionState;
    const draft = sessionState ? null : target.activeDraft();
    const address = sessionState?.agentId ?? draft?.agentAddress ?? '';
    if (!address) return;
    const { agentId, projectId } = parseAgentAddress(address);
    secretForm = {
      agentId,
      projectId,
      sessionId: sessionState?.sessionId ?? '',
      send: sessionState ? null : composerSendMessage,
    };
  }
  let displayedSecretAddress = $derived(
    parseAgentAddress(target.activeSessionState?.agentId ?? ''),
  );
  let composerListFiles = $derived.by(() => {
    const agentId =
      target.activeSessionState?.agentId ??
      target.activeDraft()?.agentAddress ??
      '';
    if (!agentId) {
      return null;
    }
    // A Session lists the Project it works in; a draft the Project it chose.
    // `request.directory` asks for one directory's entries instead of the index.
    const scope = sessionSettings.readScope();
    return async (request = {}) =>
      await chatController.listFiles(agentId, { ...scope, ...request });
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
  // Chat stays mounted while another primary view is visible so Runs and
  // history can keep reconciling. Only the visible Chat surface may publish
  // navigation into App's shared selection; an async navigation that finishes
  // after Chat is hidden must not overwrite the active view's choice.
  function reportAgentSelected(agentId) {
    if (active) {
      onAgentSelected?.(agentId);
    }
  }

  const activity = createChatViewActivity({
    chatState,
    target,
    get active() {
      return active;
    },
    get onNavigationIndicator() {
      return onNavigationIndicator;
    },
  });
  const lifecycle = createChatViewLifecycle({
    chatState,
    target,
    navigation,
    actions,
    sessionSettings,
    activity,
    reportAgentSelected,
    get active() {
      return active;
    },
    get sharedAgents() {
      return sharedAgents;
    },
    get sharedSelectedAgentId() {
      return sharedSelectedAgentId;
    },
    get preserveSessionSelection() {
      return preserveSessionSelection;
    },
    get subscribeAgentRenames() {
      return subscribeAgentRenames;
    },
    get connectionSnapshot() {
      return connectionSnapshot;
    },
    get agentsRefreshToken() {
      return agentsRefreshToken;
    },
    get runEvents() {
      return runEvents;
    },
    get commandStatuses() {
      return commandStatuses;
    },
    get sessionActivity() {
      return sessionActivity;
    },
    get queueInvalidation() {
      return queueInvalidation;
    },
    get terminalsRefreshToken() {
      return terminalsRefreshToken;
    },
    get sessionsRefreshToken() {
      return sessionsRefreshToken;
    },
    get sessionInvalidations() {
      return sessionInvalidations;
    },
    get commandsRefreshToken() {
      return commandsRefreshToken;
    },
    get onAgentsChanged() {
      return onAgentsChanged;
    },
    onSessionCreated() {
      createdSessions += 1;
    },
  });
  const chatController = lifecycle.controller;

  $effect(() => {
    chatController.reconcileSubAgentRows(activity.timelineItems, {
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
    agentActivity={activity.pickerAgentActivity}
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
    onNewSession={handleNewSession}
    {otherArea}
    actions={workspaceActions}
    infoPanelOpen={activityPanelOpen}
    infoPanelId={`${componentId}-info`}
    infoToggleId={`${componentId}-info-toggle`}
    infoRunningCount={activityRunningCount}
    onToggleInfoPanel={() => (activityPanelOpen = !activityPanelOpen)}
  />

  {#if target.projectAgentActive}
    <ProjectScanBanner report={target.projectReport} {onNavigateToProjects} />
    {#if target.projectScanError}
      <Banner variant="error" class="chat-view__state-banner">
        {target.projectScanError}
      </Banner>
    {/if}
  {/if}

  {#if chatState.agentActivityError}
    <Banner variant="warn" class="chat-view__state-banner">
      {t('chat.activityLoadError')}
      {chatState.agentActivityError}
    </Banner>
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
          liveActivity={activity.sessionDrawerActivity}
          {sessionActivity}
          initialFilters={sessionFilters}
          onFiltersChange={(next) => {
            sessionFilters = next;
            onSessionFiltersChange(next);
          }}
          onSessionSelected={navigation.handleSessionSelected}
          {otherArea}
          onSessionDeleted={navigation.handleSessionDeleted}
          onSessionDeleteStarted={navigation.beginSessionDeletion}
          onCompactionPolicyChange={chatController.applySessionCompactionPolicy}
          {projects}
          {onReorderProjects}
          projectTeams={target.projectGroups}
          onNewSessionInProject={startSessionInProject}
        />
      {/if}
      <div
        class="chat-view__surface"
        class:chat-view__surface--info-open={activityPanelOpen}
        style={`--chat-overlay-height: ${layout.footerOverlayHeight}px; --chat-scrollbar-width: ${layout.chatScrollbarWidth}px`}
      >
        <div class="chat-view__timeline-shell">
          <ChatTimeline
            timelineItems={activity.timelineItems}
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
            commandStatuses={activity.commandStatuses}
            sessionWaiting={sessionActivity?.[
              target.activeSessionState?.key ?? ''
            ]?.state === SESSION_WAITING}
            onLoadOlder={actions.loadOlderHistory}
            onLoadRunHistory={(gap) =>
              chatController.loadRunHistory(target.activeSessionState, gap)}
            onLoadToolCall={(call) =>
              chatController.loadToolCall(target.activeSessionState, call)}
            onNavigateToSubAgent={navigation.handleNavigateToSubAgentLink}
            otherArea={subAgentOtherArea}
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
          {:else if navigation.reflectionSessionActive}
            <Banner
              appearance="compact"
              class="chat-view__footer-banner"
              aria-live="polite"
            >
              <strong>
                {t('chat.reflectionSessionNotice')}
              </strong>
              {#if navigation.reflectionSourceTarget}
                <Button
                  variant="tertiary"
                  class="chat-view__reflection-session-return"
                  disabled={chatState.loadingHistory}
                  onClick={navigation.handleReturnToReflectionSource}
                >
                  {t('chat.returnToMainSession')}
                </Button>
              {/if}
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
            <SecretRequests
              agentId={displayedSecretAddress.agentId}
              projectId={displayedSecretAddress.projectId}
              sessionId={target.activeSessionState?.sessionId ?? ''}
              refreshToken={agentSecretsRefreshToken}
            />
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
            {#if liveCallSession}
              <Banner appearance="compact" class="chat-view__footer-banner">
                <p>
                  {t('chat.liveCallNotice')}
                </p>
              </Banner>
            {:else if composerAvailable}
              {#if sessionSettings.view?.goal}
                <ComposerGoal
                  goal={sessionSettings.view.goal}
                  onCommand={actions.handleGoalCommand}
                />
              {/if}
              <ChatComposer
                bind:this={composer}
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
                backgroundWorkRunning={activity.backgroundWorkRunning}
                stoppingAll={target.activeSessionState?.stoppingAll ?? false}
                onStopAll={actions.handleStopAll}
                onTranscriptionError={actions.handleTranscriptionError}
                onListFiles={composerListFiles}
                onLoadModelCatalog={composerLoadModelCatalog}
                onOpenSecretForm={composerSendMessage ? openSecretForm : null}
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
                      outputSpeed={target.activeSessionState?.outputSpeed}
                      outputLatency={target.activeSessionState?.outputLatency}
                      {projects}
                      {projectsLoaded}
                      onSelectProject={sessionSettings.selectProject}
                      onSelectAgent={moveDraftToAgent}
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
            {#if secretForm}
              <SecretGiveForm
                agentId={secretForm.agentId}
                projectId={secretForm.projectId}
                sessionId={secretForm.sessionId}
                onSendCommand={secretForm.send}
                onGiven={actions.showChatToast}
                onClose={() => (secretForm = null)}
              />
            {/if}
          </div>
        </div>
        <ChatActivityPanel
          bind:open={activityPanelOpen}
          onRunningCountChange={(count) => (activityRunningCount = count)}
          id={`${componentId}-info`}
          toggleId={`${componentId}-info-toggle`}
          timelineItems={activity.timelineItems}
          subAgentStatuses={chatState.subAgentStatuses}
          subAgentWork={chatState.subAgentWork}
          onSubAgentWorkWanted={actions.handleSubAgentWorkWanted}
          subAgentCommands={activity.subAgentCommands}
          backgroundCommandStatuses={target.activeSessionState
            ?.backgroundCommandStatuses}
          commandStatuses={activity.commandStatuses}
          reflectionTasks={reflectionTaskRows(target.activeSessionState)}
          sessionStats={sessionInfo.stats}
          session={sessionInfo.summary}
          usage={sessionInfo.usage}
          subAgentTree={sessionInfo.subAgents}
          onSessionStatsWanted={sessionInfo.setWanted}
          parentSession={navigation.sessionParentLink}
          onNavigateToSubAgent={navigation.handleNavigateToSubAgentLink}
          otherArea={subAgentOtherArea}
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
          onStopSubAgentWork={actions.handleStopSubAgentWork}
          onStopBackgroundCommand={actions.handleStopBackgroundCommand}
          onLoadCommandOutput={actions.loadCommandOutput}
        />
      </div>
    </div>
  {/if}
</section>
