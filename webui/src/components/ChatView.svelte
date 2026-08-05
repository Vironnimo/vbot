<script>
  import { onDestroy, onMount } from 'svelte';

  import { subscribeRunEvents } from '$lib/api.js';
  import { getDraft } from '$lib/composerMemory.js';
  import {
    extractMentionTokens,
    matchMentionCandidates,
  } from '$lib/fileMentions.js';
  import { t } from '$lib/i18n.js';
  import { agentNeedsModel } from '$lib/onboarding.js';
  import { parseAgentAddress } from '$lib/agentAddress.js';
  import { tooltip } from '$lib/tooltip.js';
  import { createChatRunStream } from '../lib/chatRunStream.js';
  import {
    agentActivityStatus,
    canCreateNewSession,
    createChatController,
    createChatState,
    currentSessionState,
    ensureSessionState,
    formatAgentAddress,
    isProjectSelected,
    isRunActive,
    isSessionEmpty,
    newestUnreadSessionForAgent,
    pickProjectAgentSessionId,
    resolveAgentAddressing,
    selectAgent,
    selectedAgent,
    sessionHasTerminalRun,
    setAgents,
    visibleTimelineItemsForRender,
  } from '../lib/chatState.js';
  import {
    projectTeam as normalizeProjectTeam,
    normalizeScanReport,
  } from '../lib/projectsView.js';
  import ChatActivityPanel from './chat/ChatActivityPanel.svelte';
  import ChatHeader from './chat/ChatHeader.svelte';
  import ProjectScanBanner from './chat/ProjectScanBanner.svelte';
  import ChatComposer from './ChatComposer.svelte';
  import SessionListDrawer from './SessionListDrawer.svelte';
  import ChatTimeline from './ChatTimeline.svelte';
  import QueuedMessages from './QueuedMessages.svelte';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import EmptyState from './ui/EmptyState.svelte';

  let {
    active = true,
    sharedAgents = [],
    sharedSelectedAgentId = '',
    // Chat reading-column width preference: 'comfortable' | 'wide' | 'full'.
    // Phase 3 seeds the persisted value from App; the default keeps the chat
    // self-contained (centered, capped at the comfortable measure).
    chatWidth = 'comfortable',
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
    wakewordStatus = { enabled: false, state: 'off' },
    desktopCapabilities = null,
    onNavigateToVoiceSettings = () => {},
    // App supplies the server-backed operational state. `null` means Settings
    // are still loading, so Chat does not guess which prerequisite is missing.
    hasConnectedProvider = true,
    // Invoked from the setup notices. App routes Provider setup directly to
    // Settings and Model assignment to the current Agent.
    onConnectProvider = () => {},
    onPickModel = () => {},
  } = $props();

  const chatState = $state(createChatState());
  let creatingSession = $state(false);
  let composerFocusRequest = $state(0);
  // Chat-local bottom toast for transient command replies and lifecycle notices.
  // Error notices stay in the top stack.
  let chatToast = $state('');
  // Non-persisted `output: "transient"` command cards (/status, /help) rendered
  // in the chat stream. Kept in a dedicated array so incoming run events never
  // clear them; only a displayed-session change (or reload) empties them. Each
  // card carries the id of the timeline item it followed at creation, so the
  // timeline anchors it in place instead of restacking all cards at the bottom.
  let transientCards = $state([]);
  let transientCardsSessionKey = '';
  let displayedSessionGeneration = 0;
  let generationSessionKey = '';
  let transientCardSeq = 0;
  let showSessionDrawer = $state(false);
  let viewingSessionId = $state('');
  let viewingSessionAgentId = $state('');
  let viewingSubAgentSession = $state(false);
  let submittedTurnScrollKey = $state(0);
  let submittedTurnScrollRunId = $state('');
  let subAgentLinkFollowRequest = $state(null);
  let subAgentLinkFollowRequestId = 0;
  let handledSessionNavigationKey = '';
  // Bottom command toast auto-dismiss. Kept as a single constant so the
  // dwell time can be tuned in one place.
  const CHAT_TOAST_TIMEOUT_MS = 5000;
  const MOBILE_CHAT_MEDIA_QUERY = '(max-width: 640px)';
  let chatToastTimeoutId = null;

  const requestComposerFocus = ({ includeMobile = false } = {}) => {
    const mobile =
      typeof window !== 'undefined' &&
      typeof window.matchMedia === 'function' &&
      window.matchMedia(MOBILE_CHAT_MEDIA_QUERY).matches;
    if (!includeMobile && mobile) {
      return;
    }
    composerFocusRequest += 1;
  };

  // --- Project (second-bar) state -----------------------------------------
  //
  // The second bar is a pure projection of `project.show`'s scan team — no
  // second source of truth. Selecting a project loads its team and report;
  // selecting a project agent makes it the active agent. A project (config)
  // agent has NO server `current_session_id` (RPC-contract trap 1), so its
  // session is chosen locally and held in `projectAgentSessions`, keyed by the
  // agent's full address (`agent@projekt`).
  let projectTeam = $state([]);
  let projectReport = $state(null);
  let projectScanError = $state('');
  let loadingProjectTeam = $state(false);
  // The active project agent's bare id, '' when chatting an identity agent.
  let selectedProjectAgentId = $state('');
  // address (`agent@projekt`) -> locally chosen session id (trap 1).
  let projectAgentSessions = $state({});
  // Guards repeated project-show side effects for the same chosen project.
  let lastLoadedProjectId = '';
  // Set true after the first project effect run. That first run is the reload
  // restore — it honors the remembered project agent (`sharedSelectedProjectAgentId`);
  // every later run is a user-initiated dropdown switch that jumps to the
  // project default.
  let initialProjectRestoreDone = false;
  // The address + invalidation token the command/skill suggestions were last
  // loaded for. `undefined` is distinct from every real key, so the first effect
  // always loads; Agent changes and Extension lifecycle events both refresh it.
  let lastCommandsAddress = undefined;

  // Whether the active agent is a project (config) team agent. When false the
  // chat is on an identity agent and every RPC payload is byte-identical to
  // today (the hard no-regression rule).
  let projectAgentActive = $derived(
    isProjectSelected(selectedProjectId) && selectedProjectAgentId !== '',
  );

  // The chosen project's display name, used as the bold prefix on the team bar.
  let selectedProjectName = $derived(
    projects.find((project) => project.project_id === selectedProjectId)
      ?.display_name ||
      selectedProjectId ||
      '',
  );

  let activeAgent = $derived(getActiveAgent());
  let activeSessionState = $derived(getActiveSessionState());
  let activeTimelineItems = $derived(
    visibleTimelineItemsForRender(activeSessionState),
  );
  let identityAgentStatuses = $derived.by(() =>
    Object.fromEntries(
      chatState.agents.map((agent) => [
        agent.id,
        agentActivityStatus(chatState, agent.id, displayedSessionKey()),
      ]),
    ),
  );
  let projectAgentStatuses = $derived.by(() =>
    Object.fromEntries(
      projectTeam.map((member) => {
        const address = formatAgentAddress(member.agent_id, selectedProjectId);
        return [
          member.agent_id,
          agentActivityStatus(chatState, address, displayedSessionKey()),
        ];
      }),
    ),
  );
  // Outside address of the displayed agent (bare id for identity, full
  // `agent@projekt` otherwise) — what address-parsing RPCs like session.list
  // need (trap 2). The session drawer lists sessions through it.
  let activeAgentAddress = $derived(activeAddressing().agentAddress);
  // Agent-bar selection follows the owner of the displayed Session, while the
  // underlying selected Agent remains the return target for an override. This
  // keeps nested Sub-Agent navigation truthful without losing the root context.
  let displayedIdentityAgentId = $derived.by(() => {
    const addressing = activeAddressing();
    return addressing.projectId ? '' : addressing.bareAgentId;
  });
  let displayedProjectAgentId = $derived.by(() => {
    const addressing = activeAddressing();
    return addressing.projectId === selectedProjectId
      ? addressing.bareAgentId
      : '';
  });
  let subAgentSessionActive = $derived(
    Boolean(viewingSessionId) && viewingSubAgentSession,
  );
  // The parent-session target of the displayed sub-agent session, resolved
  // from the child's `subagent_parent` metadata (via session.list) when the
  // sub-agent view opens — so the banner button can say "Return to parent
  // session" only when the parent is actually reachable. `null` while
  // unresolved or for child sessions without usable parent metadata.
  let subAgentParentTarget = $state(null);
  let subAgentParentFetchKey = '';

  $effect(() => {
    if (!subAgentSessionActive) {
      subAgentParentFetchKey = '';
      subAgentParentTarget = null;
      return;
    }
    const key = displayedSessionKey();
    if (!key || key === subAgentParentFetchKey) {
      return;
    }
    subAgentParentFetchKey = key;
    subAgentParentTarget = null;
    loadSubAgentParentTarget(key, overrideAgentAddress(), viewingSessionId);
  });

  const loadSubAgentParentTarget = async (
    displayKey,
    childAddress,
    childSessionId,
  ) => {
    if (!childAddress || !childSessionId) {
      return;
    }
    try {
      const listed = await chatController.listSessions(childAddress);
      // A newer navigation may have superseded this child view mid-flight.
      if (displayedSessionKey() !== displayKey) {
        return;
      }
      const childSession = (listed?.sessions ?? []).find(
        (session) => String(session?.id ?? '').trim() === childSessionId,
      );
      const parent = childSession?.subagent_parent;
      const parentAgentId =
        typeof parent?.agent_id === 'string' ? parent.agent_id.trim() : '';
      const parentSessionId =
        typeof parent?.session_id === 'string' ? parent.session_id.trim() : '';
      if (!parentAgentId || !parentSessionId) {
        return;
      }
      const parentProjectId =
        typeof parent?.project_id === 'string' ? parent.project_id.trim() : '';
      // A vanished identity parent cannot be opened — keep the
      // return-to-current fallback (a project parent is not roster-checkable
      // and surfaces a load error instead of dead-ending).
      if (!parentProjectId && !agentById(parentAgentId)) {
        return;
      }
      const parentAgentAddress = formatAgentAddress(
        parentAgentId,
        parentProjectId,
      );
      let parentSession = null;
      if (parentAgentAddress === childAddress) {
        parentSession = (listed?.sessions ?? []).find(
          (session) => String(session?.id ?? '').trim() === parentSessionId,
        );
      } else {
        try {
          const parentListed =
            await chatController.listSessions(parentAgentAddress);
          if (displayedSessionKey() !== displayKey) {
            return;
          }
          parentSession = (parentListed?.sessions ?? []).find(
            (session) => String(session?.id ?? '').trim() === parentSessionId,
          );
        } catch {
          // Parent navigation remains usable when only its nesting metadata
          // cannot be loaded; the destination history can still resolve.
        }
      }
      subAgentParentTarget = {
        agentAddress: parentAgentAddress,
        sessionId: parentSessionId,
        isSubAgentSession:
          parentSession?.is_subagent_session === true ||
          (parentSession?.subagent_parent !== null &&
            typeof parentSession?.subagent_parent === 'object'),
      };
    } catch {
      // Best effort — the banner button falls back to return-to-current.
    }
  };
  // Any local override away from the selected agent's current session — also
  // true for same-agent drawer selections, which must offer a return path too.
  let sessionOverrideActive = $derived(Boolean(viewingSessionId));
  let newSessionBlocked = $derived(!canCreateNewSession(activeSessionState));
  let composerDisabled = $derived(!activeAgent || chatState.loadingHistory);
  // Provider availability is the first prerequisite for every current Agent.
  // Do not infer it from Models: App supplies Settings' authoritative usable-
  // connection state. A model-less Identity Agent becomes the second step once
  // at least one Provider is connected; Project Agents resolve their Model
  // through Project defaults, and override stand-ins carry no Model field.
  let providerSetupMissing = $derived(
    Boolean(activeAgent) && hasConnectedProvider === false,
  );
  let agentModelMissing = $derived(
    Boolean(activeAgent) &&
      !projectAgentActive &&
      !activeAgent?.__overrideAddress &&
      hasConnectedProvider === true &&
      agentNeedsModel(activeAgent),
  );
  // The composer's per-session draft is keyed by the full displayed-session key;
  // its per-agent input history is keyed by the agent part alone (bare id for an
  // identity agent, `agent@projekt` for a project agent), so sessions of the
  // same agent share one history.
  let composerDraftKey = $derived(displayedSessionKey());
  let composerHistoryKey = $derived.by(() => {
    const separator = composerDraftKey.indexOf('::');
    return separator >= 0 ? composerDraftKey.slice(0, separator) : '';
  });
  // Each callback closes over the displayed address at the moment Svelte hands
  // it to the Composer. The Composer snapshots that function before any async
  // @-mention lookup, so navigation cannot redirect an older submit.
  let composerSendMessage = $derived.by(() => {
    const agent = activeAgent;
    const sessionState = activeSessionState;
    if (!agent || !sessionState) {
      return null;
    }
    return async (content, options = {}) =>
      await sendStream(agent, sessionState, content, options);
  });
  let composerListFiles = $derived.by(() => {
    const agentId = activeSessionState?.agentId ?? '';
    if (!agentId) {
      return null;
    }
    return async () => await chatController.listFiles(agentId);
  });
  const displayedSessionIsEmpty = () =>
    isSessionEmpty(activeSessionState) &&
    getDraft(composerDraftKey).trim().length === 0 &&
    transientCards.length === 0;
  let lastSharedSelectedAgentId = '';
  let lastSharedAgents = null;
  let lastAgentsRefreshToken = null;

  // The active project team member (config agent) when a project agent is the
  // chosen chat target, else null. Looked up by bare id against the projected
  // team. It is NOT in `chatState.agents` (that holds only identity agents).
  function activeProjectMember() {
    if (!projectAgentActive) {
      return null;
    }
    return (
      projectTeam.find(
        (member) => member.agent_id === selectedProjectAgentId,
      ) ?? null
    );
  }

  // The outside address of the agent that owns the non-override ("current")
  // view: the active project team agent when one is chosen, else the selected
  // identity agent (bare id — identity addressing is unchanged).
  function activeOwnAgentAddress() {
    if (projectAgentActive) {
      return currentProjectAgentAddress();
    }
    return chatState.selectedAgentId;
  }

  // The outside address of the agent that owns the overridden (viewed)
  // session. `viewingSessionAgentId` stores the explicit owner (a bare
  // identity id or a full `agent@projekt` address); '' means "the active
  // agent's own past session".
  function overrideAgentAddress() {
    return viewingSessionAgentId || activeOwnAgentAddress();
  }

  // Resolve the addressing for the active agent (RPC-contract traps 1 & 2).
  // An active session override wins over both the project branch and the
  // identity-current branch — a drawer pick or sub-agent link decides what is
  // displayed regardless of which agent bar is active. Without an override:
  // - identity agent: `agentAddress === bareAgentId`, `projectId: null`,
  //   session from the identity `current_session_id` path. Byte-identical
  //   to today.
  // - project agent: full `agent@projekt` address for chat/session/history,
  //   bare id for queue/cancel-tool; session chosen locally (trap 1).
  function activeAddressing() {
    if (viewingSessionId) {
      const agentAddress = overrideAgentAddress();
      const { agentId, projectId } = parseAgentAddress(agentAddress);
      return {
        bareAgentId: agentId,
        projectId: projectId || null,
        agentAddress,
        isProjectAgent: Boolean(projectId),
        sessionId: viewingSessionId,
      };
    }
    if (projectAgentActive) {
      const addressing = resolveAgentAddressing(
        selectedProjectAgentId,
        selectedProjectId,
        true,
      );
      return {
        ...addressing,
        isProjectAgent: true,
        sessionId: projectAgentSessions[addressing.agentAddress] ?? '',
      };
    }
    const agent = selectedAgent(chatState);
    const bareAgentId = agent?.id ?? '';
    return {
      bareAgentId,
      projectId: null,
      agentAddress: bareAgentId,
      isProjectAgent: false,
      sessionId: agent?.current_session_id || '',
    };
  }

  function getActiveAgent() {
    if (viewingSessionId) {
      if (!viewingSessionAgentId) {
        // The active agent's own past session.
        return projectAgentActive
          ? projectAgentAsAgent(activeProjectMember())
          : selectedAgent(chatState);
      }
      return (
        agentById(viewingSessionAgentId) ??
        overrideAgentDisplayStandIn(viewingSessionAgentId)
      );
    }
    if (projectAgentActive) {
      return projectAgentAsAgent(activeProjectMember());
    }
    return selectedAgent(chatState);
  }

  // Minimal agent-like object for an overridden session whose owner is not an
  // identity-roster agent — a project team agent's session (or a project
  // child), or an identity agent deleted while its session is still viewed.
  // Keeps the chat surface (header, banner, return button) alive instead of
  // dead-ending on "choose an agent". The bare id stays in `id` so queue and
  // cancel-tool payloads keep the bare spelling (trap 2).
  function overrideAgentDisplayStandIn(agentAddress) {
    const { agentId } = parseAgentAddress(agentAddress);
    return {
      id: agentId,
      name: agentId || agentAddress,
      current_session_id: '',
      context_window: null,
      __overrideAddress: agentAddress,
    };
  }

  // Shape a projected team member into the minimal agent-like object the chat
  // surface renders (header name, token badge context window). The local
  // session id stands in for `current_session_id` so the existing session
  // machinery reads it without a special case.
  function projectAgentAsAgent(member) {
    if (!member) {
      return null;
    }
    const addressing = resolveAgentAddressing(
      member.agent_id,
      selectedProjectId,
      true,
    );
    return {
      id: member.agent_id,
      name: member.display_name || member.agent_id,
      current_session_id: projectAgentSessions[addressing.agentAddress] ?? '',
      context_window: null,
      __projectAddress: addressing.agentAddress,
    };
  }

  function agentById(agentId) {
    return chatState.agents.find((agent) => agent.id === agentId) ?? null;
  }

  function getActiveSessionState() {
    if (viewingSessionId) {
      const agentAddress = overrideAgentAddress();
      return agentAddress
        ? (chatState.sessions[`${agentAddress}::${viewingSessionId}`] ?? null)
        : null;
    }
    if (projectAgentActive) {
      const { agentAddress, sessionId } = activeAddressing();
      if (!agentAddress || !sessionId) {
        return null;
      }
      return chatState.sessions[`${agentAddress}::${sessionId}`] ?? null;
    }
    return currentSessionState(chatState);
  }

  function displayedSessionKey() {
    if (viewingSessionId) {
      const agentAddress = overrideAgentAddress();
      return agentAddress ? `${agentAddress}::${viewingSessionId}` : '';
    }
    if (projectAgentActive) {
      const { agentAddress, sessionId } = activeAddressing();
      return agentAddress && sessionId ? `${agentAddress}::${sessionId}` : '';
    }
    const agent = selectedAgent(chatState);
    const sessionId = agent?.current_session_id;
    return agent?.id && sessionId ? `${agent.id}::${sessionId}` : '';
  }

  // The project the displayed session runs under (parsed from its agent
  // address). Used to qualify bare child-agent ids from persisted spawn
  // descriptors: a project run's children live under the same project anchor,
  // so their history/navigation RPCs need the full `child@projekt` address
  // (trap 2), while the status projection stays keyed by the bare id.
  function displayedSessionProjectId() {
    const key = displayedSessionKey();
    const separator = key.indexOf('::');
    const agentPart = separator >= 0 ? key.slice(0, separator) : '';
    const { projectId } = parseAgentAddress(agentPart);
    return projectId || '';
  }

  function qualifiedChildAgentAddress(agentId) {
    const bareId = typeof agentId === 'string' ? agentId.trim() : '';
    if (!bareId || bareId.includes('@')) {
      return bareId;
    }
    const projectId = displayedSessionProjectId();
    return projectId ? formatAgentAddress(bareId, projectId) : bareId;
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
      handleSelectAgent(sharedSelectedAgentId, { focusComposer: false });
    }
  });

  // React to the project dropdown selection. Choosing a project loads its
  // scan team + report (second bar). Selecting "No project" (Personal) tears the
  // second bar down and the chat falls back to the identity path — byte-
  // identical to today. Guarded by `lastLoadedProjectId` so the load runs once
  // per choice.
  //
  // The first run after mount is the reload restore: it honors the remembered
  // project agent (a team-member id, or '' = an identity agent was active so no
  // team member is opened, or null = nothing remembered → default). Every later
  // run is a user-initiated switch, which jumps to the project default —
  // `restoreAgentId === null` signals that.
  $effect(() => {
    const projectId = isProjectSelected(selectedProjectId)
      ? selectedProjectId
      : '';
    if (projectId === lastLoadedProjectId) {
      return;
    }
    const isInitialRestore = !initialProjectRestoreDone;
    const restoreAgentId = isInitialRestore
      ? (sharedSelectedProjectAgentId ?? null)
      : null;
    initialProjectRestoreDone = true;
    lastLoadedProjectId = projectId;
    if (!projectId) {
      clearProjectContext();
      if (!isInitialRestore) {
        requestComposerFocus();
      }
      return;
    }
    // The initial (reload) restore must not clear a session override that a
    // mount-adopted history entry has just applied — the override stays the
    // displayed session, the member session loads invisibly behind it.
    void loadProjectTeam(projectId, {
      restoreAgentId,
      keepOverride: isInitialRestore,
    }).then(() => {
      if (
        !isInitialRestore &&
        selectedProjectId === projectId &&
        selectedProjectAgentId
      ) {
        requestComposerFocus();
      }
    });
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
      loadAgents({ preferredAgentId: sharedSelectedAgentId });
    }
  });

  $effect(() => {
    chatController.handleServerEvents(runServerEvent, runServerEvents);
  });

  let lastActivityRefreshKey = '';
  $effect(() => {
    const addresses = [
      ...chatState.agents.map((agent) => agent.id),
      ...projectTeam.map((member) =>
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
    const sessionState = activeSessionState;
    const unreadRunId = sessionState?.unreadRunId ?? '';
    if (
      !unreadRunId ||
      sessionState.markReadFailedRunId === unreadRunId ||
      !isDisplayedSession(sessionState.agentId, sessionState.sessionId) ||
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
    return () => chatController.destroy();
  });

  // Reload command/skill suggestions whenever the active address or live command
  // catalog changes. The token does not disturb the draft or active selection.
  $effect(() => {
    const { agentAddress } = activeAddressing();
    const commandsKey = `${commandsRefreshToken}:${agentAddress}`;
    if (commandsKey === lastCommandsAddress) {
      return;
    }
    lastCommandsAddress = commandsKey;
    loadCommands(agentAddress);
  });

  onDestroy(() => {
    if (chatToastTimeoutId !== null) {
      clearTimeout(chatToastTimeoutId);
      chatToastTimeoutId = null;
    }
  });

  // Transient cards belong to the displayed session only. Switching sessions
  // (or the page reloading) drops them; reloading the same session's history
  // (e.g. after /compact) does not, because the displayed key is unchanged.
  $effect(() => {
    const key = displayedSessionKey();
    if (key !== generationSessionKey) {
      generationSessionKey = key;
      displayedSessionGeneration += 1;
    }
    if (key !== transientCardsSessionKey) {
      transientCardsSessionKey = key;
      transientCards = [];
    }
  });

  const showChatToast = (message) => {
    if (chatToastTimeoutId !== null) {
      clearTimeout(chatToastTimeoutId);
      chatToastTimeoutId = null;
    }

    chatToast = typeof message === 'string' ? message : '';

    if (!chatToast) {
      return;
    }

    chatToastTimeoutId = setTimeout(() => {
      chatToast = '';
      chatToastTimeoutId = null;
    }, CHAT_TOAST_TIMEOUT_MS);
  };

  const clearSessionActionError = (sessionState = activeSessionState) => {
    chatState.actionError = '';
    if (sessionState) {
      sessionState.actionError = '';
    }
  };

  const setSessionActionError = (
    message,
    sessionState = activeSessionState,
  ) => {
    if (sessionState) {
      sessionState.actionError = message;
      return;
    }
    chatState.actionError = message;
  };

  const appendTransientCard = (text, sessionState) => {
    const body = typeof text === 'string' ? text : '';
    if (!body) {
      return;
    }
    // Anchor the card to the timeline item present when the command ran, so it
    // stays at that position (like a chat message) instead of being pushed to
    // the bottom by later messages. `null` anchors a card created on an empty
    // timeline to the top.
    const items = visibleTimelineItemsForRender(sessionState);
    const anchorId = items.length > 0 ? items[items.length - 1].id : null;
    transientCardSeq += 1;
    transientCards = [
      ...transientCards,
      { id: `transient-${transientCardSeq}`, text: body, anchorId },
    ];
  };

  const loadCommands = (agentAddress) =>
    chatController.loadCommands(agentAddress);

  const loadAgents = (options = {}) => chatController.loadAgents(options);

  const loadCurrentHistory = () => {
    chatState.actionError = '';
    return chatController.loadCurrentHistory();
  };

  // Load a session's history by its outside agent spelling (bare id for an
  // identity session, `agent@projekt` for a project-agent session) — one path
  // for both worlds, since `chat.history` parses the address (trap 2).
  //
  // Stale-response discipline: the displayed session can change while
  // `chat.history` is in flight (rapid switching, fast Back/Forward). After
  // every await, per-session state may always be written (each response lands
  // in its own session state), but global UI state (`chatState.loadingHistory`,
  // `chatState.historyError`) and the SSE stream attach belong to the DISPLAYED session
  // only — a stale response must not re-open a subscription the newer
  // navigation just closed, unlock the composer early, or banner-error a
  // healthy session.
  const loadHistoryForSession = (agentId, sessionId) => {
    chatState.actionError = '';
    return chatController.loadHistoryForSession(agentId, sessionId);
  };

  const loadOlderHistory = () =>
    chatController.loadOlderHistory(activeSessionState);

  // Tear the second bar down: back to the identity-only chat (Personal).
  const clearProjectContext = () => {
    projectTeam = [];
    projectReport = null;
    projectScanError = '';
    selectedProjectAgentId = '';
    onProjectAgentSelected?.('');
    loadingProjectTeam = false;
  };

  // Load a project's scan team (second bar) and report (banner) via
  // `project.show` (live re-scan), then choose the active agent. An empty team
  // is valid: the second bar simply renders empty, no error. The report is kept
  // for the banner, shown only when the scan was not clean.
  //
  // `restoreAgentId` decides who becomes active:
  //   - `null` — a genuine project switch: jump to the default agent (else the
  //     first team member).
  //   - `''` — a reload restore where an identity agent was active alongside the
  //     project: open no team member, the identity bar stays in control.
  //   - a team-member id — a reload restore: reopen that member if it is still
  //     on the team, otherwise fall through to the default.
  const loadProjectTeam = async (
    projectId,
    { restoreAgentId = null, keepOverride = false } = {},
  ) => {
    loadingProjectTeam = true;
    projectScanError = '';
    selectedProjectAgentId = '';
    try {
      const result = await chatController.loadProject(projectId);
      // A newer selection may have superseded this one mid-flight.
      if (selectedProjectId !== projectId) {
        return;
      }
      projectTeam = normalizeProjectTeam(result?.scan);
      projectReport = normalizeScanReport(result?.scan?.report);
      if (restoreAgentId !== null) {
        if (restoreAgentId === '') {
          return;
        }
        const remembered = projectTeam.find(
          (member) => member.agent_id === restoreAgentId,
        );
        if (remembered) {
          await openProjectAgent(remembered.agent_id, { keepOverride });
          return;
        }
      }
      const defaultAgentId = defaultProjectAgentId(result?.project);
      const target =
        projectTeam.find((member) => member.agent_id === defaultAgentId) ??
        projectTeam[0] ??
        null;
      if (target) {
        await openProjectAgent(target.agent_id, { keepOverride });
      }
    } catch (error) {
      if (selectedProjectId !== projectId) {
        return;
      }
      projectTeam = [];
      projectReport = null;
      projectScanError = `${t('chat.project.loadError', 'The project team could not be loaded.')} ${error.message}`;
    } finally {
      if (selectedProjectId === projectId) {
        loadingProjectTeam = false;
      }
    }
  };

  function defaultProjectAgentId(project) {
    const value = project?.default_agent;
    return typeof value === 'string' ? value.trim() : '';
  }

  // Switch the chat to a project team agent. Clears any identity-side session
  // override and resolves the project agent's session locally (trap 1): the
  // most recent from `session.list`, else a fresh `session.create`. The session
  // is held in `projectAgentSessions` keyed by the agent's full address.
  const openProjectAgent = async (agentId, { keepOverride = false } = {}) => {
    const hadOverride = sessionOverrideActive;
    if (!keepOverride) {
      clearSessionOverride();
    }
    selectedProjectAgentId = agentId;
    onProjectAgentSelected?.(agentId);
    if (!keepOverride && hadOverride) {
      // An override cleared by switching agents is an override change and
      // becomes a history entry, mirroring the identity chip path.
      reportSessionNavigation();
    }
    const addressing = resolveAgentAddressing(agentId, selectedProjectId, true);
    await ensureProjectAgentSession(addressing);
  };

  // Choose (and if needed create) the local session for a project agent, then
  // load its history. `session.list`/`session.create`/`chat.history` all take
  // the FULL address (`agent@projekt`) — trap 2.
  const ensureProjectAgentSession = async (addressing) => {
    const { agentAddress } = addressing;
    clearSessionActionError();
    try {
      const newestUnreadSession = newestUnreadSessionForAgent(
        chatState,
        agentAddress,
      );
      let sessionId =
        newestUnreadSession?.sessionId ??
        projectAgentSessions[agentAddress] ??
        '';
      if (!sessionId) {
        const listed = await chatController.listSessions(agentAddress);
        // A newer project/agent selection may have superseded this one.
        if (currentProjectAgentAddress() !== agentAddress) {
          return;
        }
        sessionId = pickProjectAgentSessionId(listed?.sessions);
        if (!sessionId) {
          const created = await chatController.createSession({
            agent_id: agentAddress,
          });
          if (currentProjectAgentAddress() !== agentAddress) {
            return;
          }
          sessionId = created?.session_id ?? '';
        }
        if (!sessionId) {
          return;
        }
        projectAgentSessions = {
          ...projectAgentSessions,
          [agentAddress]: sessionId,
        };
      }
      await loadHistoryForSession(agentAddress, sessionId);
    } catch (error) {
      if (currentProjectAgentAddress() !== agentAddress) {
        return;
      }
      setSessionActionError(
        `${t('chat.project.sessionError', 'The project agent session could not be opened.')} ${error.message}`,
      );
    }
  };

  // The address of the currently active project agent, '' when none. Used to
  // drop the results of a superseded async session resolution.
  function currentProjectAgentAddress() {
    if (!projectAgentActive) {
      return '';
    }
    return resolveAgentAddressing(
      selectedProjectAgentId,
      selectedProjectId,
      true,
    ).agentAddress;
  }

  const handleSelectProject = (projectId) => {
    const next = isProjectSelected(projectId) ? projectId : '';
    if (
      next === (isProjectSelected(selectedProjectId) ? selectedProjectId : '')
    ) {
      return;
    }
    onProjectSelected?.(next);
  };

  const handleSelectProjectAgent = async (agentId) => {
    if (!agentId) {
      return;
    }
    const agentAddress = formatAgentAddress(agentId, selectedProjectId);
    if (
      agentId === selectedProjectAgentId &&
      !newestUnreadSessionForAgent(chatState, agentAddress)
    ) {
      return;
    }
    await openProjectAgent(agentId);
    requestComposerFocus();
  };

  const handleSelectAgent = async (agentId, { focusComposer = true } = {}) => {
    // Choosing an identity agent always returns the chat to the identity bar,
    // tearing down any active project-agent selection (the upper bar wins for
    // the identity path; the project stays selected in the dropdown so its
    // team bar remains, but the active chat is the identity agent).
    selectedProjectAgentId = '';
    onProjectAgentSelected?.('');
    const unreadSession = newestUnreadSessionForAgent(chatState, agentId);
    if (unreadSession) {
      clearSessionOverride();
      selectAgent(chatState, agentId);
      onAgentSelected?.(agentId);
      const currentSessionId =
        selectedAgent(chatState)?.current_session_id ?? '';
      viewingSessionId =
        unreadSession.sessionId === currentSessionId
          ? ''
          : unreadSession.sessionId;
      viewingSessionAgentId = '';
      viewingSubAgentSession = false;
      reportSessionNavigation();
      await loadHistoryForSession(agentId, unreadSession.sessionId);
      if (focusComposer) {
        requestComposerFocus();
      }
      return;
    }
    if (agentId === chatState.selectedAgentId) {
      if (sessionOverrideActive) {
        clearSessionOverride();
        reportSessionNavigation();
        await loadCurrentHistory();
        if (focusComposer) {
          requestComposerFocus();
        }
      }
      return;
    }
    clearSessionOverride();
    selectAgent(chatState, agentId);
    onAgentSelected?.(agentId);
    reportSessionNavigation();
    await loadCurrentHistory();
    if (focusComposer) {
      requestComposerFocus();
    }
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
    const selectionChanged = await applyNavigationSelection(
      navigation.selection,
    );

    if (navigation.returnToCurrent) {
      const hadOverride = sessionOverrideActive;
      clearSessionOverride();
      if (hadOverride || selectionChanged) {
        await loadActiveOwnHistory();
      }
      return;
    }

    if (navigation.subAgent === true) {
      if (navigation.followSession === true) {
        subAgentLinkFollowRequestId += 1;
        subAgentLinkFollowRequest = {
          requestId: subAgentLinkFollowRequestId,
          sessionKey: `${navigation.agentId}::${navigation.sessionId}`,
        };
      }
      await handleSubAgentNavigation(navigation.agentId, navigation.sessionId);
      return;
    }

    viewingSessionAgentId =
      navigation.agentId === activeOwnAgentAddress() ? '' : navigation.agentId;
    viewingSubAgentSession = false;
    viewingSessionId = navigation.sessionId;
    await loadHistoryForSession(navigation.agentId, navigation.sessionId);
  };

  // Restore the selection half of a history entry: the selected identity
  // agent, the chosen project, and the active project agent. Applied here
  // (not in App) so the restore never routes through the user-action handlers
  // that report navigation — the mirrors converge through the non-pushing
  // callbacks, and the watching prop effects are pre-synced
  // (`lastSharedSelectedAgentId`/`lastLoadedProjectId`) so the round-trip
  // cannot re-run the restore as a fresh user action. Returns whether the
  // active chat target changed (the caller then reloads the current view).
  const applyNavigationSelection = async (selection) => {
    if (!selection) {
      return false;
    }
    let changed = false;

    const agentId =
      typeof selection.agentId === 'string' ? selection.agentId : '';
    if (
      agentId &&
      agentId !== chatState.selectedAgentId &&
      chatState.agents.some((agent) => agent.id === agentId)
    ) {
      selectAgent(chatState, agentId);
      lastSharedSelectedAgentId = agentId;
      onAgentSelected?.(agentId);
      changed = true;
    }

    const projectId = isProjectSelected(selection.projectId)
      ? selection.projectId
      : '';
    const projectAgentId =
      typeof selection.projectAgentId === 'string'
        ? selection.projectAgentId
        : '';
    if (projectId !== lastLoadedProjectId) {
      // Same imperative ownership as the /agent move: pre-sync the guard so
      // the dropdown-watching effect treats the round-tripped prop as
      // already loaded instead of jumping to the project default.
      initialProjectRestoreDone = true;
      lastLoadedProjectId = projectId;
      onProjectSelected?.(projectId);
      changed = true;
      if (!projectId) {
        clearProjectContext();
      } else {
        selectedProjectAgentId = '';
        await loadProjectTeamForMove(projectId);
      }
    }
    if (projectId && projectAgentId !== selectedProjectAgentId) {
      selectedProjectAgentId = projectAgentId;
      onProjectAgentSelected?.(projectAgentId);
      changed = true;
    }
    return changed;
  };

  const handleSessionSelected = async (sessionId) => {
    const agentAddress = activeAddressing().agentAddress;
    const normalizedSessionId = String(sessionId ?? '').trim();
    if (!agentAddress || !normalizedSessionId) {
      return;
    }

    // The drawer lists the displayed agent's sessions. Picking one of the
    // active agent's own sessions is a same-agent past-session view (or a
    // return to its current session); picking while a cross-agent override is
    // displayed keeps that agent's framing. The address form serves both
    // worlds — a project agent's sessions go through `agent@projekt`.
    const isOwnAgent = agentAddress === activeOwnAgentAddress();
    const ownCurrentSessionId = isOwnAgent
      ? projectAgentActive
        ? (projectAgentSessions[agentAddress] ?? '')
        : (selectedAgent(chatState)?.current_session_id ?? '')
      : '';
    viewingSessionAgentId = isOwnAgent ? '' : agentAddress;
    viewingSubAgentSession = !isOwnAgent;
    viewingSessionId =
      isOwnAgent && normalizedSessionId === ownCurrentSessionId
        ? ''
        : normalizedSessionId;
    reportSessionNavigation();
    await loadHistoryForSession(agentAddress, normalizedSessionId);
    requestComposerFocus();
  };

  // A session was deleted from the drawer. If this window was viewing it (the
  // current session, or an explicit override on it), navigate to the landing the
  // server chose (#2: most-recently-active remaining, else a fresh session);
  // otherwise stay put and let the list refresh. The server re-aims the identity
  // current pointer and emits resource_changed(agents), so the current marking
  // converges across windows and the override below reconciles to it.
  const handleSessionDeleted = async ({
    deletedSessionId,
    nextSessionId,
  } = {}) => {
    const removedId = String(deletedSessionId ?? '').trim();
    const landingId = String(nextSessionId ?? '').trim();
    if (!removedId) {
      return;
    }
    const agent = activeAgent;
    const viewedSessionId = viewingSessionId || agent?.current_session_id || '';
    if (viewedSessionId === removedId && landingId) {
      await handleSessionSelected(landingId);
    }
  };

  // When the active agent's current session catches up to a same-agent override
  // that now points at it — e.g. the server re-aimed current after we deleted the
  // session we were viewing (#2) — the override is redundant. Drop it so the view
  // reads as "on current" with no leftover return banner. Sub-agent session views
  // (viewingSessionAgentId set) are deliberately excluded.
  $effect(() => {
    if (
      viewingSessionId &&
      !viewingSessionAgentId &&
      activeAgent?.current_session_id === viewingSessionId
    ) {
      viewingSessionId = '';
      viewingSessionAgentId = '';
      viewingSubAgentSession = false;
    }
  });

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
            agentId: viewingSessionAgentId || activeOwnAgentAddress(),
            sessionId: viewingSessionId,
            subAgent: viewingSubAgentSession,
          }
        : null,
    );
  };

  // Load the active agent's own current view after an override was cleared:
  // the project team agent's locally chosen session when one is active, else
  // the selected identity agent's current session.
  const loadActiveOwnHistory = async () => {
    if (projectAgentActive) {
      await ensureProjectAgentSession(
        resolveAgentAddressing(selectedProjectAgentId, selectedProjectId, true),
      );
      return;
    }
    await loadCurrentHistory();
  };

  const handleReturnToCurrentSession = async () => {
    if (!subAgentSessionActive || chatState.loadingHistory) {
      return;
    }

    // A sub-agent session returns to its PARENT session (from the child's
    // `subagent_parent` metadata). Without resolvable parent metadata (old
    // child sessions, deleted parent agent) the button falls back to the
    // return-to-current behavior below.
    if (subAgentSessionActive && subAgentParentTarget) {
      await navigateToParentSession(subAgentParentTarget);
      requestComposerFocus();
      return;
    }

    clearSessionOverride();
    reportSessionNavigation();
    await loadActiveOwnHistory();
    requestComposerFocus();
  };

  // User-initiated navigation from a child session to its parent session: a
  // normal session navigation, so it reports up and becomes a history push —
  // Back returns to the child. A parent that is itself a Sub-Agent Session
  // keeps the contextual banner so another parent step remains available;
  // the root parent returns to ordinary Session presentation.
  const navigateToParentSession = async ({
    agentAddress,
    sessionId,
    isSubAgentSession = false,
  }) => {
    const ownAddress = activeOwnAgentAddress();
    const ownCurrentSessionId = projectAgentActive
      ? (projectAgentSessions[ownAddress] ?? '')
      : (selectedAgent(chatState)?.current_session_id ?? '');
    viewingSubAgentSession = isSubAgentSession;
    if (agentAddress === ownAddress && sessionId === ownCurrentSessionId) {
      clearSessionOverride();
    } else {
      viewingSessionAgentId = agentAddress === ownAddress ? '' : agentAddress;
      viewingSessionId = sessionId;
    }
    reportSessionNavigation();
    await loadHistoryForSession(agentAddress, sessionId);
  };

  const handleNewSession = async () => {
    if (newSessionBlocked || chatState.loadingHistory || creatingSession) {
      return;
    }
    // "New session" means "make the chat ready for a fresh conversation."
    // Repeating it on an already blank Session is therefore idempotent, while
    // a local draft still counts as work that deserves its own Session.
    requestComposerFocus({ includeMobile: true });
    if (displayedSessionIsEmpty()) {
      return;
    }
    if (projectAgentActive) {
      // Symmetric with the identity path below: a new session always leaves
      // any override view and becomes the displayed session.
      clearSessionOverride();
      reportSessionNavigation();
      if (await createProjectAgentSession()) {
        requestComposerFocus({ includeMobile: true });
      }
      return;
    }
    const agent = selectedAgent(chatState);
    if (!agent) {
      return;
    }
    const sourceSessionState = activeSessionState;
    clearSessionOverride();
    creatingSession = true;
    clearSessionActionError(sourceSessionState);
    try {
      const session = await chatController.createSession({
        agent_id: agent.id,
        make_current: true,
      });
      await switchToCurrentSession(agent.id, session.session_id);
      requestComposerFocus({ includeMobile: true });
    } catch (error) {
      setSessionActionError(
        `${t('chat.sessionCreateError', 'New session could not be created.')} ${error.message}`,
        sourceSessionState,
      );
    } finally {
      creatingSession = false;
    }
  };

  // New session for a project agent: `session.create` with the full address and
  // NO `make_current` (the backend ignores it for project agents anyway — trap
  // 1), then point the local session store at it and load it.
  const createProjectAgentSession = async () => {
    const agentAddress = currentProjectAgentAddress();
    if (!agentAddress) {
      return false;
    }
    const sourceSessionState = activeSessionState;
    creatingSession = true;
    clearSessionActionError(sourceSessionState);
    try {
      const created = await chatController.createSession({
        agent_id: agentAddress,
      });
      const sessionId = created?.session_id ?? '';
      if (!sessionId || currentProjectAgentAddress() !== agentAddress) {
        return false;
      }
      projectAgentSessions = {
        ...projectAgentSessions,
        [agentAddress]: sessionId,
      };
      await loadHistoryForSession(agentAddress, sessionId);
      return true;
    } catch (error) {
      setSessionActionError(
        `${t('chat.sessionCreateError', 'New session could not be created.')} ${error.message}`,
        sourceSessionState,
      );
      return false;
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

  // `/agent <addr>` move: relocate the CURRENT session (same session id) to the
  // target agent's home and open it there. Unlike `/handoff` (which summarizes
  // into a NEW session), the session id is unchanged — the move happened on the
  // backend already; the accessor just opens it under the target. The target's
  // outside address decides the world (the one signal: presence of `@`), so the
  // same handler crosses every direction (identity↔project, both ways). It
  // reuses the two-bar machinery: an identity target goes through the bare-id
  // current-session path, a project target through the project-bar path.
  const moveSessionToAgent = async (move) => {
    if (!move?.sessionId || !move?.bareAgentId) {
      return;
    }
    if (move.isProjectTarget) {
      await moveToProjectAgent(move);
      return;
    }
    moveToIdentityAgent(move);
    await switchToCurrentSession(move.bareAgentId, move.sessionId);
  };

  // Identity target: drop any active project-agent bar so the chat returns to
  // the identity world (the project stays chosen in the dropdown, but the active
  // chat is the identity agent), then select the target identity agent. The
  // session switch itself is `switchToCurrentSession` (caller).
  const moveToIdentityAgent = (move) => {
    selectedProjectAgentId = '';
    onProjectAgentSelected?.('');
    if (move.bareAgentId !== chatState.selectedAgentId) {
      selectAgent(chatState, move.bareAgentId);
      onAgentSelected?.(move.bareAgentId);
    }
  };

  // Project target: open the SAME session under the project team agent. The
  // project context is set locally (so the second bar reflects it immediately,
  // crossing the agent/project boundary) and reported up so App persists it for
  // the next reload — mirroring `openProjectAgent`, but the session is the moved
  // one, pre-seeded into `projectAgentSessions` so `ensureProjectAgentSession`
  // reuses it instead of picking/creating.
  //
  // The move owns the transition imperatively rather than waiting on the
  // dropdown-driven effect: `lastLoadedProjectId` is set to the target up front
  // so the `selectedProjectId`-watching effect treats it as already-loaded and
  // never re-runs `loadProjectTeam` with the project default (which would
  // clobber the moved agent/session). `selectedProjectAgentId` makes
  // `projectAgentActive`/`activeAddressing` resolve to the target before the
  // round-tripped `selectedProjectId` prop has flushed, so the chat does not
  // depend on that flush timing.
  const moveToProjectAgent = async (move) => {
    clearSessionOverride();
    const { projectId, bareAgentId, agentAddress, sessionId } = move;
    projectAgentSessions = {
      ...projectAgentSessions,
      [agentAddress]: sessionId,
    };
    initialProjectRestoreDone = true;
    lastLoadedProjectId = projectId;
    selectedProjectAgentId = bareAgentId;
    onProjectSelected?.(projectId);
    onProjectAgentSelected?.(bareAgentId);
    await loadProjectTeamForMove(projectId);
    await ensureProjectAgentSession(
      resolveAgentAddressing(bareAgentId, projectId, true),
    );
  };

  // Load just the team + report for a move target (no agent auto-selection —
  // the move picks the agent itself). Errors surface as the scan error notice.
  const loadProjectTeamForMove = async (projectId) => {
    loadingProjectTeam = true;
    projectScanError = '';
    try {
      const result = await chatController.loadProject(projectId);
      projectTeam = normalizeProjectTeam(result?.scan);
      projectReport = normalizeScanReport(result?.scan?.report);
    } catch (error) {
      projectTeam = [];
      projectReport = null;
      projectScanError = `${t('chat.project.loadError', 'The project team could not be loaded.')} ${error.message}`;
    } finally {
      loadingProjectTeam = false;
    }
  };

  // Spawn-row "view session" links carry the child's BARE agent id from the
  // persisted descriptor. A project run's child lives under the same project
  // anchor, so the navigation address must be qualified as `child@projekt`
  // before it reaches the App-level navigation (identity children pass
  // through unchanged).
  const handleNavigateToSubAgentLink = (target) => {
    if (!target?.agentId || !target?.sessionId) {
      return;
    }
    const agentId = qualifiedChildAgentAddress(target.agentId);
    navigateToSubAgent({
      ...target,
      agentId,
    });
  };

  const handleTranscriptionError = (message) => {
    setSessionActionError(message);
  };

  const sendStream = async (agent, sessionState, content, options = {}) => {
    const sourceSessionKey = sessionState?.key ?? '';
    const sourceUiGeneration = displayedSessionGeneration;
    const outcome = await chatController.sendMessage(
      sessionState,
      content,
      options,
    );
    const presentationIsCurrent =
      sourceSessionKey &&
      displayedSessionKey() === sourceSessionKey &&
      displayedSessionGeneration === sourceUiGeneration;
    if (!presentationIsCurrent) {
      return outcome.kind !== 'failed' && outcome.kind !== 'ignored';
    }
    if (outcome.kind === 'move') {
      await moveSessionToAgent(outcome.move);
      requestComposerFocus({ includeMobile: true });
    } else if (outcome.kind === 'switch') {
      const targetAgentId = outcome.sessionSwitch.targetAgentId || agent.id;
      if (targetAgentId !== chatState.selectedAgentId) {
        selectAgent(chatState, targetAgentId);
        onAgentSelected?.(targetAgentId);
      }
      await switchToCurrentSession(
        targetAgentId,
        outcome.sessionSwitch.sessionId,
      );
      requestComposerFocus({ includeMobile: true });
    } else if (outcome.kind === 'transient') {
      appendTransientCard(outcome.reply, sessionState);
    } else if (outcome.kind === 'toast') {
      showChatToast(outcome.reply);
    } else if (outcome.kind === 'started') {
      submittedTurnScrollRunId = outcome.runId;
      submittedTurnScrollKey += 1;
    }
    return outcome.kind !== 'failed' && outcome.kind !== 'ignored';
  };

  const handleCancelRun = async () => {
    await chatController.cancelActiveRun(activeSessionState);
  };

  // Per-tool-call cancel: cancel the bash without aborting the owning run.
  const handleCancelToolCall = async ({ runId, toolCallId } = {}) => {
    const agent = activeAgent;
    await chatController.cancelTool({
      sessionState: activeSessionState,
      agentId: agent?.id ?? '',
      runId,
      toolCallId,
    });
  };

  const handleCancelSubAgent = async ({ tool } = {}) => {
    await chatController.cancelSubAgent({
      tool,
      sessionState: activeSessionState,
      projectId: displayedSessionProjectId(),
    });
  };

  const handleCancelBackgroundProcess = async ({ processSessionId } = {}) => {
    await chatController.cancelBackgroundProcess({
      sessionState: activeSessionState,
      agentId: activeSessionState?.agentId ?? activeAgent?.id ?? '',
      processSessionId,
      projectId: displayedSessionProjectId(),
    });
  };

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
      projectId: displayedSessionProjectId(),
    });
  }

  // Exposed for tests (mirrors `verifySubAgentStatus`): drives the per-row
  // sub-agent cancel exactly as the timeline button's callback does.
  export async function cancelSubAgent(tool) {
    await handleCancelSubAgent({ tool });
  }

  const handleRemoveQueuedMessage = async (queuedMessageId) => {
    const sessionState = activeSessionState;
    await chatController.removeQueued(sessionState, queuedMessageId);
  };

  const handleEditQueuedMessage = async (queuedMessageId, newContent) => {
    const sessionState = activeSessionState;
    if (!sessionState) {
      return false;
    }
    const fileMentions = await collectQueueEditFileMentions(
      newContent,
      sessionState.agentId,
    );
    return await chatController.updateQueued(
      sessionState,
      queuedMessageId,
      newContent,
      fileMentions,
    );
  };

  const collectQueueEditFileMentions = async (text, agentAddress) => {
    const tokens = extractMentionTokens(typeof text === 'string' ? text : '');
    if (tokens.length === 0) {
      return [];
    }
    try {
      if (!agentAddress) {
        return [];
      }
      const result = await chatController.listFiles(agentAddress);
      return matchMentionCandidates(
        tokens,
        Array.isArray(result?.files) ? result.files : [],
      );
    } catch {
      // Without a file list nothing can be verified as a mention; the edit
      // still goes through as plain text.
      return [];
    }
  };

  let chatController;
  const runStream = createChatRunStream({
    chatState,
    subscribeRunEvents,
    syncSessionQueue: (sessionState) =>
      chatController.syncSessionQueue(sessionState),
    reconcileRunSession: (sessionState, expectedRunId) =>
      chatController.reconcileRunSession(sessionState, expectedRunId),
    isDisplayedSession,
    updateSubAgentRunStatuses: (updates, options) =>
      chatController.applySubAgentStatusUpdates(updates, options),
  });
  chatController = createChatController({
    chatState,
    runStream,
    translate: t,
    isDisplayedSession,
    shouldLoadCurrentHistory: () => !viewingSessionId && !projectAgentActive,
    onAgentsChanged: (agents) => onAgentsChanged?.(agents),
    onAgentSelected: (agentId) => onAgentSelected?.(agentId),
    onRestartQueueDiscarded: (count) => {
      showChatToast(
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
      projectId: displayedSessionProjectId(),
    });
  });
