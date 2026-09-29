<script>
  import { onDestroy, onMount, tick, untrack } from 'svelte';

  import {
    getSettings,
    listAgents,
    listConnections,
    listModels,
    listProjects,
    listTools,
    reorderAgents,
    showProject,
    skillInventory,
  } from '$lib/api.js';
  import { buildAgentTargetCatalog } from '$lib/agentForm.js';
  import { useAutosaveContext } from '$lib/autosave.js';
  import { createAgentTargetCatalogLoader } from '$lib/agentTargetOptions.js';
  import { t } from '$lib/i18n.js';
  import { createModelCatalogLoader } from '$lib/modelSelection.js';
  import { createStandaloneNavigation } from '$lib/navigation.svelte.js';
  import { shouldApplyReloadNow } from '$lib/resourceInvalidation.js';

  import AgentCreateModal from './agents/AgentCreateModal.svelte';
  import AgentEditor from './agents/AgentEditor.svelte';
  import SettingsDefaultsPanel from './settings/SettingsDefaultsPanel.svelte';
  import SettingsCompactionPanel from './settings/SettingsCompactionPanel.svelte';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import AgentListPane from './agents/AgentListPane.svelte';

  const noop = () => {};
  // The shared defaults place. Agent ids are letters, digits, hyphens and
  // underscores, so it never names an Agent.
  const DEFAULTS_PLACE = '~defaults';
  const autosaveContext = useAutosaveContext();
  const modelCatalogLoader = createModelCatalogLoader({
    listModels,
    listConnections,
  });
  let {
    // The place is the shown Agent (`[agentId]`) or the shared defaults
    // (`['~defaults']`, `['~defaults', 'compaction']` for that section); an
    // empty place shows the shared selected Agent.
    navigation = createStandaloneNavigation(),
    // Seeds the empty place; the place decides what the view shows.
    sharedSelectedAgentId = '',
    onAgentsChanged,
    onAgentSelected,
    onToast = noop,
    onNavigateToSettingsPanel = noop,
    onNavigateToAgentPrompt = noop,
    modelsRefreshToken = 0,
    projectsRefreshToken = 0,
    agentsRefreshToken = 0,
    memoriesRefreshToken = 0,
    skillsRefreshToken = 0,
  } = $props();
  let sharedDefaultsOpen = $state(false);
  let sharedSettings = $state(null);
  let sharedSettingsError = $state('');
  let sharedSettingsLoading = $state(false);
  let sharedContent = $state(null);
  let defaultsRequestId = 0;
  let defaultsShowRequestId = 0;
  let destroyed = false;

  // Shows the shared defaults, scrolled to the section the place names (the
  // jump links inside the pane only scroll).
  async function showSharedDefaults(panelId) {
    const requestId = ++defaultsShowRequestId;
    sharedDefaultsOpen = true;
    if (!sharedSettings) await loadSharedSettings();
    await tick();
    if (destroyed || requestId !== defaultsShowRequestId || !sharedDefaultsOpen)
      return;
    if (panelId === 'compaction') jumpToDefaultsSection(panelId);
    else sharedContent?.scrollTo?.({ top: 0 });
  }

  function jumpToDefaultsSection(panelId) {
    sharedContent
      ?.querySelector(`[data-settings-section="${panelId}"]`)
      ?.scrollIntoView?.({ block: 'start' });
  }

  async function loadSharedSettings() {
    const requestId = ++defaultsRequestId;
    sharedSettingsLoading = true;
    sharedSettingsError = '';
    try {
      const result = await getSettings();
      if (!destroyed && requestId === defaultsRequestId)
        sharedSettings = result;
    } catch (error) {
      if (!destroyed && requestId === defaultsRequestId)
        sharedSettingsError = viewErrorMessage(error, t('settings.loadError'));
    } finally {
      if (!destroyed && requestId === defaultsRequestId)
        sharedSettingsLoading = false;
    }
  }

  function commitSharedSettings(nextSettings) {
    sharedSettings = nextSettings;
    void loadAgents({ showLoading: false });
  }

  function sharedSettingsFailure(message) {
    if (message)
      onToast({
        title: t('errors.appError'),
        message,
        variant: 'error',
      });
  }

  function navigateFromAgent(panelId) {
    if (panelId === 'defaults' || panelId === 'compaction')
      return navigation.navigate([
        DEFAULTS_PLACE,
        ...(panelId === 'compaction' ? [panelId] : []),
      ]);
    return onNavigateToSettingsPanel(panelId);
  }

  let agents = $state([]);
  let selectedAgentId = $state('');
  // The first roster arrived / a roster read is in flight. A read in flight
  // decides the shown Agent from the place when it lands.
  let agentsLoaded = $state(false);
  let rosterLoading = $state(false);
  let agentOrderRevision = $state(0);
  let isReordering = $state(false);
  let reorderInteractionActive = $state(false);
  let pendingAgentReload = false;
  let isCreateModalOpen = $state(false);
  let isLoading = $state(false);
  let loadError = $state('');
  let availableModels = $state([]);
  let availableConnections = $state([]);
  let availableTools = $state([]);
  // skill.inventory: packages plus each Agent's and Project's Skill access.
  let skillCatalog = $state({ skills: [], agents: [], projects: [] });
  let skillCatalogRequestId = 0;
  let lastSkillTokens = null;
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
  const targetCatalog = createAgentTargetCatalogLoader({
    listProjects,
    showProject,
  });

  let selectedAgent = $derived(
    agents.find((agent) => agent.id === selectedAgentId) ?? null,
  );
  let availableAgentTargets = $derived(
    buildAgentTargetCatalog({
      identityAgents: agents,
      projectTeams: projectTargetProjects,
    }),
  );

  // Place -> shown pane and Agent. Before the first roster read, the read
  // resolves the place itself. An Agent the roster does not know yet (a
  // rename or a new Agent from elsewhere) waits for a roster read, which
  // shows it or falls back and corrects the entry.
  $effect(() => {
    const [target = '', panelId = ''] = navigation.place;
    untrack(() => {
      if (target === DEFAULTS_PLACE) {
        void showSharedDefaults(panelId);
        return;
      }
      sharedDefaultsOpen = false;
      if (!agentsLoaded) return;
      if (!target) {
        applyAgentSelection(
          resolveShownAgentId([sharedSelectedAgentId, selectedAgentId]),
        );
        showSelectionInPlace();
        return;
      }
      if (agents.some((agent) => agent.id === target)) {
        if (target !== selectedAgentId) applyAgentSelection(target);
        return;
      }
      if (!rosterLoading)
        void loadAgents({ notify: false, showLoading: false });
    });
  });

  // Shown Agent -> place. A roster read that showed another Agent (the place
  // named a deleted or unknown one, or was empty) and a rename correct the
  // current entry.
  $effect(() => {
    const agentId = selectedAgentId;
    const settled = agentsLoaded && !rosterLoading && !sharedDefaultsOpen;
    if (!settled) return;
    untrack(() => showSelectionInPlace(agentId));
  });

  function showSelectionInPlace(agentId = selectedAgentId) {
    if (sharedDefaultsOpen || (navigation.place[0] ?? '') === agentId) return;
    navigation.replace(agentId ? [agentId] : []);
  }

  // The Agent the place names, else the first of `candidates` the roster
  // knows, else the first Agent.
  function resolveShownAgentId(candidates) {
    const [target = ''] = navigation.place;
    return (
      [target === DEFAULTS_PLACE ? '' : target, ...candidates].find(
        (agentId) => agentId && agents.some((agent) => agent.id === agentId),
      ) ??
      agents[0]?.id ??
      ''
    );
  }

  onMount(() => {
    void loadCatalogs();
    void loadProjectCatalog();
    void loadAgents();
  });

  onDestroy(() => {
    targetCatalog.dispose();
    destroyed = true;

    modelCatalogLoader.invalidate();
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

  // Skill access depends on Skills, Agents (allowlists, rooting) and Projects
  // (their Skill lists), so any of their resource events refreshes it. The
  // editor draft is never replaced by a refresh.
  $effect(() => {
    const tokens = [
      skillsRefreshToken,
      agentsRefreshToken,
      projectsRefreshToken,
    ];
    const changed =
      lastSkillTokens &&
      tokens.some((token, index) => token !== lastSkillTokens[index]);
    lastSkillTokens = tokens;
    if (changed)
      loadSkillCatalog().catch((error) => {
        loadError = viewErrorMessage(error, t('agents.loadError'));
      });
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
    const catalog = await targetCatalog.load();
    if (!catalog) return;
    availableProjects = catalog.projects.map((project) => ({
      value: project.project_id,
      label: project.display_name || project.project_id,
    }));
    projectTargetProjects = catalog.projectTeams;
    projectCatalogError = catalog.projectError
      ? viewErrorMessage(
          catalog.projectError,
          t('agents.form.projectLoadError'),
        )
      : '';
    agentTargetCatalogError =
      projectCatalogError ||
      (catalog.failedProjects.length
        ? t('agents.access.projectTargetsLoadError')
        : '');
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

  function applyModelCatalogs(catalogs) {
    availableModels = catalogs.models;
    availableConnections = catalogs.connections;
    pendingModelCatalogs = null;
  }

  async function reloadModelCatalogs() {
    pendingModelCatalogs = null;
    let catalogs;
    try {
      catalogs = await modelCatalogLoader.load();
    } catch (error) {
      loadError = viewErrorMessage(error, t('agents.loadError'));
      return;
    }
    if (catalogs === null) {
      return;
    }
    if (
      shouldApplyReloadNow({
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
    pendingModelCatalogs = null;
    try {
      const [catalogs, toolsResult] = await Promise.all([
        modelCatalogLoader.load(),
        listTools(),
        loadSkillCatalog(),
      ]);

      if (catalogs !== null) {
        applyModelCatalogs(catalogs);
      }
      availableTools = Array.isArray(toolsResult?.tools)
        ? toolsResult.tools
        : [];
    } catch (error) {
      loadError = viewErrorMessage(error, t('agents.loadError'));
    }
  }

  async function loadSkillCatalog() {
    const requestId = ++skillCatalogRequestId;
    const result = await skillInventory();
    if (destroyed || requestId !== skillCatalogRequestId) return;
    skillCatalog = {
      skills: Array.isArray(result?.skills) ? result.skills : [],
      agents: Array.isArray(result?.agents) ? result.agents : [],
      projects: Array.isArray(result?.projects) ? result.projects : [],
    };
  }

  async function loadAgents(options = {}) {
    const requestId = ++agentListRequestId;
    const showLoading = options.showLoading !== false;
    if (showLoading) {
      isLoading = true;
      loadingAgentListRequestId = requestId;
    }
    rosterLoading = true;
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
      applyAgentSelection(
        resolveShownAgentId([selectedAgentId, sharedSelectedAgentId]),
      );
      agentsLoaded = true;
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
      if (requestId === agentListRequestId) {
        rosterLoading = false;
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
        title: viewErrorMessage(error, t('agents.order.saveError')),
        variant: 'error',
      });
      await loadAgents({ notify: false, showLoading: false });
    } finally {
      isReordering = false;
      if (pendingAgentReload) {
        pendingAgentReload = false;
        await loadAgents({ notify: false, showLoading: false });
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

  function applyAgentSelection(agentId) {
    selectedAgentId = agentId;
    const agent = agents.find((item) => item.id === agentId) ?? null;
    if (agent) {
      onAgentSelected?.(agent);
    }
    return true;
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

  function openCreateModal() {
    return autosaveContext.requestTransition(openCreateModalAfterSave);
  }

  async function openCreateModalAfterSave() {
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

  // Showing the new Agent is a step. Creation also emits a roster
  // invalidation; whichever roster read lands last shows the Agent the place
  // names.
  async function handleAgentCreated(agentId) {
    isCreateModalOpen = false;
    navigation.navigate([agentId]);
    await loadAgents();
  }

  // The roster read falls back to another Agent and corrects the entry.
  async function handleAgentDeleted() {
    await loadAgents();
  }

  function notifyAgentsChanged() {
    onAgentsChanged?.(agents);
  }

  function viewErrorMessage(error, fallback) {
    return error?.message || fallback || t('errors.generic');
  }
</script>

<section class="agents-view view active" aria-labelledby="agents-list-title">
  <div class="agents-layout">
    <AgentListPane
      {agents}
      selectedAgentId={sharedDefaultsOpen ? '' : selectedAgentId}
      {sharedDefaultsOpen}
      onOpenSharedDefaults={() => navigation.navigate([DEFAULTS_PLACE])}
      {isLoading}
      {isReordering}
      onSelect={(agentId) => navigation.navigate([agentId])}
      onCreate={openCreateModal}
      onReorder={handleAgentsReordered}
      onReorderInteractionChange={handleReorderInteractionChange}
    />

    <div class="agent-editor-host" hidden={sharedDefaultsOpen}>
      {#key selectedAgent?.id ?? 'new-agent'}
        <AgentEditor
          agent={selectedAgent}
          agentsCount={agents.length}
          {availableModels}
          {availableConnections}
          {availableTools}
          {skillCatalog}
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
          onNavigateToSettingsPanel={navigateFromAgent}
          {onNavigateToAgentPrompt}
          {memoriesRefreshToken}
          onModelDropdownOpenChange={trackModelDropdownOpen}
        />
      {/key}
    </div>

    {#if sharedDefaultsOpen || sharedSettings}
      <div class="agent-shared-pane" hidden={!sharedDefaultsOpen}>
        <div
          class="agent-detail-scroll agent-shared-content"
          bind:this={sharedContent}
        >
          <div class="management-header agent-shared-header">
            <div class="detail-top agent-shared-title">
              <div>
                <h2 class="detail-heading">
                  {t('agents.shared.title')}
                </h2>
                <p class="agent-shared-scope">
                  {t('agents.shared.scope')}
                </p>
              </div>
              <Button
                variant="secondary"
                onClick={() =>
                  navigation.up(selectedAgentId ? [selectedAgentId] : [])}
                >{t('agents.shared.back')}</Button
              >
            </div>
            <nav
              class="agent-shared-jumps"
              aria-label={t('agents.shared.sections')}
            >
              <Button
                variant="tertiary"
                onClick={() => jumpToDefaultsSection('defaults')}
                >{t('agents.shared.modelTitle')}</Button
              >
              <Button
                variant="tertiary"
                onClick={() => jumpToDefaultsSection('compaction')}
                >{t('settings.compaction.title')}</Button
              >
            </nav>
          </div>
          {#if sharedSettingsLoading}
            <Banner variant="neutral">{t('settings.loading')}</Banner>
          {:else if sharedSettingsError}
            <Banner variant="error"
              >{sharedSettingsError}<Button
                variant="secondary"
                onClick={loadSharedSettings}>{t('common.retry')}</Button
              ></Banner
            >
          {:else if sharedSettings}
            <section
              class="s-section"
              data-settings-section="defaults"
              aria-labelledby="agent-shared-model-title"
            >
              <header class="s-section__head">
                <h3 class="s-section__title" id="agent-shared-model-title">
                  {t('agents.shared.modelTitle')}
                </h3>
              </header>
              <p class="s-section__desc">
                {t('agents.shared.modelDescription')}
              </p>
              <div class="s-section__body">
                <SettingsDefaultsPanel
                  settings={sharedSettings}
                  onCommit={commitSharedSettings}
                  {onToast}
                  onError={sharedSettingsFailure}
                  {modelsRefreshToken}
                />
              </div>
            </section>

            <section
              class="s-section"
              data-settings-section="compaction"
              aria-labelledby="agent-shared-compaction-title"
            >
              <header class="s-section__head">
                <h3 class="s-section__title" id="agent-shared-compaction-title">
                  {t('settings.compaction.title')}
                </h3>
              </header>
              <p class="s-section__desc">
                {t('agents.shared.compactionDescription')}
              </p>
              <div id="agent-shared-compaction" class="s-section__body">
                <SettingsCompactionPanel
                  settings={sharedSettings}
                  onCommit={commitSharedSettings}
                  {onToast}
                  onError={sharedSettingsFailure}
                  {modelsRefreshToken}
                />
              </div>
            </section>
          {/if}
        </div>
      </div>
    {/if}
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
