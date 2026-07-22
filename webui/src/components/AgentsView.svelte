<script>
  import { onMount } from 'svelte';

  import {
    getSettings,
    listAgents,
    listConnections,
    listModels,
    listProjects,
    listSkills,
    listTools,
    reorderAgents,
    showProject,
  } from '$lib/api.js';
  import { buildAgentTargetCatalog } from '$lib/agentForm.js';
  import {
    projectIdsFromList,
    projectTeamEntry,
  } from '$lib/agentTargetOptions.js';
  import { t } from '$lib/i18n.js';
  import {
    SURFACE_FORM,
    shouldApplyReloadNow,
  } from '$lib/resourceInvalidation.js';

  import AgentCreateModal from './agents/AgentCreateModal.svelte';
  import AgentEditor from './agents/AgentEditor.svelte';
  import AgentListPane from './agents/AgentListPane.svelte';

  const noop = () => {};

  let {
    sharedSelectedAgentId = '',
    onAgentsChanged,
    onAgentSelected,
    onToast = noop,
    onNavigateToSettingsPanel = noop,
    onNavigateToAgentPrompt = noop,
    modelsRefreshToken = 0,
    projectsRefreshToken = 0,
    agentsRefreshToken = 0,
  } = $props();

  let agents = $state([]);
  let selectedAgentId = $state('');
  let agentOrderRevision = $state(0);
  let isReordering = $state(false);
  let reorderInteractionActive = $state(false);
  let pendingAgentReload = false;
  let lastSharedSelectedAgentId = $state('');
  let isCreateModalOpen = $state(false);
  let isLoading = $state(false);
  let loadError = $state('');
  let availableModels = $state([]);
  let availableConnections = $state([]);
  let availableTools = $state([]);
  let availableSkills = $state([]);
  let invalidSkills = $state([]);
  let availableProjects = $state([]);
  let projectTargetProjects = $state([]);
  let projectCatalogError = $state('');
  let agentTargetCatalogError = $state('');
  // The global agent defaults, fetched once when the create modal opens so it can
  // label its inherit options from the live global default (an agent's
  // "effective" does not exist yet at create time). Empty object on failure.
  let createModalAgentDefaults = $state({});
  // A live model reload fetches in the background but holds the visible option
  // swap while a model picker in the editor is open, so an open selection is
  // never disturbed (the chosen value lives in the editor's form state).
  let modelDropdownOpenCount = $state(0);
  let pendingModelCatalogs = null;
  let lastModelsRefreshToken = null;
  let lastProjectsRefreshToken = null;
  let lastAgentsRefreshToken = null;
  let agentListRequestId = 0;
  let loadingAgentListRequestId = 0;
  let projectCatalogRequestId = 0;

  let selectedAgent = $derived(
    agents.find((agent) => agent.id === selectedAgentId) ?? null,
  );
  let availableAgentTargets = $derived(
    buildAgentTargetCatalog({
      identityAgents: agents,
      projectTeams: projectTargetProjects,
    }),
  );

  $effect(() => {
    if (
      sharedSelectedAgentId &&
      sharedSelectedAgentId !== lastSharedSelectedAgentId &&
      agents.some((agent) => agent.id === sharedSelectedAgentId)
    ) {
      lastSharedSelectedAgentId = sharedSelectedAgentId;
      if (sharedSelectedAgentId !== selectedAgentId) {
        selectAgent(sharedSelectedAgentId);
      }
    } else if (!sharedSelectedAgentId) {
      lastSharedSelectedAgentId = sharedSelectedAgentId;
    }
  });

  onMount(() => {
    void loadCatalogs();
    void loadProjectCatalog();
    void loadAgents({ preferredAgentId: sharedSelectedAgentId });
  });

  $effect(() => {
    if (lastAgentsRefreshToken === null) {
      lastAgentsRefreshToken = agentsRefreshToken;
      return;
    }
    if (agentsRefreshToken === lastAgentsRefreshToken) {
      return;
    }
    lastAgentsRefreshToken = agentsRefreshToken;
    if (isReordering || reorderInteractionActive) {
      pendingAgentReload = true;
      return;
    }
    void loadAgents({ notify: false, showLoading: false });
  });

  $effect(() => {
    if (lastProjectsRefreshToken === null) {
      lastProjectsRefreshToken = projectsRefreshToken;
      return;
    }
    if (projectsRefreshToken !== lastProjectsRefreshToken) {
      lastProjectsRefreshToken = projectsRefreshToken;
      void loadProjectCatalog();
    }
  });

  async function loadProjectCatalog() {
    const requestId = ++projectCatalogRequestId;
    try {
      const result = await listProjects();
      if (requestId !== projectCatalogRequestId) {
        return;
      }
      const projects = Array.isArray(result?.projects) ? result.projects : [];
      availableProjects = projects.map((project) => ({
        value: project.project_id,
        label: project.display_name || project.project_id,
      }));
      projectCatalogError = '';
      const projectIds = projectIdsFromList(result);
      const teamResults = await Promise.allSettled(
        projectIds.map(async (projectId) => {
          const shown = await showProject(projectId);
          return projectTeamEntry(projectId, shown);
        }),
      );
      if (requestId !== projectCatalogRequestId) {
        return;
      }
      projectTargetProjects = teamResults
        .filter((entry) => entry.status === 'fulfilled')
        .map((entry) => entry.value);
      agentTargetCatalogError = teamResults.some(
        (entry) => entry.status === 'rejected',
      )
        ? t(
            'agents.access.projectTargetsLoadError',
            'Some Project Agent targets could not be loaded.',
          )
        : '';
    } catch (error) {
      if (requestId !== projectCatalogRequestId) {
        return;
      }
      availableProjects = [];
      projectTargetProjects = [];
      projectCatalogError = viewErrorMessage(
        error,
        t('agents.form.projectLoadError', 'Projects could not be loaded.'),
      );
      agentTargetCatalogError = projectCatalogError;
    }
  }

  // Reload the model catalog when the generic invalidation channel signals a
  // model/provider change (first run is a no-op: mount already loaded).
  $effect(() => {
    if (lastModelsRefreshToken === null) {
      lastModelsRefreshToken = modelsRefreshToken;
      return;
    }
    if (modelsRefreshToken !== lastModelsRefreshToken) {
      lastModelsRefreshToken = modelsRefreshToken;
      void reloadModelCatalogs();
    }
  });

  async function fetchModelCatalogs() {
    try {
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
    } catch (error) {
      loadError = viewErrorMessage(error, t('agents.loadError'));
      return null;
    }
  }

  function applyModelCatalogs(catalogs) {
    availableModels = catalogs.models;
    availableConnections = catalogs.connections;
    pendingModelCatalogs = null;
  }

  async function reloadModelCatalogs() {
    const catalogs = await fetchModelCatalogs();
    if (!catalogs) {
      return;
    }
    if (
      shouldApplyReloadNow(SURFACE_FORM, {
        dropdownOpen: modelDropdownOpenCount > 0,
      })
    ) {
      applyModelCatalogs(catalogs);
    } else {
      pendingModelCatalogs = catalogs;
    }
  }

  function trackModelDropdownOpen(open) {
    modelDropdownOpenCount = Math.max(
      0,
      modelDropdownOpenCount + (open ? 1 : -1),
    );
    if (modelDropdownOpenCount === 0 && pendingModelCatalogs) {
      applyModelCatalogs(pendingModelCatalogs);
    }
  }

  async function loadCatalogs() {
    try {
      const [modelsResult, connectionsResult, toolsResult, skillsResult] =
        await Promise.all([
          listModels(),
          listConnections(),
          listTools(),
          listSkills(),
        ]);

      availableModels = Array.isArray(modelsResult?.models)
        ? modelsResult.models
        : [];
      availableConnections = Array.isArray(connectionsResult?.connections)
        ? connectionsResult.connections
        : [];
      availableTools = Array.isArray(toolsResult?.tools)
        ? toolsResult.tools
        : [];
      availableSkills = Array.isArray(skillsResult?.skills)
        ? skillsResult.skills
        : [];
      invalidSkills = Array.isArray(skillsResult?.invalid_skills)
        ? skillsResult.invalid_skills
        : [];
    } catch (error) {
      loadError = viewErrorMessage(error, t('agents.loadError'));
    }
  }

  async function loadAgents(options = {}) {
    const requestId = ++agentListRequestId;
    const showLoading = options.showLoading !== false;
    if (showLoading) {
      isLoading = true;
      loadingAgentListRequestId = requestId;
    }
    loadError = '';

    try {
      const result = await listAgents();
      if (requestId !== agentListRequestId) {
        return;
      }
      agents = Array.isArray(result?.agents) ? result.agents : [];
      agentOrderRevision = Number.isInteger(result?.order_revision)
        ? result.order_revision
        : 0;
      const preferredAgentId = options.preferredAgentId ?? selectedAgentId;
      selectAgent(resolveSelectedAgentId(agents, preferredAgentId));
      if (options.notify !== false) {
        notifyAgentsChanged();
      }
    } catch (error) {
      if (requestId !== agentListRequestId) {
        return;
      }
      loadError = viewErrorMessage(error, t('agents.loadError'));
    } finally {
      if (loadingAgentListRequestId === requestId) {
        isLoading = false;
        loadingAgentListRequestId = 0;
      }
    }
  }

  async function handleAgentsReordered(agentIds) {
    if (isReordering || agentIds.length !== agents.length) {
      return;
    }
    const agentsById = new Map(agents.map((agent) => [agent.id, agent]));
    if (agentIds.some((agentId) => !agentsById.has(agentId))) {
      return;
    }

    const previousAgents = agents;
    const preferredAgentId = selectedAgentId;
    agents = agentIds.map((agentId) => agentsById.get(agentId));
    isReordering = true;
    try {
      const result = await reorderAgents(agentIds, agentOrderRevision);
      agents = Array.isArray(result?.agents) ? result.agents : agents;
      agentOrderRevision = Number.isInteger(result?.order_revision)
        ? result.order_revision
        : agentOrderRevision;
      notifyAgentsChanged();
    } catch (error) {
      agents = previousAgents;
      onToast({
        title: viewErrorMessage(
          error,
          t('agents.order.saveError', 'Agent order could not be saved.'),
        ),
        variant: 'error',
      });
      await loadAgents({
        preferredAgentId,
        notify: false,
        showLoading: false,
      });
    } finally {
      isReordering = false;
      if (pendingAgentReload) {
        pendingAgentReload = false;
        await loadAgents({
          preferredAgentId,
          notify: false,
          showLoading: false,
        });
      }
    }
  }

  function handleReorderInteractionChange(active) {
    reorderInteractionActive = active;
    if (!active && !isReordering && pendingAgentReload) {
      pendingAgentReload = false;
      void loadAgents({ notify: false, showLoading: false });
    }
  }

  function resolveSelectedAgentId(nextAgents, preferredAgentId) {
    if (nextAgents.some((agent) => agent.id === preferredAgentId)) {
      return preferredAgentId;
    }

    return nextAgents[0]?.id ?? '';
  }

  function selectAgent(agentId) {
    selectedAgentId = agentId;
    const agent = agents.find((item) => item.id === agentId) ?? null;
    if (agent) {
      onAgentSelected?.(agent);
    }
  }

  function handleAgentUpdated(nextAgent, options = {}) {
    agents = agents.map((agent) =>
      agent.id === nextAgent.id ? nextAgent : agent,
    );
    notifyAgentsChanged();

    if (options.notifySelection !== false && selectedAgentId === nextAgent.id) {
      onAgentSelected?.(nextAgent);
    }
  }

  function handleAgentRenamed(nextAgent, { oldId, newId }) {
    agents = agents.map((agent) => (agent.id === oldId ? nextAgent : agent));
    if (selectedAgentId === oldId) {
      selectedAgentId = newId;
      onAgentSelected?.(nextAgent);
    }
    notifyAgentsChanged();
  }

  async function openCreateModal() {
    // Fetch the global agent defaults so the modal can label its inherit options.
    // Best-effort: an empty object (fetch failure) makes the modal render the
    // absent-case labels.
    createModalAgentDefaults = {};
    isCreateModalOpen = true;
    try {
      const result = await getSettings();
      const defaults = result?.defaults?.agent;
      createModalAgentDefaults =
        defaults && typeof defaults === 'object' ? defaults : {};
    } catch {
      createModalAgentDefaults = {};
    }
  }

  async function handleAgentCreated(agentId) {
    isCreateModalOpen = false;
    // Creation also emits a roster invalidation. Make the intended selection
    // authoritative before either reload can finish so a competing refresh
    // cannot preserve the previously selected Agent.
    selectAgent(agentId);
    await loadAgents({ preferredAgentId: agentId });
  }

  async function handleAgentDeleted() {
    await loadAgents();
  }

  function notifyAgentsChanged() {
    onAgentsChanged?.(agents);
  }

  function viewErrorMessage(error, fallback) {
    return (
      error?.message ||
      fallback ||
      t('errors.generic', 'Something went wrong. Try again.')
    );
  }
