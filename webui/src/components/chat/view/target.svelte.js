import {
  isProjectSelected,
  agentActivityStatus,
  resolveAgentAddressing,
  selectedAgent,
  currentSessionState,
  newestUnreadSessionForAgent,
  pickProjectAgentSessionId,
} from '../../../lib/chatState.js';
import { formatAgentAddress, parseAgentAddress } from '$lib/agentAddress.js';
import { t } from '$lib/i18n.js';
import { LIBRARIAN_AGENT_ID, librarianName } from '$lib/librarian.js';
import { isLiveAgentId, liveAgentName } from '$lib/liveAgents.js';
import {
  projectTeam as normalizeProjectTeam,
  normalizeScanReport,
} from '../../../lib/projectsView.js';

export function createChatViewTarget(context) {
  // --- Project Agent state -------------------------------------------------
  //
  // The Agent picker lists every Project's Team from the Project list's
  // cached scans. Choosing a Project Agent selects its Project and makes the
  // Agent active; the selected Project's Team and report are then re-read
  // live through `project.show` and shown in place of the listed ones. A
  // project (config) agent has NO server `current_session_id` (RPC-contract
  // trap 1), so its session is chosen locally and held in
  // `projectAgentSessions`, keyed by the agent's full address
  // (`agent@projekt`).
  let projectTeam = $state([]);

  // The Project `projectTeam` and `projectReport` belong to, '' for none.
  let teamProjectId = $state('');

  // The cached scan present when this Team was opened or last refreshed.
  // A later Project-list scan supersedes the older live projection.
  let listedProjectScan = $state.raw(null);

  let projectReport = $state(null);

  let projectScanError = $state('');

  let loadingProjectTeam = $state(false);

  // The active project agent's bare id, '' when chatting an identity agent.
  let selectedProjectAgentId = $state('');

  // address (`agent@projekt`) -> locally chosen session id (trap 1). An empty
  // id holds a draft; an absent address is not resolved yet.
  let projectAgentSessions = $state({});

  // Guards repeated project-show side effects for the same chosen project.
  let lastLoadedProjectId = '';
  let projectTeamLoadVersion = 0;

  // Set true after the first project effect run. That first run is the reload
  // restore — it honors the remembered project agent (`sharedSelectedProjectAgentId`);
  // every later run is a user-initiated dropdown switch that jumps to the
  // project default.
  let initialProjectRestoreDone = false;

  // Whether the active agent is a project (config) team agent. When false the
  // chat is on an identity agent and every RPC payload is byte-identical to
  // today (the hard no-regression rule).
  let projectAgentActive = $derived(
    isProjectSelected(context.selectedProjectId) &&
      selectedProjectAgentId !== '',
  );

  // Every Project with a Team, as the Agent picker groups it: the selected
  // Project's live Team once read, the others' cached Teams from the list.
  let projectGroups = $derived.by(() =>
    context.projects
      .map((project) => {
        const projectId = project.project_id;
        const live = projectId === teamProjectId;
        const team = live ? projectTeam : normalizeProjectTeam(project.scan);
        const report = live
          ? projectReport
          : normalizeScanReport(project.scan?.report);
        return {
          projectId,
          name: project.display_name || projectId,
          warning: report?.clean === false,
          members: team.map((member) => ({
            agent_id: member.agent_id,
            display_name: member.display_name,
            model: member.effective?.model?.value,
            thinkingEffort: member.effective?.thinking_effort?.value,
          })),
        };
      })
      .filter((group) => group.members.length > 0),
  );

  $effect(() => {
    const projectId = teamProjectId;
    if (!projectId || projectId !== lastLoadedProjectId) return;
    const scan = context.projects.find(
      (project) => project.project_id === projectId,
    )?.scan;
    if (!scan || scan === listedProjectScan) return;

    listedProjectScan = scan;
    projectTeamLoadVersion += 1;
    projectTeam = normalizeProjectTeam(scan);
    projectReport = normalizeScanReport(scan.report);
    projectScanError = '';
    loadingProjectTeam = false;
  });

  let activeAgent = $derived(getActiveAgent());

  let activeSessionState = $derived(getActiveSessionState());

  let projectAgentStatuses = $derived.by(() =>
    Object.fromEntries(
      projectTeam.map((member) => {
        const address = formatAgentAddress(
          member.agent_id,
          context.selectedProjectId,
        );
        return [
          member.agent_id,
          agentActivityStatus(
            context.chatState,
            address,
            displayedSessionKey(),
          ),
        ];
      }),
    ),
  );

  // Outside address of the displayed agent (bare id for identity, full
  // `agent@projekt` otherwise) — what address-parsing RPCs like session.list
  // need (trap 2). The session drawer lists sessions through it.
  let activeAgentAddress = $derived(activeAddressing().agentAddress);

  // Roster the session drawer's All-agents filter lists sessions for: every
  // identity agent plus the selected project's team. The other Projects'
  // Teams stay out, so one bounded request still covers the roster.
  let sessionDrawerAgents = $derived.by(() => {
    const roster = context.chatState.agents.map((agent) => ({
      address: agent.id,
      name: agent.name || agent.id,
    }));
    if (isProjectSelected(context.selectedProjectId)) {
      for (const member of projectTeam) {
        roster.push({
          address: formatAgentAddress(
            member.agent_id,
            context.selectedProjectId,
          ),
          name: member.display_name || member.agent_id,
        });
      }
    }
    return roster;
  });

  // Agent-bar selection follows the owner of the displayed Session, while the
  // underlying selected Agent remains the return target for an override. This
  // keeps nested Sub-Agent navigation truthful without losing the root context.
  let displayedIdentityAgentId = $derived.by(() => {
    const addressing = activeAddressing();
    return addressing.projectId ? '' : addressing.bareAgentId;
  });

  let displayedProjectAgentId = $derived.by(() => {
    const addressing = activeAddressing();
    return addressing.projectId === context.selectedProjectId
      ? addressing.bareAgentId
      : '';
  });

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
    return context.chatState.selectedAgentId;
  }

  // The outside address of the agent that owns the overridden (viewed)
  // session. `viewingSessionAgentId` stores the explicit owner (a bare
  // identity id or a full `agent@projekt` address); '' means "the active
  // agent's own past session".
  function overrideAgentAddress() {
    return context.navigation.viewingSessionAgentId || activeOwnAgentAddress();
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
    if (context.navigation.viewingSessionId) {
      const agentAddress = overrideAgentAddress();
      const { agentId, projectId } = parseAgentAddress(agentAddress);
      return {
        bareAgentId: agentId,
        projectId: projectId || null,
        agentAddress,
        isProjectAgent: Boolean(projectId),
        sessionId: context.navigation.viewingSessionId,
      };
    }
    if (projectAgentActive) {
      const addressing = resolveAgentAddressing(
        selectedProjectAgentId,
        context.selectedProjectId,
        true,
      );
      return {
        ...addressing,
        isProjectAgent: true,
        sessionId: projectAgentSessions[addressing.agentAddress] ?? '',
      };
    }
    const agent = selectedAgent(context.chatState);
    const bareAgentId = agent?.id ?? '';
    return {
      bareAgentId,
      projectId: null,
      agentAddress: bareAgentId,
      isProjectAgent: false,
      sessionId: agent?.current_session_id || '',
    };
  }

  // The draft the active Agent shows instead of a Session, or null. A draft
  // is an Agent's next conversation before it exists: an Identity Agent
  // without a current Session in this area, or a Project Agent holding no
  // Session. Its first send creates the Session. The key names it like a
  // Session key, per Chat area, so each area keeps its own composer draft;
  // `~` never starts a Session id.
  function activeDraft() {
    if (context.navigation.viewingSessionId) {
      return null;
    }
    const agentAddress = projectAgentActive
      ? currentProjectAgentAddress()
      : (selectedAgent(context.chatState)?.id ?? '');
    const sessionId = projectAgentActive
      ? projectAgentSessions[agentAddress]
      : selectedAgent(context.chatState)?.current_session_id || '';
    return agentAddress && sessionId === ''
      ? { agentAddress, key: `${agentAddress}::~draft-${context.draftScope}` }
      : null;
  }

  function getActiveAgent() {
    if (context.navigation.viewingSessionId) {
      if (!context.navigation.viewingSessionAgentId) {
        // The active agent's own past session.
        return projectAgentActive
          ? projectAgentAsAgent(activeProjectMember())
          : selectedAgent(context.chatState);
      }
      return (
        agentById(context.navigation.viewingSessionAgentId) ??
        overrideAgentDisplayStandIn(context.navigation.viewingSessionAgentId)
      );
    }
    if (projectAgentActive) {
      return projectAgentAsAgent(activeProjectMember());
    }
    return selectedAgent(context.chatState);
  }

  // Minimal agent-like object for an overridden session whose owner is not an
  // identity-roster agent — a project team agent's session (or a project
  // child), the hidden Librarian's or a Live Agent's session, or an identity
  // agent deleted while its session is still viewed. Keeps the chat surface
  // (header, banner, return button) alive instead of dead-ending on "choose
  // an agent". The bare id stays in `id` so queue and cancel-tool payloads
  // keep the bare spelling (trap 2). `__liveCall` marks a Session of a Live
  // voice call, which only the call writes to.
  function overrideAgentDisplayStandIn(agentAddress) {
    const { agentId, projectId } = parseAgentAddress(agentAddress);
    const liveCall = !projectId && isLiveAgentId(agentId);
    return {
      id: agentId,
      name: liveCall
        ? liveAgentName(agentId)
        : agentId === LIBRARIAN_AGENT_ID && !projectId
          ? librarianName()
          : agentId || agentAddress,
      current_session_id: '',
      __overrideAddress: agentAddress,
      __liveCall: liveCall,
    };
  }

  // Shape a projected team member into the minimal agent-like object the chat
  // surface renders (header name). The local
  // session id stands in for `current_session_id` so the existing session
  // machinery reads it without a special case.
  function projectAgentAsAgent(member) {
    if (!member) {
      return null;
    }
    const addressing = resolveAgentAddressing(
      member.agent_id,
      context.selectedProjectId,
      true,
    );
    return {
      id: member.agent_id,
      name: member.display_name || member.agent_id,
      current_session_id: projectAgentSessions[addressing.agentAddress] ?? '',
      __projectAddress: addressing.agentAddress,
    };
  }

  function agentById(agentId) {
    return (
      context.chatState.agents.find((agent) => agent.id === agentId) ?? null
    );
  }

  function getActiveSessionState() {
    if (context.navigation.viewingSessionId) {
      const agentAddress = overrideAgentAddress();
      return agentAddress
        ? (context.chatState.sessions[
            `${agentAddress}::${context.navigation.viewingSessionId}`
          ] ?? null)
        : null;
    }
    if (projectAgentActive) {
      const { agentAddress, sessionId } = activeAddressing();
      if (!agentAddress || !sessionId) {
        return null;
      }
      return (
        context.chatState.sessions[`${agentAddress}::${sessionId}`] ?? null
      );
    }
    return currentSessionState(context.chatState);
  }

  function displayedSessionKey() {
    if (context.navigation.viewingSessionId) {
      const agentAddress = overrideAgentAddress();
      return agentAddress
        ? `${agentAddress}::${context.navigation.viewingSessionId}`
        : '';
    }
    const draft = activeDraft();
    if (draft) {
      return draft.key;
    }
    if (projectAgentActive) {
      const { agentAddress, sessionId } = activeAddressing();
      return agentAddress && sessionId ? `${agentAddress}::${sessionId}` : '';
    }
    const agent = selectedAgent(context.chatState);
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

  // React to a selected Project the Chat did not choose itself: the reload
  // restore and a Project removed meanwhile. The picker, Session moves and
  // history restores switch Projects imperatively and pre-sync
  // `lastLoadedProjectId`, so this runs once per outside change.
  //
  // The first run after mount is the reload restore: it honors the remembered
  // project agent (a team-member id, or null = nothing remembered → default).
  // A remembered '' (an identity agent was active) leaves the Project: only a
  // Project Agent selects one. Every later run jumps to the project default —
  // `restoreAgentId === null` signals that.
  $effect(() => {
    const projectId = isProjectSelected(context.selectedProjectId)
      ? context.selectedProjectId
      : '';
    if (projectId === lastLoadedProjectId) {
      return;
    }
    const isInitialRestore = !initialProjectRestoreDone;
    const restoreAgentId = isInitialRestore
      ? (context.sharedSelectedProjectAgentId ?? null)
      : null;
    initialProjectRestoreDone = true;
    if (projectId && restoreAgentId === '') {
      lastLoadedProjectId = '';
      context.onProjectSelected?.('');
      return;
    }
    lastLoadedProjectId = projectId;
    // A later project switch is a user choice: the Session it lands on is a
    // new history step.
    const switchProject = (action) =>
      isInitialRestore ? action() : context.navigation.asStep(action);
    if (!projectId) {
      void switchProject(async () => clearProjectContext());
      if (!isInitialRestore) {
        context.layout.requestComposerFocus();
      }
      return;
    }
    // The initial (reload) restore must not clear a session override that a
    // mount-adopted history entry has just applied — the override stays the
    // displayed session, the member session loads invisibly behind it.
    void switchProject(async () => {
      const focusAfterLoad = context.layout.captureComposerFocus();
      await loadProjectTeam(projectId, {
        restoreAgentId,
        keepOverride: isInitialRestore,
      });
      if (
        !isInitialRestore &&
        context.selectedProjectId === projectId &&
        selectedProjectAgentId
      ) {
        focusAfterLoad();
      }
    });
  });

  // Drop the Project context: back to the identity-only chat.
  const clearProjectContext = () => {
    projectTeamLoadVersion += 1;
    projectTeam = [];
    teamProjectId = '';
    listedProjectScan = null;
    projectReport = null;
    projectScanError = '';
    selectedProjectAgentId = '';
    context.onProjectAgentSelected?.('');
    loadingProjectTeam = false;
  };

  // Load a project's scan team and report (banner) via `project.show` (live
  // re-scan), then choose the active agent. An empty team is valid, not an
  // error. The report is kept for the banner, shown only when the scan was
  // not clean.
  //
  // `restoreAgentId` decides who becomes active:
  //   - `null` — a genuine project switch: jump to the default agent (else the
  //     first team member).
  //   - a team-member id — a reload restore: reopen that member if it is still
  //     on the team, otherwise fall through to the default.
  const loadProjectTeam = async (
    projectId,
    { restoreAgentId = null, keepOverride = false } = {},
  ) => {
    listedProjectScan =
      context.projects.find((project) => project.project_id === projectId)
        ?.scan ?? null;
    const requestVersion = ++projectTeamLoadVersion;
    const isCurrent = () =>
      requestVersion === projectTeamLoadVersion &&
      context.selectedProjectId === projectId;
    loadingProjectTeam = true;
    projectScanError = '';
    selectedProjectAgentId = '';
    try {
      const result = await context.chatController.loadProject(projectId);
      // A newer selection may have superseded this one mid-flight.
      if (!isCurrent()) {
        return;
      }
      projectTeam = normalizeProjectTeam(result?.scan);
      teamProjectId = projectId;
      projectReport = normalizeScanReport(result?.scan?.report);
      if (restoreAgentId) {
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
      if (!isCurrent()) {
        return;
      }
      projectTeam = [];
      teamProjectId = projectId;
      projectReport = null;
      projectScanError = `${t('chat.project.loadError')} ${error.message}`;
    } finally {
      if (isCurrent()) {
        loadingProjectTeam = false;
      }
    }
  };

  function defaultProjectAgentId(project) {
    const value = project?.default_agent;
    return typeof value === 'string' ? value.trim() : '';
  }

  // Switch the chat to a project team agent. Clears any identity-side session
  // override and resolves the project agent's session locally (trap 1): on an
  // explicit picker choice (`preferUnread`) its newest unread session first,
  // else the already held one (or draft), else the most recent from
  // `session.list`, else a draft. The choice is held in `projectAgentSessions`
  // keyed by the agent's full address.
  const openProjectAgent = async (
    agentId,
    {
      keepOverride = false,
      preferUnread = false,
      projectId = context.selectedProjectId,
    } = {},
  ) => {
    if (!keepOverride) {
      context.navigation.clearSessionOverride();
    }
    selectedProjectAgentId = agentId;
    context.onProjectAgentSelected?.(agentId);
    const addressing = resolveAgentAddressing(agentId, projectId, true);
    await ensureProjectAgentSession(addressing, { preferUnread });
  };

  // Choose the local session for a project agent, then load its history. A
  // project agent without any Session shows a draft; its first send creates
  // the Session. `session.list`/`chat.history` take the FULL address
  // (`agent@projekt`) — trap 2. The chosen session is held in
  // `projectAgentSessions` before its history loads: a project agent displays
  // only its held session, so loading any other one (e.g. its newest unread
  // session) would fetch history the chat never shows and never marks read.
  // Only an explicit selection prefers the newest unread session; history
  // restores and Session moves keep the held (or pre-seeded) session.
  const ensureProjectAgentSession = async (
    addressing,
    { preferUnread = false } = {},
  ) => {
    const { agentAddress } = addressing;
    context.actions.clearSessionActionError();
    try {
      const newestUnreadSession = preferUnread
        ? newestUnreadSessionForAgent(context.chatState, agentAddress)
        : null;
      let sessionId =
        newestUnreadSession?.sessionId ?? projectAgentSessions[agentAddress];
      if (sessionId === undefined) {
        const listed = await context.chatController.listSessions(agentAddress, {
          limit: 1,
          includeSubagents: false,
          includeMemoryReflections: false,
          includeSkillReflections: false,
          includeCron: false,
        });
        // A newer project/agent selection may have superseded this one.
        if (currentProjectAgentAddress() !== agentAddress) {
          return;
        }
        sessionId = pickProjectAgentSessionId(listed?.sessions);
      }
      if (projectAgentSessions[agentAddress] !== sessionId) {
        projectAgentSessions = {
          ...projectAgentSessions,
          [agentAddress]: sessionId,
        };
      }
      if (!sessionId) {
        return;
      }
      await context.loadHistoryForSession(agentAddress, sessionId);
    } catch (error) {
      if (currentProjectAgentAddress() !== agentAddress) {
        return;
      }
      context.actions.setSessionActionError(
        `${t('chat.project.sessionError')} ${error.message}`,
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
      context.selectedProjectId,
      true,
    ).agentAddress;
  }

  // Choosing an identity agent leaves the Project: only a Project Agent
  // selects one. Pre-syncs the guard, so the effect above does not react.
  const leaveProject = () => {
    initialProjectRestoreDone = true;
    if (!lastLoadedProjectId && !isProjectSelected(context.selectedProjectId)) {
      return;
    }
    lastLoadedProjectId = '';
    clearProjectContext();
    context.onProjectSelected?.('');
  };

  // A picker choice of a Project Agent, in any Project. Another Project is
  // selected imperatively (like a Session move): its listed Team shows the
  // Agent at once while `project.show` re-reads Team and report.
  const handleSelectProjectAgent = async (agentId, projectId) => {
    if (!agentId || !isProjectSelected(projectId)) {
      return;
    }
    const agentAddress = formatAgentAddress(agentId, projectId);
    const projectChanges = projectId !== lastLoadedProjectId;
    if (
      !projectChanges &&
      agentId === selectedProjectAgentId &&
      !context.navigation.viewingSessionId &&
      !newestUnreadSessionForAgent(context.chatState, agentAddress)
    ) {
      return;
    }
    await context.navigation.asStep(async () => {
      const focusAfterLoad = context.layout.captureComposerFocus();
      if (!projectChanges) {
        await openProjectAgent(agentId, { preferUnread: true });
        focusAfterLoad();
        return;
      }
      context.navigation.clearSessionOverride();
      const reading = openProjectTeam(projectId);
      await openProjectAgent(agentId, {
        keepOverride: true,
        preferUnread: true,
        projectId,
      });
      await reading;
      focusAfterLoad();
    });
  };

  // Re-read the open Project's team in place, e.g. after `/model` changed a
  // member's Model: no loading state, no Agent selection, and a failed read
  // keeps the team shown.
  const refreshProjectTeam = async () => {
    const projectId = lastLoadedProjectId;
    if (!projectId) {
      return;
    }
    const requestVersion = projectTeamLoadVersion;
    try {
      const result = await context.chatController.loadProject(projectId);
      if (
        requestVersion === projectTeamLoadVersion &&
        lastLoadedProjectId === projectId
      ) {
        projectTeam = normalizeProjectTeam(result?.scan);
      }
    } catch {
      // Best effort: the team keeps the values it showed.
    }
  };

  // Select a Project whose Agent the caller opens itself (picker, Session
  // move, history restore): no agent auto-selection. The listed Team and
  // report stand in at once; `project.show` then re-reads both. A failed read
  // keeps the listed ones and surfaces the scan error notice.
  const openProjectTeam = async (projectId) => {
    initialProjectRestoreDone = true;
    lastLoadedProjectId = projectId;
    const listed = context.projects.find(
      (project) => project.project_id === projectId,
    );
    listedProjectScan = listed?.scan ?? null;
    const requestVersion = ++projectTeamLoadVersion;
    const isCurrent = () =>
      requestVersion === projectTeamLoadVersion &&
      lastLoadedProjectId === projectId;
    projectTeam = normalizeProjectTeam(listed?.scan);
    teamProjectId = projectId;
    projectReport = normalizeScanReport(listed?.scan?.report);
    loadingProjectTeam = true;
    projectScanError = '';
    context.onProjectSelected?.(projectId);
    try {
      const result = await context.chatController.loadProject(projectId);
      if (!isCurrent()) return;
      projectTeam = normalizeProjectTeam(result?.scan);
      projectReport = normalizeScanReport(result?.scan?.report);
    } catch (error) {
      if (!isCurrent()) return;
      projectScanError = `${t('chat.project.loadError')} ${error.message}`;
    } finally {
      if (isCurrent()) loadingProjectTeam = false;
    }
  };
  return {
    get projectTeam() {
      return projectTeam;
    },
    get projectReport() {
      return projectReport;
    },
    get projectScanError() {
      return projectScanError;
    },
    get loadingProjectTeam() {
      return loadingProjectTeam;
    },
    get selectedProjectAgentId() {
      return selectedProjectAgentId;
    },
    set selectedProjectAgentId(value) {
      selectedProjectAgentId = value;
    },
    get projectAgentSessions() {
      return projectAgentSessions;
    },
    set projectAgentSessions(value) {
      projectAgentSessions = value;
    },
    get lastLoadedProjectId() {
      return lastLoadedProjectId;
    },
    set lastLoadedProjectId(value) {
      lastLoadedProjectId = value;
    },
    get initialProjectRestoreDone() {
      return initialProjectRestoreDone;
    },
    set initialProjectRestoreDone(value) {
      initialProjectRestoreDone = value;
    },
    get projectAgentActive() {
      return projectAgentActive;
    },
    get projectGroups() {
      return projectGroups;
    },
    get activeAgent() {
      return activeAgent;
    },
    get activeSessionState() {
      return activeSessionState;
    },
    get projectAgentStatuses() {
      return projectAgentStatuses;
    },
    get activeAgentAddress() {
      return activeAgentAddress;
    },
    get sessionDrawerAgents() {
      return sessionDrawerAgents;
    },
    get displayedIdentityAgentId() {
      return displayedIdentityAgentId;
    },
    get displayedProjectAgentId() {
      return displayedProjectAgentId;
    },
    activeOwnAgentAddress,
    activeAddressing,
    activeDraft,
    agentById,
    displayedSessionKey,
    displayedSessionProjectId,
    qualifiedChildAgentAddress,
    isDisplayedSession,
    get clearProjectContext() {
      return clearProjectContext;
    },
    get ensureProjectAgentSession() {
      return ensureProjectAgentSession;
    },
    currentProjectAgentAddress,
    get handleSelectProjectAgent() {
      return handleSelectProjectAgent;
    },
    get openProjectTeam() {
      return openProjectTeam;
    },
    leaveProject,
    refreshProjectTeam,
  };
}
