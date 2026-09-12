import { formatAgentAddress } from '$lib/agentAddress.js';
import {
  sessionDisplayName,
  sessionParentReference,
} from '../../../lib/sessionListView.js';
import {
  newestUnreadSessionForAgent,
  selectAgent,
  selectedAgent,
  isProjectSelected,
  resolveAgentAddressing,
  ensureSessionState,
  setAgents,
} from '../../../lib/chatState.js';
import { t } from '$lib/i18n.js';

export function createChatViewNavigation(context) {
  let creatingSession = $state(false);

  let viewingSessionId = $state('');

  let viewingSessionAgentId = $state('');

  let viewingSubAgentSession = $state(false);

  let subAgentLinkFollowRequest = $state(null);

  let subAgentLinkFollowRequestId = 0;

  let handledSessionNavigationKey = '';

  let subAgentSessionActive = $derived(
    Boolean(viewingSessionId) && viewingSubAgentSession,
  );

  // Every derived Session resolves its immediate parent from the authoritative
  // Session-list projection: Sub-Agent Sessions use `subagent_parent`, while
  // Fork and Reflection Sessions use `fork_source`. The navigation target is
  // available from provenance alone; the Activity panel link waits for the
  // parent row so it can show the real Session name instead of a raw id.
  let subAgentParentTarget = $state(null);

  let sessionParentLink = $state(null);

  let sessionParentFetchKey = '';

  $effect(() => {
    const addressing = context.target.activeAddressing();
    const displayKey = context.target.displayedSessionKey();
    const fetchKey = `${displayKey}::${context.sessionsRefreshToken}`;
    if (!displayKey || !addressing.agentAddress || !addressing.sessionId) {
      sessionParentFetchKey = '';
      subAgentParentTarget = null;
      sessionParentLink = null;
      return;
    }
    if (fetchKey === sessionParentFetchKey) {
      return;
    }
    sessionParentFetchKey = fetchKey;
    subAgentParentTarget = null;
    sessionParentLink = null;
    loadSessionParent(
      fetchKey,
      displayKey,
      addressing.agentAddress,
      addressing.sessionId,
    );
  });

  const loadSessionParent = async (
    fetchKey,
    displayKey,
    childAddress,
    childSessionId,
  ) => {
    if (!childAddress || !childSessionId) {
      return;
    }
    try {
      const listed = await context.chatController.listSessions(childAddress, {
        limit: 1,
        requiredSession: {
          agentId: childAddress,
          sessionId: childSessionId,
        },
      });
      // A newer navigation or Session invalidation may have superseded this
      // request mid-flight.
      if (
        context.target.displayedSessionKey() !== displayKey ||
        sessionParentFetchKey !== fetchKey
      ) {
        return;
      }
      const childSession = (listed?.sessions ?? []).find(
        (session) => String(session?.id ?? '').trim() === childSessionId,
      );
      const parent = sessionParentReference(childSession);
      if (!parent) {
        return;
      }
      const parentAgentId = parent.agent_id;
      const parentSessionId = parent.session_id;
      const parentProjectId = parent.project_id ?? '';
      // A vanished identity parent cannot be opened — keep the
      // return-to-current fallback (a project parent is not roster-checkable
      // and surfaces a load error instead of dead-ending).
      if (!parentProjectId && !context.target.agentById(parentAgentId)) {
        return;
      }
      const parentAgentAddress = formatAgentAddress(
        parentAgentId,
        parentProjectId,
      );
      const target = {
        agentAddress: parentAgentAddress,
        sessionId: parentSessionId,
        isSubAgentSession: false,
      };
      if (parent.kind === 'subagent') {
        subAgentParentTarget = target;
      }

      // The exact Parent Session may be outside the child's first list page,
      // including same-Agent forks, so resolve it through requiredSession.
      let parentSession = null;
      try {
        const parentListed = await context.chatController.listSessions(
          parentAgentAddress,
          {
            limit: 1,
            requiredSession: {
              agentId: parentAgentAddress,
              sessionId: parentSessionId,
            },
          },
        );
        if (
          context.target.displayedSessionKey() !== displayKey ||
          sessionParentFetchKey !== fetchKey
        ) {
          return;
        }
        parentSession = (parentListed?.sessions ?? []).find(
          (session) => String(session?.id ?? '').trim() === parentSessionId,
        );
      } catch {
        // Provenance still supports the existing Sub-Agent return path, but
        // Session info must not present an unresolved link.
        return;
      }
      if (!parentSession) {
        return;
      }
      target.isSubAgentSession =
        parentSession?.is_subagent_session === true ||
        sessionParentReference(parentSession)?.kind === 'subagent';
      if (parent.kind === 'subagent') {
        subAgentParentTarget = target;
      }
      sessionParentLink = {
        displayName: sessionDisplayName(parentSession),
        target,
      };
    } catch {
      // Best effort — the banner button falls back to return-to-current.
    }
  };

  // Any local override away from the selected agent's current session — also
  // true for same-agent drawer selections, which must offer a return path too.
  let sessionOverrideActive = $derived(Boolean(viewingSessionId));

  // App-driven session navigation: sub-agent link clicks routed through
  // `navigateToSubAgent` and browser-history restores. Both arrive here so
  // they never echo back through `onSessionNavigation` as a new history push.
  $effect(() => {
    const navigation = context.pendingSessionNavigation;
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

  const handleSelectAgent = async (agentId, { focusComposer = true } = {}) => {
    // Choosing an identity agent always returns the chat to the identity bar,
    // tearing down any active project-agent selection (the upper bar wins for
    // the identity path; the project stays selected in the dropdown so its
    // team bar remains, but the active chat is the identity agent).
    context.target.selectedProjectAgentId = '';
    context.onProjectAgentSelected?.('');
    const unreadSession = newestUnreadSessionForAgent(
      context.chatState,
      agentId,
    );
    if (unreadSession) {
      clearSessionOverride();
      selectAgent(context.chatState, agentId);
      context.onAgentSelected?.(agentId);
      const currentSessionId =
        selectedAgent(context.chatState)?.current_session_id ?? '';
      viewingSessionId =
        unreadSession.sessionId === currentSessionId
          ? ''
          : unreadSession.sessionId;
      viewingSessionAgentId = '';
      viewingSubAgentSession = false;
      reportSessionNavigation();
      await context.loadHistoryForSession(agentId, unreadSession.sessionId);
      if (focusComposer) {
        context.layout.requestComposerFocus();
      }
      return;
    }
    if (agentId === context.chatState.selectedAgentId) {
      if (sessionOverrideActive) {
        clearSessionOverride();
        reportSessionNavigation();
        await context.loadCurrentHistory();
        if (focusComposer) {
          context.layout.requestComposerFocus();
        }
      }
      return;
    }
    clearSessionOverride();
    selectAgent(context.chatState, agentId);
    context.onAgentSelected?.(agentId);
    reportSessionNavigation();
    await context.loadCurrentHistory();
    if (focusComposer) {
      context.layout.requestComposerFocus();
    }
  };

  const handleSubAgentNavigation = async (agentId, sessionId) => {
    if (!agentId || !sessionId) {
      return;
    }

    viewingSessionAgentId = agentId;
    viewingSessionId = sessionId;
    viewingSubAgentSession = true;
    await context.loadHistoryForSession(agentId, sessionId);
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
      navigation.agentId === context.target.activeOwnAgentAddress()
        ? ''
        : navigation.agentId;
    viewingSubAgentSession = false;
    viewingSessionId = navigation.sessionId;
    await context.loadHistoryForSession(
      navigation.agentId,
      navigation.sessionId,
    );
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
      agentId !== context.chatState.selectedAgentId &&
      context.chatState.agents.some((agent) => agent.id === agentId)
    ) {
      selectAgent(context.chatState, agentId);
      context.lastSharedSelectedAgentId = agentId;
      context.onAgentSelected?.(agentId);
      changed = true;
    }

    const projectId = isProjectSelected(selection.projectId)
      ? selection.projectId
      : '';
    const projectAgentId =
      typeof selection.projectAgentId === 'string'
        ? selection.projectAgentId
        : '';
    if (projectId !== context.target.lastLoadedProjectId) {
      // Same imperative ownership as the /agent move: pre-sync the guard so
      // the dropdown-watching effect treats the round-tripped prop as
      // already loaded instead of jumping to the project default.
      context.target.initialProjectRestoreDone = true;
      context.target.lastLoadedProjectId = projectId;
      context.onProjectSelected?.(projectId);
      changed = true;
      if (!projectId) {
        context.target.clearProjectContext();
      } else {
        context.target.selectedProjectAgentId = '';
        await context.target.loadProjectTeamForMove(projectId);
      }
    }
    if (projectId && projectAgentId !== context.target.selectedProjectAgentId) {
      context.target.selectedProjectAgentId = projectAgentId;
      context.onProjectAgentSelected?.(projectAgentId);
      changed = true;
    }
    return changed;
  };

  const handleSessionSelected = async (
    sessionId,
    sessionAgentAddress,
    isSubAgentSession,
  ) => {
    // The drawer may list sessions of other agents (All-agents filter); the
    // row's owning address wins, otherwise the displayed agent's own address.
    const agentAddress =
      String(sessionAgentAddress ?? '').trim() ||
      context.target.activeAddressing().agentAddress;
    const normalizedSessionId = String(sessionId ?? '').trim();
    if (!agentAddress || !normalizedSessionId) {
      return;
    }

    // The drawer lists the displayed agent's sessions. Picking one of the
    // active agent's own sessions is a same-agent past-session view (or a
    // return to its current session); picking while a cross-agent override is
    // displayed keeps that agent's framing. The address form serves both
    // worlds — a project agent's sessions go through `agent@projekt`.
    const isOwnAgent = agentAddress === context.target.activeOwnAgentAddress();
    const ownCurrentSessionId = isOwnAgent
      ? context.target.projectAgentActive
        ? (context.target.projectAgentSessions[agentAddress] ?? '')
        : (selectedAgent(context.chatState)?.current_session_id ?? '')
      : '';
    viewingSessionAgentId = isOwnAgent ? '' : agentAddress;
    // The sub-agent notice follows the picked row's real sub-agent flag,
    // not cross-agent-ness: a foreign agent's ordinary session is a normal
    // override view, while the banner (and its parent-return button) stay
    // reserved for actual sub-agent sessions.
    viewingSubAgentSession = isSubAgentSession === true;
    viewingSessionId =
      isOwnAgent && normalizedSessionId === ownCurrentSessionId
        ? ''
        : normalizedSessionId;
    reportSessionNavigation();
    await context.loadHistoryForSession(agentAddress, normalizedSessionId);
    context.layout.requestComposerFocus();
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
    agentAddress,
  } = {}) => {
    const removedId = String(deletedSessionId ?? '').trim();
    const landingId = String(nextSessionId ?? '').trim();
    if (!removedId) {
      return;
    }
    const agent = context.target.activeAgent;
    const viewedSessionId = viewingSessionId || agent?.current_session_id || '';
    if (viewedSessionId === removedId && landingId) {
      await handleSessionSelected(landingId, agentAddress);
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
      context.target.activeAgent?.current_session_id === viewingSessionId
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
    context.onSessionNavigation?.(
      viewingSessionId
        ? {
            agentId:
              viewingSessionAgentId || context.target.activeOwnAgentAddress(),
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
    if (context.target.projectAgentActive) {
      await context.target.ensureProjectAgentSession(
        resolveAgentAddressing(
          context.target.selectedProjectAgentId,
          context.selectedProjectId,
          true,
        ),
      );
      return;
    }
    await context.loadCurrentHistory();
  };

  const handleReturnToCurrentSession = async () => {
    if (!subAgentSessionActive || context.chatState.loadingHistory) {
      return;
    }

    // A sub-agent session returns to its PARENT session (from the child's
    // `subagent_parent` metadata). Without resolvable parent metadata (old
    // child sessions, deleted parent agent) the button falls back to the
    // return-to-current behavior below.
    if (subAgentSessionActive && subAgentParentTarget) {
      await navigateToParentSession(subAgentParentTarget);
      context.layout.requestComposerFocus();
      return;
    }

    clearSessionOverride();
    reportSessionNavigation();
    await loadActiveOwnHistory();
    context.layout.requestComposerFocus();
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
    const ownAddress = context.target.activeOwnAgentAddress();
    const ownCurrentSessionId = context.target.projectAgentActive
      ? (context.target.projectAgentSessions[ownAddress] ?? '')
      : (selectedAgent(context.chatState)?.current_session_id ?? '');
    viewingSubAgentSession = isSubAgentSession;
    if (agentAddress === ownAddress && sessionId === ownCurrentSessionId) {
      clearSessionOverride();
    } else {
      viewingSessionAgentId = agentAddress === ownAddress ? '' : agentAddress;
      viewingSessionId = sessionId;
    }
    reportSessionNavigation();
    await context.loadHistoryForSession(agentAddress, sessionId);
  };

  const handleNewSession = async () => {
    if (context.chatState.loadingHistory || creatingSession) {
      return;
    }
    // "New session" means "make the chat ready for a fresh conversation."
    // Repeating it on an already blank Session is therefore idempotent, while
    // a local draft still counts as work that deserves its own Session.
    context.layout.requestComposerFocus({ includeMobile: true });
    if (context.composerAvailable && context.displayedSessionIsEmpty()) {
      return;
    }
    if (context.target.projectAgentActive) {
      // Symmetric with the identity path below: a new session always leaves
      // any override view and becomes the displayed session.
      clearSessionOverride();
      reportSessionNavigation();
      if (await createProjectAgentSession()) {
        context.layout.requestComposerFocus({ includeMobile: true });
      }
      return;
    }
    const agent = selectedAgent(context.chatState);
    if (!agent) {
      return;
    }
    const sourceSessionState = context.target.activeSessionState;
    clearSessionOverride();
    creatingSession = true;
    context.actions.clearSessionActionError(sourceSessionState);
    try {
      const session = await context.chatController.createSession({
        agent_id: agent.id,
        make_current: true,
      });
      await switchToCurrentSession(agent.id, session.session_id);
      context.layout.requestComposerFocus({ includeMobile: true });
    } catch (error) {
      context.actions.setSessionActionError(
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
    const agentAddress = context.target.currentProjectAgentAddress();
    if (!agentAddress) {
      return false;
    }
    const sourceSessionState = context.target.activeSessionState;
    creatingSession = true;
    context.actions.clearSessionActionError(sourceSessionState);
    try {
      const created = await context.chatController.createSession({
        agent_id: agentAddress,
      });
      const sessionId = created?.session_id ?? '';
      if (
        !sessionId ||
        context.target.currentProjectAgentAddress() !== agentAddress
      ) {
        return false;
      }
      context.target.projectAgentSessions = {
        ...context.target.projectAgentSessions,
        [agentAddress]: sessionId,
      };
      await context.loadHistoryForSession(agentAddress, sessionId);
      return true;
    } catch (error) {
      context.actions.setSessionActionError(
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
    const updatedAgents = context.chatState.agents.map((candidate) =>
      candidate.id === agentId
        ? { ...candidate, current_session_id: normalizedSessionId }
        : candidate,
    );
    setAgents(context.chatState, updatedAgents);
    context.onAgentsChanged?.(updatedAgents);
    context.onAgentSelected?.(agentId);
    reportSessionNavigation();
    ensureSessionState(context.chatState, agentId, normalizedSessionId);
    await context.loadHistoryForSession(agentId, normalizedSessionId);
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
    context.target.selectedProjectAgentId = '';
    context.onProjectAgentSelected?.('');
    if (move.bareAgentId !== context.chatState.selectedAgentId) {
      selectAgent(context.chatState, move.bareAgentId);
      context.onAgentSelected?.(move.bareAgentId);
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
    context.target.projectAgentSessions = {
      ...context.target.projectAgentSessions,
      [agentAddress]: sessionId,
    };
    context.target.initialProjectRestoreDone = true;
    context.target.lastLoadedProjectId = projectId;
    context.target.selectedProjectAgentId = bareAgentId;
    context.onProjectSelected?.(projectId);
    context.onProjectAgentSelected?.(bareAgentId);
    await context.target.loadProjectTeamForMove(projectId);
    await context.target.ensureProjectAgentSession(
      resolveAgentAddressing(bareAgentId, projectId, true),
    );
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
    const agentId = context.target.qualifiedChildAgentAddress(target.agentId);
    context.navigateToSubAgent({
      ...target,
      agentId,
    });
  };

  // A reflection review lives in a same-Agent fork; opening it is ordinary
  // same-agent session navigation, not sub-agent navigation, so no child
  // banner appears and the fork's live Run streams like any other session.
  const handleOpenReflection = (row) => {
    const sessionId = String(row?.sessionId ?? '').trim();
    if (!sessionId) {
      return;
    }
    void handleSessionSelected(
      sessionId,
      context.target.activeSessionState?.agentId ?? '',
      false,
    );
  };
  return {
    get creatingSession() {
      return creatingSession;
    },
    get viewingSessionId() {
      return viewingSessionId;
    },
    get viewingSessionAgentId() {
      return viewingSessionAgentId;
    },
    get subAgentLinkFollowRequest() {
      return subAgentLinkFollowRequest;
    },
    get subAgentSessionActive() {
      return subAgentSessionActive;
    },
    get subAgentParentTarget() {
      return subAgentParentTarget;
    },
    get sessionParentLink() {
      return sessionParentLink;
    },
    get sessionOverrideActive() {
      return sessionOverrideActive;
    },
    get handleSelectAgent() {
      return handleSelectAgent;
    },
    get handleSessionSelected() {
      return handleSessionSelected;
    },
    get handleSessionDeleted() {
      return handleSessionDeleted;
    },
    get clearSessionOverride() {
      return clearSessionOverride;
    },
    get reportSessionNavigation() {
      return reportSessionNavigation;
    },
    get handleReturnToCurrentSession() {
      return handleReturnToCurrentSession;
    },
    get navigateToParentSession() {
      return navigateToParentSession;
    },
    get handleNewSession() {
      return handleNewSession;
    },
    get switchToCurrentSession() {
      return switchToCurrentSession;
    },
    get moveSessionToAgent() {
      return moveSessionToAgent;
    },
    get handleNavigateToSubAgentLink() {
      return handleNavigateToSubAgentLink;
    },
    get handleOpenReflection() {
      return handleOpenReflection;
    },
  };
}
