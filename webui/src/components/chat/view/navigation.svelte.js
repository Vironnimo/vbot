import { formatAgentAddress, parseAgentAddress } from '$lib/agentAddress.js';
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
import { tick, untrack } from 'svelte';
import { takeSessionInvalidations } from '$lib/sessionInvalidation.js';

export function createChatViewNavigation(context) {
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

  // Bumped when a Session invalidation names the displayed Session or its
  // resolved Parent Session (a title, provenance, or deletion change).
  let sessionParentRevision = $state(0);

  let sessionParentAddresses = [];

  let lastSessionInvalidationId = null;

  $effect(() => {
    const entries = context.sessionInvalidations;
    untrack(() => {
      const { targets, lastId, overflowed } = takeSessionInvalidations(
        entries,
        lastSessionInvalidationId,
      );
      lastSessionInvalidationId = lastId;
      if (overflowed || targets.some(namesSessionLineage)) {
        sessionParentRevision += 1;
      }
    });
  });

  function namesSessionLineage(target) {
    if (target.all) {
      return true;
    }
    if (target.renamedAgent) {
      return sessionParentAddresses.some(
        ({ agentAddress }) =>
          agentAddress === target.renamedAgent.oldAgentId ||
          agentAddress === target.renamedAgent.newAgentId,
      );
    }
    // Run completion and read acknowledgement leave titles and provenance
    // unchanged.
    if (target.runId || target.readRunId) {
      return false;
    }
    return sessionParentAddresses.some(
      ({ agentAddress, sessionId }) =>
        agentAddress === target.agentAddress && sessionId === target.sessionId,
    );
  }

  $effect(() => {
    const addressing = context.target.activeAddressing();
    const displayKey = context.target.displayedSessionKey();
    const fetchKey = `${displayKey}::${context.sessionsRefreshToken}::${sessionParentRevision}`;
    if (!displayKey || !addressing.agentAddress || !addressing.sessionId) {
      sessionParentFetchKey = '';
      sessionParentAddresses = [];
      subAgentParentTarget = null;
      sessionParentLink = null;
      return;
    }
    if (fetchKey === sessionParentFetchKey) {
      return;
    }
    sessionParentFetchKey = fetchKey;
    sessionParentAddresses = [
      {
        agentAddress: addressing.agentAddress,
        sessionId: addressing.sessionId,
      },
    ];
    subAgentParentTarget = null;
    sessionParentLink = null;
    untrack(() =>
      loadSessionParent(
        fetchKey,
        displayKey,
        addressing.agentAddress,
        addressing.sessionId,
      ),
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
    const superseded = () =>
      context.target.displayedSessionKey() !== displayKey ||
      sessionParentFetchKey !== fetchKey;
    try {
      const child = await context.chatController.getSession(
        childAddress,
        childSessionId,
      );
      // A newer navigation or Session invalidation may have superseded this
      // request mid-flight.
      if (superseded()) {
        return;
      }
      const parent = sessionParentReference(child?.session);
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
      sessionParentAddresses = [
        ...sessionParentAddresses,
        { agentAddress: parentAgentAddress, sessionId: parentSessionId },
      ];
      const target = {
        agentAddress: parentAgentAddress,
        sessionId: parentSessionId,
        isSubAgentSession: false,
      };
      if (parent.kind === 'subagent') {
        subAgentParentTarget = target;
      }

      let parentSession = null;
      try {
        const parentRead = await context.chatController.getSession(
          parentAgentAddress,
          parentSessionId,
        );
        if (superseded()) {
          return;
        }
        parentSession = parentRead?.session ?? null;
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
  // `navigateToSubAgent`, Back/Forward, deep links and the main navigation.
  // They arrive here and are never reported back as a new step.
  $effect(() => {
    const navigation = context.pendingSessionNavigation;
    const requestId = navigation?.requestId ?? '';
    const navigationKey = !navigation
      ? ''
      : navigation.returnToCurrent
        ? `::return::${requestId}`
        : navigation.draft === true && navigation.agentId
          ? `${navigation.agentId}::draft::${requestId}`
          : navigation.agentId && navigation.sessionId
            ? `${navigation.agentId}::${navigation.sessionId}::${navigation.subAgent === true}::${requestId}`
            : '';
    if (!navigationKey || navigationKey === handledSessionNavigationKey) {
      return;
    }

    handledSessionNavigationKey = navigationKey;
    applySessionNavigation(navigation);
  });

  // A Session deleted from the other Chat area of the workspace or reported
  // deleted by the server (another window, or this window's own deletion
  // echoed back): release this area's pointer to it too, without a history
  // entry or composer focus.
  let handledSiblingDeletionId = 0;
  $effect(() => {
    const deletion = context.siblingSessionDeletion;
    const requestId = deletion?.requestId ?? 0;
    if (!requestId || requestId === handledSiblingDeletionId) {
      return;
    }
    handledSiblingDeletionId = requestId;
    void handleSessionDeleted(deletion, { passive: true });
  });

  // `step: false` follows a selection made elsewhere (the shared Agent
  // selection) without a new history step.
  const handleSelectAgent = (
    agentId,
    { focusComposer = true, step = true } = {},
  ) => {
    const select = () => selectAgentSession(agentId, { focusComposer });
    return step ? asStep(select) : select();
  };

  const selectAgentSession = async (agentId, { focusComposer }) => {
    // Choosing an identity agent leaves any Project Agent and its Project.
    context.target.selectedProjectAgentId = '';
    context.onProjectAgentSelected?.('');
    context.target.leaveProject();
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
      await context.loadHistoryForSession(agentId, unreadSession.sessionId);
      if (focusComposer) {
        context.layout.requestComposerFocus();
      }
      return;
    }
    if (agentId === context.chatState.selectedAgentId) {
      if (sessionOverrideActive) {
        clearSessionOverride();
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
  // browser-history restore. Restores re-enter past overrides, a draft, or
  // return to the current session without creating new history entries.
  const applySessionNavigation = async (navigation) => {
    stepMarked = false;
    const isCurrent = () => context.pendingSessionNavigation === navigation;
    const selectionChanged = await applyNavigationSelection(
      navigation.selection,
      isCurrent,
    );
    if (!isCurrent()) return;

    // A draft entry shows its Agent's draft again; an entry whose Agent is no
    // longer the active one falls back to the current view.
    if (
      navigation.draft === true &&
      navigation.agentId === context.target.activeOwnAgentAddress()
    ) {
      showDraft(navigation.agentId);
      return;
    }

    if (navigation.returnToCurrent || navigation.draft === true) {
      const hadOverride = sessionOverrideActive;
      clearSessionOverride();
      // The start place names no Session; report the one shown even when it
      // did not change.
      reportShownSessionAgain();
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

    // An entry naming the active Agent's current Session shows it as current.
    setViewedSession(navigation.agentId, navigation.sessionId, false);
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
  const applyNavigationSelection = async (selection, isCurrent) => {
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
      // Same imperative ownership as the /agent move: the target pre-syncs
      // its guard, so its effect does not jump to the project default.
      changed = true;
      if (!projectId) {
        context.target.leaveProject();
      } else {
        context.target.selectedProjectAgentId = '';
        await context.target.openProjectTeam(projectId);
      }
    }
    if (!isCurrent()) return false;
    if (projectId && projectAgentId !== context.target.selectedProjectAgentId) {
      context.target.selectedProjectAgentId = projectAgentId;
      context.onProjectAgentSelected?.(projectAgentId);
      changed = true;
    }
    return changed;
  };

  const handleSessionSelected = (
    sessionId,
    sessionAgentAddress,
    isSubAgentSession,
  ) =>
    asStep(() =>
      showSession(sessionId, sessionAgentAddress, isSubAgentSession),
    );

  const showSession = async (
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

    setViewedSession(agentAddress, normalizedSessionId, isSubAgentSession);
    await context.loadHistoryForSession(agentAddress, normalizedSessionId);
    context.layout.requestComposerFocus();
  };

  // The drawer lists the displayed agent's sessions. Picking one of the
  // active agent's own sessions is a same-agent past-session view (or a
  // return to its current session); picking while a cross-agent override is
  // displayed keeps that agent's framing. The address form serves both
  // worlds — a project agent's sessions go through `agent@projekt`.
  const setViewedSession = (agentAddress, sessionId, isSubAgentSession) => {
    const isOwnAgent = agentAddress === context.target.activeOwnAgentAddress();
    const ownCurrentSessionId = isOwnAgent ? ownCurrentSession() : '';
    viewingSessionAgentId = isOwnAgent ? '' : agentAddress;
    // The sub-agent notice follows the picked row's real sub-agent flag,
    // not cross-agent-ness: a foreign agent's ordinary session is a normal
    // override view, while the banner (and its parent-return button) stay
    // reserved for actual sub-agent sessions.
    viewingSubAgentSession = isSubAgentSession === true;
    viewingSessionId =
      isOwnAgent && sessionId === ownCurrentSessionId ? '' : sessionId;
  };

  // A session was deleted from the drawer (or passively: from the other Chat
  // area, or reported by the server). The area first releases its own pointer
  // to it: this area keeps its Session selection across roster refreshes
  // (`preserveSessionSelection`), so an identity Agent's current pointer would
  // otherwise keep naming the archived Session, and a Project Agent's locally
  // held Session would reopen it on the next agent selection. The server
  // re-aimed the identity pointer to the landing it returned (#2:
  // most-recently-active remaining, else none: the Agent shows a draft). If
  // this area was viewing the deleted Session (current or override), it then
  // navigates to that landing; otherwise it stays put and lets the list
  // refresh.
  //
  // The server's deletion event and the drawer's delete response arrive in
  // either order. When the event came first, this area already followed
  // passively; the drawer's deletion then completes the deliberate part
  // (composer focus) instead of finding nothing to do. Either way the deleted
  // Session's history entry is corrected to its landing, never a new step.
  let passiveDeletionFollow = null;
  const handleSessionDeleted = async (
    { deletedSessionId, nextSessionId, agentAddress } = {},
    { passive = false } = {},
  ) => {
    const removedId = String(deletedSessionId ?? '').trim();
    const landingId = String(nextSessionId ?? '').trim();
    const ownerAddress =
      String(agentAddress ?? '').trim() ||
      context.target.activeAddressing().agentAddress;
    if (!removedId || !ownerAddress) {
      return;
    }
    const viewedSessionId =
      viewingSessionId || context.target.activeAgent?.current_session_id || '';
    releaseDeletedSession(ownerAddress, removedId, landingId);
    if (!passive) {
      context.onSessionDeleted?.({
        deletedSessionId: removedId,
        nextSessionId: landingId,
        agentAddress: ownerAddress,
      });
    }
    const followed = passiveDeletionFollow;
    if (
      !passive &&
      followed?.removedId === removedId &&
      followed.landingId === viewedSessionId
    ) {
      passiveDeletionFollow = null;
      await followed.loaded;
      context.layout.requestComposerFocus();
      return;
    }
    if (viewedSessionId !== removedId) {
      return;
    }
    if (passive) {
      // Another area's or window's action: follow it without stealing
      // composer focus.
      const loaded = followDeletionLanding(ownerAddress, landingId);
      passiveDeletionFollow = { removedId, landingId, loaded };
      await loaded;
      return;
    }
    passiveDeletionFollow = null;
    // The deleted Session's entry now names its landing.
    await followDeletionLanding(ownerAddress, landingId);
    context.layout.requestComposerFocus();
  };

  // Without a landing the owner has no Session left and shows a draft; a
  // deleted foreign override returns to the active Agent's own view.
  const followDeletionLanding = async (ownerAddress, landingId) => {
    if (landingId) {
      setViewedSession(ownerAddress, landingId, false);
      await context.loadHistoryForSession(ownerAddress, landingId);
      return;
    }
    clearSessionOverride();
    if (ownerAddress === context.target.activeOwnAgentAddress()) {
      showDraft(ownerAddress);
      return;
    }
    await loadActiveOwnHistory();
  };

  const releaseDeletedSession = (agentAddress, removedId, landingId) => {
    const projectSessions = context.target.projectAgentSessions;
    if (projectSessions[agentAddress] === removedId) {
      context.target.projectAgentSessions = {
        ...projectSessions,
        [agentAddress]: landingId,
      };
    }
    const { agentId, projectId } = parseAgentAddress(agentAddress);
    if (projectId) {
      return;
    }
    const agents = context.chatState.agents;
    if (
      agents.some(
        (agent) =>
          agent.id === agentId && agent.current_session_id === removedId,
      )
    ) {
      setAgents(
        context.chatState,
        agents.map((agent) =>
          agent.id === agentId
            ? { ...agent, current_session_id: landingId }
            : agent,
        ),
      );
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

  // The active Agent's own current Session: a Project Agent's locally held
  // one, else the selected identity Agent's current Session.
  const ownCurrentSession = () =>
    context.target.projectAgentActive
      ? (context.target.projectAgentSessions[
          context.target.activeOwnAgentAddress()
        ] ?? '')
      : (selectedAgent(context.chatState)?.current_session_id ?? '');

  // The Session this area shows (`{agentId, sessionId, subAgent}`, an empty
  // `sessionId` for a draft), null while it is not known yet.
  const shownSession = () => {
    const ownAddress = context.target.activeOwnAgentAddress();
    if (viewingSessionId) {
      return {
        agentId: viewingSessionAgentId || ownAddress,
        sessionId: viewingSessionId,
        subAgent: viewingSubAgentSession,
      };
    }
    const draft = context.target.activeDraft();
    if (draft) {
      return { agentId: draft.agentAddress, sessionId: '', subAgent: false };
    }
    const sessionId = ownCurrentSession();
    return ownAddress && sessionId
      ? { agentId: ownAddress, sessionId, subAgent: false }
      : null;
  };

  // Every change of the shown Session reaches App as this area's place. A
  // user action (`asStep`) makes its change a new history step; any other
  // change - a restore, a load resolving the Session, a deletion, a moved
  // Session, a draft's created Session - corrects the current entry.
  let stepMarked = false;
  let reportedSessionKey = '';
  let reportRevision = $state(0);
  const reportShownSessionAgain = () => {
    reportedSessionKey = '';
    reportRevision += 1;
  };
  $effect(() => {
    void reportRevision;
    const session = shownSession();
    untrack(() => {
      if (!session) return;
      const key = `${session.agentId}::${session.sessionId}::${session.subAgent}`;
      if (key === reportedSessionKey) return;
      reportedSessionKey = key;
      const replace = !stepMarked;
      stepMarked = false;
      context.onSessionNavigation?.(session, { replace });
    });
  });

  const asStep = async (action) => {
    stepMarked = true;
    try {
      return await action();
    } finally {
      await tick();
      stepMarked = false;
    }
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

  const handleReturnToCurrentSession = () => asStep(returnToCurrentSession);

  const returnToCurrentSession = async () => {
    if (!subAgentSessionActive || context.chatState.loadingHistory) {
      return;
    }

    // A sub-agent session returns to its PARENT session (from the child's
    // `subagent_parent` metadata). Without resolvable parent metadata (old
    // child sessions, deleted parent agent) the button falls back to the
    // return-to-current behavior below.
    if (subAgentSessionActive && subAgentParentTarget) {
      await showParentSession(subAgentParentTarget);
      context.layout.requestComposerFocus();
      return;
    }

    clearSessionOverride();
    await loadActiveOwnHistory();
    context.layout.requestComposerFocus();
  };

  // User-initiated navigation from a child session to its parent session: a
  // normal session navigation and a new history step — Back returns to the
  // child. A parent that is itself a Sub-Agent Session keeps the contextual
  // banner so another parent step remains available; the root parent returns
  // to ordinary Session presentation.
  const navigateToParentSession = (target) =>
    asStep(() => showParentSession(target));

  const showParentSession = async ({
    agentAddress,
    sessionId,
    isSubAgentSession = false,
  }) => {
    const ownAddress = context.target.activeOwnAgentAddress();
    const ownCurrentSessionId = ownCurrentSession();
    viewingSubAgentSession = isSubAgentSession;
    if (agentAddress === ownAddress && sessionId === ownCurrentSessionId) {
      clearSessionOverride();
    } else {
      viewingSessionAgentId = agentAddress === ownAddress ? '' : agentAddress;
      viewingSessionId = sessionId;
    }
    await context.loadHistoryForSession(agentAddress, sessionId);
  };

  const handleNewSession = () => asStep(startNewSession);

  // "New session" makes the chat ready for a fresh conversation: it shows the
  // active Agent's draft, and the first send creates the Session. Nothing is
  // requested from the server. A draft or a blank Session already is a fresh
  // conversation, so repeating it only focuses the composer, while a local
  // composer text still counts as work that keeps its own Session.
  const startNewSession = () => {
    const agentAddress = context.target.activeOwnAgentAddress();
    if (context.chatState.loadingHistory || !agentAddress) {
      return;
    }
    const fresh =
      context.target.activeDraft() ||
      (context.composerAvailable && context.displayedSessionIsEmpty());
    if (!fresh) {
      showDraft(agentAddress);
    }
    context.layout.requestComposerFocus({ includeMobile: true });
  };

  // Show the active Agent's draft in place of its current Session. An
  // Identity Agent's current Session in this area becomes empty (the server
  // pointer is untouched until the draft's first send); a Project Agent holds
  // an empty Session id.
  const showDraft = (agentAddress) => {
    clearSessionOverride();
    if (parseAgentAddress(agentAddress).projectId) {
      context.target.projectAgentSessions = {
        ...context.target.projectAgentSessions,
        [agentAddress]: '',
      };
      return;
    }
    setAgents(
      context.chatState,
      context.chatState.agents.map((agent) =>
        agent.id === agentAddress
          ? { ...agent, current_session_id: '' }
          : agent,
      ),
    );
  };

  // `/new` asked for a fresh conversation with the Agent of the Session it
  // was sent in: show that Agent's draft. An Identity Agent becomes the
  // selected one; another Project Agent's Session stays shown.
  const showAgentDraft = (agentAddress) => {
    const { projectId } = parseAgentAddress(agentAddress);
    if (!projectId) {
      moveToIdentityAgent({ bareAgentId: agentAddress });
    } else if (agentAddress !== context.target.activeOwnAgentAddress()) {
      return false;
    }
    showDraft(agentAddress);
    return true;
  };

  // A draft's first send created this Session: it takes the draft's place as
  // the Agent's current Session in this area. While the draft is shown, the
  // created Session replaces it, which also corrects the history entry. When
  // the area has moved on, a later return to the Agent opens the created
  // Session. A Session the area chose for the Agent since then stays.
  const adoptCreatedSession = (agentAddress, sessionId) => {
    if (parseAgentAddress(agentAddress).projectId) {
      if (context.target.projectAgentSessions[agentAddress] !== '') {
        return;
      }
      context.target.projectAgentSessions = {
        ...context.target.projectAgentSessions,
        [agentAddress]: sessionId,
      };
      return;
    }
    const agents = context.chatState.agents;
    if (
      !agents.some(
        (agent) => agent.id === agentAddress && !agent.current_session_id,
      )
    ) {
      return;
    }
    const updatedAgents = agents.map((agent) =>
      agent.id === agentAddress
        ? { ...agent, current_session_id: sessionId }
        : agent,
    );
    setAgents(context.chatState, updatedAgents);
    context.onAgentsChanged?.(updatedAgents);
  };

  const switchToCurrentSession = async (agentId, sessionId) => {
    const normalizedSessionId = String(sessionId ?? '').trim();
    if (!agentId || !normalizedSessionId) {
      return false;
    }

    clearSessionOverride();
    const updatedAgents = context.chatState.agents.map((candidate) =>
      candidate.id === agentId
        ? { ...candidate, current_session_id: normalizedSessionId }
        : candidate,
    );
    setAgents(context.chatState, updatedAgents);
    context.onAgentsChanged?.(updatedAgents);
    ensureSessionState(context.chatState, agentId, normalizedSessionId);
    context.onAgentSelected?.(agentId);
    const destination = context.actions.captureDisplayedSession();
    await context.loadHistoryForSession(agentId, normalizedSessionId);
    return context.actions.isDisplayedSessionCurrent(destination);
  };

  // `/agent <addr>` move: relocate the CURRENT session (same session id) to the
  // target agent's home and open it there. Unlike `/handoff` (which summarizes
  // into a NEW session), the session id is unchanged — the move happened on the
  // backend already; the accessor just opens it under the target. The target's
  // outside address decides the world (the one signal: presence of `@`), so the
  // same handler crosses every direction (identity↔project, both ways). It
  // reuses the picker's paths: an identity target goes through the bare-id
  // current-session path, a project target through the Project Agent path.
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

  // Identity target: leave any Project Agent and its Project, then select the
  // target identity agent. The session switch itself is
  // `switchToCurrentSession` (caller).
  const moveToIdentityAgent = (move) => {
    context.target.selectedProjectAgentId = '';
    context.onProjectAgentSelected?.('');
    context.target.leaveProject();
    if (move.bareAgentId !== context.chatState.selectedAgentId) {
      selectAgent(context.chatState, move.bareAgentId);
      context.onAgentSelected?.(move.bareAgentId);
    }
  };

  // Project target: open the SAME session under the project team agent. The
  // project context is set locally (so the picker reflects it immediately,
  // crossing the agent/project boundary) and reported up so App persists it for
  // the next reload — mirroring `openProjectAgent`, but the session is the moved
  // one, pre-seeded into `projectAgentSessions` so `ensureProjectAgentSession`
  // reuses it instead of picking one.
  //
  // The move owns the transition imperatively: `openProjectTeam` pre-syncs the
  // target's guard, so its `selectedProjectId`-watching effect never re-runs
  // `loadProjectTeam` with the project default (which would clobber the moved
  // agent/session).
  const moveToProjectAgent = async (move) => {
    clearSessionOverride();
    const { projectId, bareAgentId, agentAddress, sessionId } = move;
    context.target.projectAgentSessions = {
      ...context.target.projectAgentSessions,
      [agentAddress]: sessionId,
    };
    context.target.selectedProjectAgentId = bareAgentId;
    context.onProjectAgentSelected?.(bareAgentId);
    await context.target.openProjectTeam(projectId);
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
    get asStep() {
      return asStep;
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
    get showAgentDraft() {
      return showAgentDraft;
    },
    get adoptCreatedSession() {
      return adoptCreatedSession;
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