</script>

<section class="agents-view view active" aria-labelledby="agents-list-title">
  <div class="agents-layout">
    <AgentListPane
      {agents}
      {selectedAgentId}
      {isLoading}
      {isReordering}
      onSelect={selectAgent}
      onCreate={openCreateModal}
      onReorder={handleAgentsReordered}
      onReorderInteractionChange={handleReorderInteractionChange}
    />

    {#key selectedAgent?.id ?? 'new-agent'}
      <AgentEditor
        agent={selectedAgent}
        agentsCount={agents.length}
        {availableModels}
        {availableConnections}
        {availableTools}
        {availableSkills}
        {invalidSkills}
        {availableAgentTargets}
        {agentTargetCatalogError}
        projectOptions={availableProjects}
        {projectCatalogError}
        {loadError}
        onAgentUpdated={handleAgentUpdated}
        onAgentRenamed={handleAgentRenamed}
        onAgentCreated={handleAgentCreated}
        onAgentDeleted={handleAgentDeleted}
        {onToast}
        {onNavigateToSettingsPanel}
        {onNavigateToAgentPrompt}
        onModelDropdownOpenChange={trackModelDropdownOpen}
      />
    {/key}
  </div>

  {#if isCreateModalOpen}
    <AgentCreateModal
      {availableModels}
      {availableConnections}
      agentDefaults={createModalAgentDefaults}
      onCreated={handleAgentCreated}
      onClose={() => {
        isCreateModalOpen = false;
      }}
      {onToast}
    />
  {/if}
</section>
