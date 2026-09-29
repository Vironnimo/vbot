<script>
  import { onMount, onDestroy, tick } from 'svelte';
  import { inspectSkill, skillInventory } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import Dropdown from '../Dropdown.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import TextField from '../ui/TextField.svelte';
  import AgentSkillsPanel from './AgentSkillsPanel.svelte';
  import SkillAddMenu from './SkillAddMenu.svelte';
  import SkillCollectionNav from './SkillCollectionNav.svelte';
  import SkillDetail from './SkillDetail.svelte';
  import SkillDialogs from './SkillDialogs.svelte';
  import SkillDirectoryEditor from './SkillDirectoryEditor.svelte';
  import SkillInstallDialog from './SkillInstallDialog.svelte';
  import SkillLibraryList from './SkillLibraryList.svelte';
  import SkillSelectionPanel from './SkillSelectionPanel.svelte';
  import { createSkillActions } from './actions.svelte.js';
  import {
    projectSkillView,
    projectionSkillSections,
    skillAccessOf,
  } from './skillAccess.js';
  import {
    filterSkills,
    LIBRARY_SCOPES,
    SKILL_PAGE_SIZE,
    skillCollectionText,
    skillCollections,
  } from './skillsView.js';
  import './skills.css';

  // The Skills manager: collections (library filters, Agents, Projects), the
  // package list or an Agent's / Project's Skill selection, and the detail of
  // one package. The server owns precedence and write scopes; skill.inventory
  // projects the effective access this view presents and edits.

  const noop = () => {};

  let {
    settings = null,
    onSettingsCommit = noop,
    onToast = noop,
    skillsRefreshToken = 0,
    agentsRefreshToken = 0,
    projectsRefreshToken = 0,
  } = $props();

  let inventory = $state([]);
  let agents = $state([]);
  let projects = $state([]);
  let staleShared = $state([]);
  let policyDiagnostics = $state([]);
  let loading = $state(true);
  let loaded = $state(false);
  let loadError = $state('');

  let scope = $state('all');
  let returnScope = 'all';
  let statusFilter = $state('all');
  let searchQuery = $state('');
  let page = $state(0);
  let selectedId = $state(null);
  let inspected = $state(null);
  let inspectLoading = $state(false);
  let inspectError = $state('');
  let contentTab = $state('instructions');

  let showDirectories = $state(false);
  let directoryError = $state('');
  let showInstall = $state(false);
  let installScope = $state('global');

  let detail = $state();
  let list = $state();
  let addMenu = $state();
  let resultsElement = $state();
  let directoryEditor = $state();

  let inventoryVersion = 0;
  let inspectVersion = 0;
  let disposed = false;
  let pendingInstallSelection = null;

  const actions = createSkillActions({
    get agents() {
      return agents;
    },
    get inspected() {
      return inspected;
    },
    get onToast() {
      return onToast;
    },
    get loadInventory() {
      return loadInventory;
    },
  });

  let collections = $derived(skillCollections(inventory, agents, projects));
  let collection = $derived(collections.find((item) => item.key === scope));
  let collectionText = $derived(skillCollectionText(collection));
  let isLibrary = $derived(LIBRARY_SCOPES.includes(scope));
  let scopeAgent = $derived(
    collection?.section === 'agents'
      ? agents.find((agent) => agent.id === collection.id)
      : null,
  );
  let scopeProject = $derived(
    collection?.section === 'projects'
      ? projects.find((project) => project.project_id === collection.id)
      : null,
  );
  let projectView = $derived(
    scopeProject
      ? projectSkillView(projectionSkillSections(scopeProject, inventory))
      : null,
  );
  let filtered = $derived(
    isLibrary
      ? filterSkills(inventory, searchQuery, scope, statusFilter, agents)
      : [],
  );
  let pageCount = $derived(
    Math.max(1, Math.ceil(filtered.length / SKILL_PAGE_SIZE)),
  );
  let currentPage = $derived(Math.min(page, pageCount - 1));
  let visibleSkills = $derived(
    filtered.slice(
      currentPage * SKILL_PAGE_SIZE,
      (currentPage + 1) * SKILL_PAGE_SIZE,
    ),
  );
  let selected = $derived(
    inventory.find((entry) => entry.id === selectedId) ?? null,
  );
  let statusOptions = $derived([
    { value: 'all', label: t('skills.filter.all') },
    { value: 'attention', label: t('skills.filter.attention') },
    { value: 'disabled', label: t('skills.status.disabled') },
    { value: 'available', label: t('skills.status.available') },
  ]);

  onMount(() => {
    void loadInventory();
  });

  onDestroy(() => {
    disposed = true;
    inventoryVersion++;
    inspectVersion++;
  });

  // Skills, Agent and Project resource events (our own writes included) bump
  // these tokens; refresh server truth without tearing down open drafts.
  let lastTokens = null;
  $effect(() => {
    const tokens = [
      skillsRefreshToken,
      agentsRefreshToken,
      projectsRefreshToken,
    ];
    const changed =
      lastTokens && tokens.some((token, index) => token !== lastTokens[index]);
    lastTokens = tokens;
    if (changed) void loadInventory();
  });

  function clearSelection() {
    pendingInstallSelection = null;
    selectedId = null;
    inspected = null;
    inspectError = '';
    inspectLoading = false;
    inspectVersion++;
  }

  function changeScope(next) {
    scope = next;
    page = 0;
    clearSelection();
  }

  function changeSearch(next) {
    searchQuery = next;
    page = 0;
    if (isLibrary) clearSelection();
  }

  function changeStatus(next) {
    statusFilter = next;
    page = 0;
    clearSelection();
  }

  function changePage(next) {
    page = next;
    clearSelection();
    list?.scrollToTop();
  }

  async function inspect(entry, quiet) {
    const version = ++inspectVersion;
    if (!quiet) {
      inspectLoading = true;
      inspectError = '';
    }
    try {
      const result = await inspectSkill(entry.id);
      if (!disposed && version === inspectVersion) {
        inspected = result;
        inspectError = '';
      }
    } catch (error) {
      if (!disposed && version === inspectVersion && !quiet)
        inspectError = error.message;
    } finally {
      if (!disposed && version === inspectVersion) inspectLoading = false;
    }
  }

  async function openSkill(entry, focus = true) {
    pendingInstallSelection = null;
    const quiet = inspected?.id === entry.id && !focus;
    selectedId = entry.id;
    if (inspected?.id !== entry.id) inspected = null;
    if (focus) {
      contentTab = 'instructions';
      const request = inspect(entry, false);
      await tick();
      detail?.focus();
      await request;
      return;
    }
    await inspect(entry, quiet);
  }

  function openPackage(item) {
    const entry = inventory.find(
      (candidate) => candidate.id === item.packageId,
    );
    if (entry) void openSkill(entry);
  }

  async function closeDetail() {
    const entry = selected;
    clearSelection();
    await tick();
    if (!entry) return;
    if (isLibrary) {
      list?.focusRow(entry.id);
      return;
    }
    [...(resultsElement?.querySelectorAll('[data-item-key]') ?? [])]
      .find((element) => element.dataset.itemKey === entry.name)
      ?.focus();
  }

  async function loadInventory() {
    const version = ++inventoryVersion;
    loading = true;
    loadError = '';
    try {
      const result = await skillInventory();
      if (disposed || version !== inventoryVersion) return;
      inventory = Array.isArray(result?.skills) ? result.skills : [];
      agents = Array.isArray(result?.agents) ? result.agents : [];
      projects = Array.isArray(result?.projects) ? result.projects : [];
      staleShared = result?.stale_shared ?? [];
      policyDiagnostics = result?.policy_diagnostics ?? [];
      loaded = true;
      if (scope !== 'directories' && !collection) changeScope('all');
      const installedEntry =
        pendingInstallSelection &&
        inventory.find(
          (entry) =>
            entry.name === pendingInstallSelection.name &&
            entry.editable_scope === pendingInstallSelection.scope,
        );
      if (installedEntry) {
        pendingInstallSelection = null;
        const index = filtered.findIndex(
          (entry) => entry.id === installedEntry.id,
        );
        page = Math.max(0, Math.floor(index / SKILL_PAGE_SIZE));
        void openSkill(installedEntry);
      } else if (selectedId) {
        const entry = inventory.find((item) => item.id === selectedId);
        if (!entry) clearSelection();
        else if (!actions.editing) void openSkill(entry, false);
      }
    } catch (error) {
      if (!disposed && version === inventoryVersion)
        loadError = `${t('skills.loadError')} ${error.message}`;
    } finally {
      if (!disposed && version === inventoryVersion) loading = false;
    }
  }

  function ownScope() {
    return scopeAgent ? `agent:${scopeAgent.id}` : actions.GLOBAL_SCOPE;
  }

  function openInstall() {
    installScope = ownScope();
    showInstall = true;
  }

  async function openDirectories() {
    if (scope !== 'directories') returnScope = scope;
    showDirectories = true;
    changeScope('directories');
    await tick();
    directoryEditor?.focusNewDirectory();
  }

  async function closeDirectories() {
    changeScope(
      collections.some((item) => item.key === returnScope)
        ? returnScope
        : 'all',
    );
    await tick();
    addMenu?.focus();
  }

  async function installed(result) {
    showInstall = false;
    changeScope(result.scope === actions.GLOBAL_SCOPE ? 'global' : 'all');
    searchQuery = '';
    statusFilter = 'all';
    // A later resource event can supersede our refresh; the winning inventory
    // response still fulfills this selection unless the user navigates away.
    pendingInstallSelection = { name: result.name, scope: result.scope };
    onToast({
      title:
        result.operation === 'unchanged'
          ? t('skills.install.unchanged')
          : t('skills.install.success', { name: result.name }),
      variant: 'success',
    });
    if (result.warnings?.length)
      onToast({ title: result.warnings.join('\n'), variant: 'warn' });
    await loadInventory();
  }

  function setProjectGroup(groupId, on) {
    const group = projectView?.groups.find((item) => item.id === groupId);
    if (group)
      void actions.updateProjectSkills(
        scopeProject,
        groupId,
        group.items.map((item) => item.name),
        on,
      );
  }
