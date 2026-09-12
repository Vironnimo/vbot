import { listProjects, listAgents } from '$lib/api.js';

export function createAppSelection(context) {
  const SELECTED_AGENT_KEY = 'vbot.selectedAgentId';

  const SELECTED_PROJECT_KEY = 'vbot.selectedProjectId';

  const SELECTED_PROJECT_AGENT_KEY = 'vbot.selectedProjectAgentId';

  const MANAGED_PROJECT_KEY = 'vbot.managedProjectId';

  const readStoredSelectedAgentId = () => {
    try {
      if (typeof localStorage === 'undefined') {
        return '';
      }
      return localStorage.getItem(SELECTED_AGENT_KEY) || '';
    } catch {
      return '';
    }
  };

  // The persisted project selection follows the same localStorage pattern as
  // the selected agent (own key). Empty = "No project" / Personal.
  const readStoredSelectedProjectId = () => {
    try {
      if (typeof localStorage === 'undefined') {
        return '';
      }
      return localStorage.getItem(SELECTED_PROJECT_KEY) || '';
    } catch {
      return '';
    }
  };

  const readStoredManagedProjectId = () => {
    try {
      if (typeof localStorage === 'undefined') {
        return '';
      }
      return localStorage.getItem(MANAGED_PROJECT_KEY) || '';
    } catch {
      return '';
    }
  };

  // The remembered active agent inside the selected project, restored on reload
  // so the chat returns to the same agent instead of the project default (the
  // default jump is only for a genuine project switch). Three states, so it is
  // read as a tri-state (never collapsed to ''):
  //   - null  → nothing remembered yet → the initial load picks the default
  //   - ''    → an identity agent was active alongside the project → restore it
  //   - 'id'  → restore that team member
  const readStoredSelectedProjectAgentId = () => {
    try {
      if (typeof localStorage === 'undefined') {
        return null;
      }
      return localStorage.getItem(SELECTED_PROJECT_AGENT_KEY);
    } catch {
      return null;
    }
  };

  let agents = $state([]);

  let selectedAgentId = $state(readStoredSelectedAgentId());

  const initialSelectedProjectId = readStoredSelectedProjectId();

  // Project context for the two-bar chat. `projects` feeds the chat dropdown;
  // `selectedProjectId` is the chosen project (empty = Personal/identity path).
  let projects = $state([]);

  let projectsLoadRequestId = 0;

  let selectedProjectId = $state(initialSelectedProjectId);

  // Projects-tab selection is remembered independently so browsing project
  // settings does not silently change the Chat context. A selected Chat
  // project seeds and updates this mirror; otherwise the Projects view keeps
  // the user's last management selection.
  let managedProjectId = $state(
    initialSelectedProjectId || readStoredManagedProjectId(),
  );

  // The remembered active agent inside the selected project (tri-state: null =
  // nothing remembered, '' = identity agent active alongside the project, or a
  // bare team-member id). Persisted like the selected agent/project; ChatView
  // reports changes back through `onProjectAgentSelected`.
  let selectedProjectAgentId = $state(readStoredSelectedProjectAgentId());

  let agentsRefreshToken = $state(0);

  $effect(() => {
    try {
      if (selectedAgentId) {
        localStorage.setItem(SELECTED_AGENT_KEY, selectedAgentId);
      } else {
        localStorage.removeItem(SELECTED_AGENT_KEY);
      }
    } catch {
      // localStorage unavailable (private browsing, storage quota)
    }
  });

  $effect(() => {
    try {
      if (managedProjectId) {
        localStorage.setItem(MANAGED_PROJECT_KEY, managedProjectId);
      } else {
        localStorage.removeItem(MANAGED_PROJECT_KEY);
      }
    } catch {
      // localStorage unavailable (private browsing, storage quota)
    }
  });

  $effect(() => {
    try {
      if (selectedProjectId) {
        localStorage.setItem(SELECTED_PROJECT_KEY, selectedProjectId);
      } else {
        localStorage.removeItem(SELECTED_PROJECT_KEY);
      }
    } catch {
      // localStorage unavailable (private browsing, storage quota)
    }
  });

  $effect(() => {
    try {
      // Tri-state: null clears the key; '' (identity active) and a team-member
      // id are both stored verbatim so the restore can tell them apart.
      if (selectedProjectAgentId === null) {
        localStorage.removeItem(SELECTED_PROJECT_AGENT_KEY);
      } else {
        localStorage.setItem(
          SELECTED_PROJECT_AGENT_KEY,
          selectedProjectAgentId,
        );
      }
    } catch {
      // localStorage unavailable (private browsing, storage quota)
    }
  });

  // ChatView reflects the project dropdown choice back here so the persisted
  // mirror stays current.
  const selectProject = (projectId) => {
    selectedProjectId = typeof projectId === 'string' ? projectId : '';
    if (selectedProjectId) {
      managedProjectId = selectedProjectId;
    }
  };

  const selectManagedProject = (projectId) => {
    managedProjectId = typeof projectId === 'string' ? projectId : '';
  };

  // ChatView reports the active project agent (a team-member id, or '' for an
  // identity agent active alongside the project) so the persisted mirror can
  // restore it on reload. ChatView always reports a string; null stays internal
  // to App (a removed project, see loadProjects).
  const selectProjectAgent = (agentId) => {
    selectedProjectAgentId = typeof agentId === 'string' ? agentId : null;
  };

  const loadProjects = async () => {
    const requestId = projectsLoadRequestId + 1;
    projectsLoadRequestId = requestId;
    try {
      const result = await listProjects();
      if (requestId !== projectsLoadRequestId) {
        return false;
      }
      projects = Array.isArray(result?.projects) ? result.projects : [];
      // Drop a stale persisted selection if its project no longer exists. The
      // remembered project agent goes with it — it only means anything within a
      // live project.
      if (
        selectedProjectId &&
        !projects.some((project) => project.project_id === selectedProjectId)
      ) {
        selectedProjectId = '';
        selectedProjectAgentId = null;
      }
      if (
        managedProjectId &&
        !projects.some((project) => project.project_id === managedProjectId)
      ) {
        managedProjectId = '';
      }
      return true;
    } catch {
      // Keep the last valid catalog during a transient RPC failure. A newer
      // request also owns any visible state change, so stale failures are inert.
      return false;
    }
  };

  // The selection half of a history entry: which identity agent and which
  // project context were active when the entry was created. Restored together
  // with the session override so Back/Forward re-establish the whole chat
  // context (chips, project bar, and displayed session agree again).
  const currentNavigationSelection = () => ({
    agentId: selectedAgentId,
    projectId: selectedProjectId,
    projectAgentId: selectedProjectAgentId,
  });

  const syncAgents = (nextAgents = []) => {
    agents = Array.isArray(nextAgents) ? nextAgents : [];
    if (
      selectedAgentId &&
      !agents.some((agent) => agent.id === selectedAgentId)
    ) {
      selectedAgentId = agents[0]?.id ?? '';
      return;
    }
    if (!selectedAgentId && agents.length > 0) {
      selectedAgentId = agents[0].id;
    }
  };

  const selectAgent = (agentOrId) => {
    selectedAgentId =
      typeof agentOrId === 'string' ? agentOrId : (agentOrId?.id ?? '');
  };

  const remapIdentityAgentId = (oldAgentId, newAgentId) => {
    if (selectedAgentId === oldAgentId) {
      selectedAgentId = newAgentId;
    }
    if (context.promptScopeTarget === oldAgentId) {
      context.promptScopeTarget = newAgentId;
      context.promptScopeTargetRequestId += 1;
    }
  };

  const refreshAgents = (nextAgents = []) => {
    syncAgents(nextAgents);
    agentsRefreshToken += 1;
  };

  // Re-fetch the agent roster after a `resource_changed(kind:"agents")` signal
  // (the migrated agent-CRUD reload — the channel carries no agent data, so we
  // re-fetch agent.list). `refreshAgents` bumps `agentsRefreshToken`, so the
  // Agents and Chat surfaces reload exactly as they did for the old agent.*
  // events.
  const reloadAgentsFromServer = async () => {
    try {
      const result = await listAgents();
      refreshAgents(result.agents);
    } catch (error) {
      console.warn('Agent list refresh failed:', error);
    }
  };
  function destroy() {
    projectsLoadRequestId += 1;
  }

  return {
    destroy,
    get agents() {
      return agents;
    },
    get selectedAgentId() {
      return selectedAgentId;
    },
    get projects() {
      return projects;
    },
    get selectedProjectId() {
      return selectedProjectId;
    },
    get managedProjectId() {
      return managedProjectId;
    },
    get selectedProjectAgentId() {
      return selectedProjectAgentId;
    },
    get agentsRefreshToken() {
      return agentsRefreshToken;
    },
    get selectProject() {
      return selectProject;
    },
    get selectManagedProject() {
      return selectManagedProject;
    },
    get selectProjectAgent() {
      return selectProjectAgent;
    },
    get loadProjects() {
      return loadProjects;
    },
    get currentNavigationSelection() {
      return currentNavigationSelection;
    },
    get syncAgents() {
      return syncAgents;
    },
    get selectAgent() {
      return selectAgent;
    },
    get remapIdentityAgentId() {
      return remapIdentityAgentId;
    },
    get refreshAgents() {
      return refreshAgents;
    },
    get reloadAgentsFromServer() {
      return reloadAgentsFromServer;
    },
  };
}
