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
import { parseModelSelectionValue } from '$lib/modelSelection.js';
import { t } from '$lib/i18n.js';
import {
  projectTeam as normalizeProjectTeam,
  normalizeScanReport,
} from '../../../lib/projectsView.js';

export function createChatViewTarget(context) {
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

  // Whether the active agent is a project (config) team agent. When false the
  // chat is on an identity agent and every RPC payload is byte-identical to
  // today (the hard no-regression rule).
  let projectAgentActive = $derived(
    isProjectSelected(context.selectedProjectId) &&
      selectedProjectAgentId !== '',
  );

  // The chosen project's display name, used as the bold prefix on the team bar.
  let selectedProjectName = $derived(
    context.projects.find(
      (project) => project.project_id === context.selectedProjectId,
    )?.display_name ||
      context.selectedProjectId ||
      '',
  );

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
  // identity agent plus the selected project's team — the same addresses the
  // Chat agent bars offer.
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

  function agentActivityTooltip(activityLabel, modelValue) {
    const { model } = parseModelSelectionValue(
      typeof modelValue === 'string' ? modelValue.trim() : '',
    );
    return model ? `${activityLabel}\n${model}` : activityLabel;
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
      context.selectedProjectId,
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
    lastLoadedProjectId = projectId;
    if (!projectId) {
      clearProjectContext();
      if (!isInitialRestore) {
        context.layout.requestComposerFocus();
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
        context.selectedProjectId === projectId &&
        selectedProjectAgentId
      ) {
        context.layout.requestComposerFocus();
      }
    });
  });

  // Tear the second bar down: back to the identity-only chat (Personal).
  const clearProjectContext = () => {
    projectTeam = [];
    projectReport = null;
    projectScanError = '';
    selectedProjectAgentId = '';
    context.onProjectAgentSelected?.('');
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
      const result = await context.chatController.loadProject(projectId);
      // A newer selection may have superseded this one mid-flight.
      if (context.selectedProjectId !== projectId) {
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
      if (context.selectedProjectId !== projectId) {
        return;
      }
      projectTeam = [];
      projectReport = null;
      projectScanError = `${t('chat.project.loadError', 'The project team could not be loaded.')} ${error.message}`;
    } finally {
      if (context.selectedProjectId === projectId) {
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
    const hadOverride = context.navigation.sessionOverrideActive;
    if (!keepOverride) {
      context.navigation.clearSessionOverride();
    }
    selectedProjectAgentId = agentId;
    context.onProjectAgentSelected?.(agentId);
    if (!keepOverride && hadOverride) {
      // An override cleared by switching agents is an override change and
      // becomes a history entry, mirroring the identity chip path.
      context.navigation.reportSessionNavigation();
    }
    const addressing = resolveAgentAddressing(
      agentId,
      context.selectedProjectId,
      true,
    );
    await ensureProjectAgentSession(addressing);
  };

  // Choose (and if needed create) the local session for a project agent, then
  // load its history. `session.list`/`session.create`/`chat.history` all take
  // the FULL address (`agent@projekt`) — trap 2.
  const ensureProjectAgentSession = async (addressing) => {
    const { agentAddress } = addressing;
    context.actions.clearSessionActionError();
    try {
      const newestUnreadSession = newestUnreadSessionForAgent(
        context.chatState,
        agentAddress,
      );
      let sessionId =
        newestUnreadSession?.sessionId ??
        projectAgentSessions[agentAddress] ??
        '';
      if (!sessionId) {
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
        if (!sessionId) {
          const created = await context.chatController.createSession({
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
      await context.loadHistoryForSession(agentAddress, sessionId);
    } catch (error) {
      if (currentProjectAgentAddress() !== agentAddress) {
        return;
      }
      context.actions.setSessionActionError(
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
      context.selectedProjectId,
      true,
    ).agentAddress;
  }

  const handleSelectProject = (projectId) => {
    const next = isProjectSelected(projectId) ? projectId : '';
    if (
      next ===
      (isProjectSelected(context.selectedProjectId)
        ? context.selectedProjectId
        : '')
    ) {
      return;
    }
    context.onProjectSelected?.(next);
  };

  const handleSelectProjectAgent = async (agentId) => {
    if (!agentId) {
      return;
    }
    const agentAddress = formatAgentAddress(agentId, context.selectedProjectId);
    if (
      agentId === selectedProjectAgentId &&
      !newestUnreadSessionForAgent(context.chatState, agentAddress)
    ) {
      return;
    }
    await openProjectAgent(agentId);
    context.layout.requestComposerFocus();
  };

  // Load just the team + report for a move target (no agent auto-selection —
  // the move picks the agent itself). Errors surface as the scan error notice.
  const loadProjectTeamForMove = async (projectId) => {
    loadingProjectTeam = true;
    projectScanError = '';
    try {
      const result = await context.chatController.loadProject(projectId);
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
    get selectedProjectName() {
      return selectedProjectName;
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
    agentActivityTooltip,
    activeOwnAgentAddress,
    activeAddressing,
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
    get handleSelectProject() {
      return handleSelectProject;
    },
    get handleSelectProjectAgent() {
      return handleSelectProjectAgent;
    },
    get loadProjectTeamForMove() {
      return loadProjectTeamForMove;
    },
  };
}