</script>

<section class="skills-view view active" aria-labelledby="skills-title">
  <aside class="skills-nav secondary-pane" aria-label={t('skills.collections')}>
    <SkillCollectionNav {collections} {scope} onSelect={changeScope} />
  </aside>

  <div class="skills-main">
    <div class="skills-mobile-nav" class:skills-mobile-hidden={selected}>
      <SkillCollectionNav
        variant="dropdown"
        {collections}
        {scope}
        onSelect={changeScope}
      />
    </div>
    <header
      class="view-header skills-header"
      class:skills-mobile-hidden={selected}
    >
      <div class="view-header__intro">
        <h2 id="skills-title" class="view-header__title">
          {scope === 'directories'
            ? t('skills.folders.title')
            : collection?.label || t('skills.title')}
        </h2>
        <p class="view-header__subtitle">
          {scope === 'directories'
            ? t('skills.locationsSubtitle')
            : collectionText.subtitle}
        </p>
      </div>
      {#if scope === 'directories'}
        <Button variant="secondary" onClick={closeDirectories}
          >← {t('common.back')}</Button
        >
      {/if}
    </header>

    {#if loadError}
      <Banner variant="error" role="alert"
        >{loadError}<Button variant="secondary" onClick={loadInventory}
          >{t('common.retry')}</Button
        ></Banner
      >
    {/if}
    {#if staleShared.length || policyDiagnostics.length}
      <details class="skills-notice">
        <summary
          >{t('skills.policyAttention', {
            count: staleShared.length + policyDiagnostics.length,
          })}</summary
        >
        {#if staleShared.length}<p>
            {staleShared.length === 1
              ? t('skills.staleSharedOne')
              : t('skills.staleShared', { count: staleShared.length })}
          </p>{/if}
        <ul>
          {#each staleShared as item, index (index)}<li>
              {item.agent_id} / {item.name}
            </li>{/each}{#each policyDiagnostics as line, index (index)}<li>
              {line}
            </li>{/each}
        </ul>
      </details>
    {/if}

    <div class="skills-directories" hidden={scope !== 'directories'}>
      {#if showDirectories}
        {#if directoryError}<Banner variant="error">{directoryError}</Banner
          >{/if}
        <SkillDirectoryEditor
          bind:this={directoryEditor}
          {settings}
          onCommit={(nextSettings) => {
            onSettingsCommit(nextSettings);
            void loadInventory();
          }}
          {onToast}
          onError={(message) => (directoryError = message)}
        />
      {/if}
    </div>
    {#if scope !== 'directories'}
      <div class="skills-toolbar" class:skills-mobile-hidden={selected}>
        <SkillAddMenu
          bind:this={addMenu}
          disabled={actions.busy}
          onInstall={openInstall}
          onCreate={() => actions.openCreateModal(ownScope())}
          onFolders={openDirectories}
        />
        <div class="skills-search">
          <svg
            viewBox="0 0 16 16"
            width="16"
            height="16"
            fill="none"
            stroke="currentColor"
            aria-hidden="true"
            ><circle cx="7" cy="7" r="4.5" /><path d="m10.5 10.5 3 3" /></svg
          >
          <TextField
            type="search"
            value={searchQuery}
            onInput={changeSearch}
            placeholder={isLibrary
              ? t('skills.searchLibrary')
              : t('skills.panel.filterPlaceholder')}
            ariaLabel={isLibrary
              ? t('skills.searchLibrary')
              : t('skills.panel.filter')}
          />
        </div>
        {#if isLibrary}
          <Dropdown
            value={statusFilter}
            options={statusOptions}
            ariaLabel={t('skills.filter.label')}
            onValueChange={changeStatus}
          />
        {/if}
      </div>
      <div class="skills-workspace" class:skills-workspace--selected={selected}>
        <section
          class="skills-results"
          class:skills-mobile-hidden={selected}
          aria-label={t('skills.results')}
          aria-busy={actions.busy}
          bind:this={resultsElement}
        >
          {#if isLibrary}
            <SkillLibraryList
              bind:this={list}
              entries={visibleSkills}
              total={filtered.length}
              {selectedId}
              {loading}
              {loaded}
              filtersActive={Boolean(searchQuery) || statusFilter !== 'all'}
              emptyTitle={collectionText.empty}
              emptyHelp={collectionText.emptyHelp}
              {agents}
              {projects}
              page={currentPage}
              {pageCount}
              onOpen={(entry) => openSkill(entry)}
              onPage={changePage}
              onClearFilters={() => {
                searchQuery = '';
                changeStatus('all');
              }}
            />
          {:else if scopeAgent}
            <div class="skills-panel-scroll">
              <AgentSkillsPanel
                agent={scopeAgent}
                access={skillAccessOf(scopeAgent)}
                {inventory}
                {agents}
                {projects}
                query={searchQuery}
                onChange={(next) => actions.updateAgentAccess(scopeAgent, next)}
                onOpen={openPackage}
              />
            </div>
          {:else if projectView}
            <div class="skills-panel-scroll">
              <SkillSelectionPanel
                groups={projectView.groups}
                active={projectView.active}
                total={projectView.total}
                query={searchQuery}
                onToggle={(groupId, name, on) =>
                  actions.updateProjectSkills(
                    scopeProject,
                    groupId,
                    [name],
                    on,
                  )}
                onSetAll={setProjectGroup}
                onOpen={openPackage}
                emptyTitle={collectionText.empty}
                emptyHelp={collectionText.emptyHelp}
              />
            </div>
          {/if}
        </section>
        {#if selected}
          <SkillDetail
            bind:this={detail}
            entry={selected}
            {inspected}
            {inspectLoading}
            {inspectError}
            {contentTab}
            {agents}
            {projects}
            {inventory}
            busy={actions.busy}
            onBack={closeDetail}
            onRetry={() => openSkill(selected, false)}
            onTab={(next) => (contentTab = next)}
            onEdit={actions.startEdit}
            onDelete={actions.requestDelete}
            onSetDisabled={actions.setDisabled}
            onAgentAccess={actions.updateAgentAccess}
            onShare={actions.setSharing}
            onProjectSkills={actions.updateProjectSkills}
          />
        {/if}
      </div>
    {/if}
  </div>
</section>

{#if showInstall}
  <SkillInstallDialog
    initialScope={installScope}
    scopeOptions={actions.scopeOptions}
    onClose={() => (showInstall = false)}
    onInstalled={installed}
  />
{/if}

<SkillDialogs {actions} />