</script>

<section
  class="view view-chat active chat-view"
  data-chat-width={chatWidth}
  aria-labelledby="chat-title"
  hidden={!active}
>
  <ChatHeader
    agents={chatState.agents}
    agentStatuses={identityAgentStatuses}
    selectedAgentId={displayedIdentityAgentId}
    loadingAgents={chatState.loadingAgents}
    {activeAgent}
    {activeSessionState}
    {showSessionDrawer}
    {creatingSession}
    {newSessionBlocked}
    newSessionLoading={chatState.loadingHistory}
    {projects}
    {selectedProjectId}
    onSelectProject={handleSelectProject}
    {wakewordStatus}
    {desktopCapabilities}
    onSelectAgent={handleSelectAgent}
    onToggleSessionDrawer={() => {
      showSessionDrawer = !showSessionDrawer;
    }}
    onNewSession={handleNewSession}
    {onNavigateToVoiceSettings}
  />

  {#if isProjectSelected(selectedProjectId)}
    <ProjectScanBanner report={projectReport} {onNavigateToProjects} />
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
          )}>{selectedProjectName}</span
        >
        {#if loadingProjectTeam}
          <span class="chat-view__project-team-empty">
            {t('loading.agents', 'Loading agents…')}
          </span>
        {:else if projectScanError}
          <span class="chat-view__project-team-error">{projectScanError}</span>
        {:else if projectTeam.length === 0}
          <span class="chat-view__project-team-empty">
            {t('chat.project.teamEmpty', 'This project has no agents yet.')}
          </span>
        {:else}
          {#each projectTeam as member (member.agent_id)}
            {@const memberName = member.display_name || member.agent_id}
            {@const memberStatus =
              projectAgentStatuses[member.agent_id] ?? 'idle'}
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
            <button
              type="button"
              class="agent-tab chat-view__project-tab"
              class:active={member.agent_id === displayedProjectAgentId}
              aria-label={memberActivityLabel}
              use:tooltip={memberActivityLabel}
              onclick={() => handleSelectProjectAgent(member.agent_id)}
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
  {:else if !activeAgent}
    <EmptyState
      fill
      title={t('chat.noAgentSelected', 'Choose an agent to start chatting.')}
    />
  {:else}
    <div class="chat-view__content-shell">
      {#if showSessionDrawer}
        <SessionListDrawer
          agentId={activeAgentAddress}
          currentSessionId={viewingSessionId || activeAgent.current_session_id}
          reloadToken={sessionsRefreshToken}
          onSessionSelected={handleSessionSelected}
          onSessionDeleted={handleSessionDeleted}
        />
      {/if}
      <div class="chat-view__surface">
        {#if chatState.loadingHistory || chatState.historyError || chatState.actionError || chatState.commandsError || activeSessionState?.actionError || activeSessionState?.streamError || activeSessionState?.error}
          <div class="chat-view__notice-stack" aria-live="polite">
            <div class="chat-view__measure chat-view__notice-inner">
              {#if chatState.loadingHistory}
                <Banner variant="neutral">
                  {t('loading.history', 'Loading chat history…')}
                </Banner>
              {/if}
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
              {#if activeSessionState?.actionError}
                <Banner variant="error">{activeSessionState.actionError}</Banner
                >
              {/if}
              {#if activeSessionState?.streamError}
                <Banner variant="warn">{activeSessionState.streamError}</Banner>
              {/if}
              {#if activeSessionState?.error}
                <Banner variant="error">
                  {t('chat.runError', 'Run failed.')}
                  {activeSessionState.error}
                </Banner>
              {/if}
            </div>
          </div>
        {/if}
        <div class="chat-view__timeline-shell">
          <ChatTimeline
            sessionState={activeSessionState}
            agentName={activeAgent.name}
            loadingHistory={chatState.loadingHistory}
            {transientCards}
            {submittedTurnScrollKey}
            {submittedTurnScrollRunId}
            followSessionRequest={subAgentLinkFollowRequest}
            hasOlderHistory={activeSessionState?.hasOlderHistory === true}
            loadingOlderHistory={activeSessionState?.loadingOlderHistory ===
              true}
            subAgentStatuses={chatState.subAgentStatuses}
            subAgentResults={chatState.subAgentResults}
            onLoadOlder={loadOlderHistory}
            onNavigateToSubAgent={handleNavigateToSubAgentLink}
            onCancelToolCall={handleCancelToolCall}
            onCancelSubAgent={handleCancelSubAgent}
          />
        </div>
        <div class="chat-view__footer-stack">
          {#if subAgentSessionActive}
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
                  {subAgentParentTarget
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
                onClick={handleReturnToCurrentSession}
              >
                {subAgentParentTarget
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
              queuedMessages={activeSessionState?.queue ?? []}
              onRemoveQueuedMessage={handleRemoveQueuedMessage}
              onEditQueuedMessage={handleEditQueuedMessage}
            />
          </div>
          <div class="chat-view__composer-shell">
            {#if chatToast}
              <div
                class="chat-view__command-toast"
                role="status"
                aria-live="polite"
              >
                <p class="chat-view__command-toast-message">{chatToast}</p>
              </div>
            {/if}
            <ChatComposer
              disabled={composerDisabled}
              isRunning={isRunActive(activeSessionState)}
              cancelling={chatState.cancellingRun}
              draftKey={composerDraftKey}
              historyKey={composerHistoryKey}
              focusRequest={composerFocusRequest}
              availableSkills={chatState.availableSkills}
              onSendMessage={composerSendMessage}
              onCancelRun={handleCancelRun}
              onTranscriptionError={handleTranscriptionError}
              onListFiles={composerListFiles}
            />
          </div>
        </div>
      </div>
      <ChatActivityPanel
        timelineItems={activeTimelineItems}
        subAgentStatuses={chatState.subAgentStatuses}
        backgroundBashStatuses={activeSessionState?.backgroundBashStatuses}
        onNavigateToSubAgent={handleNavigateToSubAgentLink}
        onCancelSubAgent={handleCancelSubAgent}
        onCancelBackgroundProcess={handleCancelBackgroundProcess}
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
    flex-direction: column;
    overflow: hidden;
    background: var(--bg);
  }

  .chat-view[hidden] {
    display: none;
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
    position: relative;
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
    background: var(--bg);
  }

  :global(.chat-view__state-banner) {
    align-self: center;
    width: min(calc(100% - 40px), 560px);
    margin-block: auto;
  }

  .chat-view__notice-stack {
    flex-shrink: 0;
    padding: 10px 20px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }

  .chat-view__project-team {
    display: flex;
    flex-shrink: 0;
    min-height: 38px;
    padding: 0 20px;
    border-bottom: 1px solid var(--border);
    background: var(--surface-2);
  }

  /* Left-aligned (no measure cap) so the team bar starts at the same left edge
     as the identity agent tabs above. */
  .chat-view__project-team-inner {
    display: flex;
    align-items: stretch;
    gap: 2px;
    min-width: 0;
    overflow-x: auto;
  }

  /* Bold project-name label before the team tabs, marking the agents as that
     project's team. The trailing divider separates it from the first tab. */
  .chat-view__project-team-name {
    display: flex;
    flex-shrink: 0;
    align-items: center;
    margin-right: 6px;
    padding-right: 12px;
    border-right: 1px solid var(--border);
    color: var(--text-hi);
    font-family: var(--font-ui);
    font-size: var(--fs-label-md);
    font-weight: 600;
    white-space: nowrap;
  }

  .chat-view__project-team-error {
    display: flex;
    align-items: center;
    padding: 0 4px;
    color: var(--red);
    font-size: 12px;
  }

  /* The project team tabs mirror the identity bar's agent tabs (which are
     scoped to ChatHeader), so the visual styling is restated locally. */
  .chat-view__project-team .agent-tab {
    display: flex;
    height: 38px;
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

  .chat-view__project-team .agent-tab:hover,
  .chat-view__project-team .agent-tab:focus-visible {
    color: var(--text-med);
    outline: none;
  }

  .chat-view__project-team .agent-tab.active {
    border-bottom-color: var(--accent);
    color: var(--accent);
  }

  .chat-view__project-team .tab-indicator {
    width: 5px;
    height: 5px;
  }

  .chat-view__project-team-empty {
    display: flex;
    align-items: center;
    padding: 0 4px;
    color: var(--text-lo);
    font-size: 12px;
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

  /* Chat-local bottom toast: floats just above the composer (same anchoring as
     the composer's own attachment-error toast), centered on the chat measure. */
  .chat-view__composer-shell {
    position: relative;
  }

  .chat-view__command-toast {
    position: absolute;
    bottom: calc(100% + 10px);
    left: 0;
    right: 0;
    z-index: 20;
    width: 100%;
    max-width: var(--chat-measure);
    margin-inline: auto;
    padding: 10px 12px;
    border: 1px solid var(--border-2);
    border-left: 2px solid var(--accent);
    border-radius: var(--r-md);
    background: var(--surface);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
  }

  .chat-view__command-toast-message {
    margin: 0;
    color: var(--text-med);
    font-family: var(--font-ui);
    font-size: 12.5px;
    line-height: 1.4;
    white-space: pre-wrap;
  }

  :global(.chat-view__footer-banner) {
    flex-shrink: 0;
    width: 100%;
    max-width: var(--chat-measure);
    margin: 0 auto 10px;
    padding: 9px 20px 9px 12px;
  }

  .chat-view__footer-banner-copy {
    min-width: 0;
  }

  .chat-view__footer-banner-actions {
    display: flex;
    flex-shrink: 0;
    gap: 8px;
  }

  .chat-view__footer-banner-title,
  .chat-view__footer-banner-hint {
    margin: 0;
  }

  .chat-view__footer-banner-title {
    color: var(--accent);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    font-weight: 500;
    letter-spacing: 0.07em;
    text-transform: uppercase;
  }

  .chat-view__footer-banner-hint {
    margin-top: 4px;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
  }

  :global(.chat-view__subagent-session-return) {
    flex-shrink: 0;
  }

  :global(.chat-view__no-model-action) {
    flex-shrink: 0;
  }

  @media (max-width: 640px) {
    .chat-view__notice-stack {
      padding: 10px 14px;
    }

    .chat-view__content-shell {
      flex-direction: column;
    }

    :global(.chat-view__footer-banner) {
      align-items: flex-start;
      flex-direction: column;
    }

    .chat-view__footer-banner-actions {
      width: 100%;
    }

    :global(.chat-view__subagent-session-return) {
      margin-right: 0;
    }
  }
</style>
