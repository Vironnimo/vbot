<script>
  import { onMount, onDestroy, tick, untrack } from 'svelte';
  import { inspectSkill, skillInventory } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import { createStandaloneNavigation } from '$lib/navigation.svelte.js';
  import { isImeComposing } from '$lib/keyboard.js';
  import Dropdown from '../Dropdown.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import ContextMenu from '../ui/ContextMenu.svelte';
  import { contextMenuAnchor } from '../ui/contextMenu.js';
  import TextField from '../ui/TextField.svelte';
  import AgentSkillsPanel from './AgentSkillsPanel.svelte';
  import LibrarianSection from './LibrarianSection.svelte';
  import SkillAddMenu from './SkillAddMenu.svelte';
  import SkillArchiveList from './SkillArchiveList.svelte';
  import SkillCollectionNav from './SkillCollectionNav.svelte';
  import SkillDialogs from './SkillDialogs.svelte';
  import SkillDirectoryEditor from './SkillDirectoryEditor.svelte';
  import SkillInstallDialog from './SkillInstallDialog.svelte';
  import SkillLibraryList from './SkillLibraryList.svelte';
  import SkillPage from './SkillPage.svelte';
  import SkillSelectionPanel from './SkillSelectionPanel.svelte';
  import { createSkillActions } from './actions.svelte.js';
  import {
    projectSkillView,
    projectionSkillSections,
    skillAccessOf,
  } from './skillAccess.js';
  import {
    agentRowMenu,
    archivedRowMenu,
    libraryRowMenu,
    projectRowMenu,
  } from './skillMenus.js';
  import { filterArchivedSkills } from './skillRecords.js';
  import {
    ARCHIVED_COLLECTION,
    filterSkills,
    LIBRARY_SCOPES,
    SKILL_PAGE_SIZE,
    skillCollectionText,
    skillCollections,
  } from './skillsView.js';
  import './skills.css';

  // The Skills manager: collection navigation (library filters, archived
  // Skills, Agents, Projects) beside one content area. The content shows the
  // collection page (the package list, the archived packages, or an Agent's /
  // Project's Skill selection) or, in its place, the page of one package;
  // returning restores the collection page's scroll position, filters and
  // focused row. The server owns precedence and write scopes; skill.inventory
  // projects the effective access this view presents and edits.
  //
  // Its place: `[collection]` for a collection page, `[collection, skillId]`
  // for a package page, `['directories']` for the Skill folders. Filters,
  // search and content tabs are not places.

  const noop = () => {};

  let {
    navigation = createStandaloneNavigation(),
    settings = null,
    onSettingsCommit = noop,
    onToast = noop,
    // Opens a Session in Chat (a Librarian pass opens the Librarian's).
    onOpenSession = noop,
    skillsRefreshToken = 0,
    agentsRefreshToken = 0,
    projectsRefreshToken = 0,
  } = $props();

  let inventory = $state([]);
  let archived = $state([]);
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

  let viewElement = $state();
  let collectionTitle = $state();
  let skillPage = $state();
  let list = $state();
  let addMenu = $state();
  let resultsElement = $state();
  let directoryEditor = $state();

  let inventoryVersion = 0;
  let inspectVersion = 0;
  let disposed = false;
  let pendingInstallSelection = null;
  // The collection page's scroll offset when a package page replaced it.
  let returnScrollTop = null;
  // The package opened last from the library, marked when returning.
  let lastOpenedId = $state(null);
  // The open row context menu (components/ui/ContextMenu.svelte), or null.
  let menu = $state(null);
  // The content tab the next opened package page starts on, when a link
  // asked for one (the Librarian's changes open a Skill's history).
  let pendingContentTab = null;

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

  let collections = $derived(
    skillCollections(inventory, agents, projects, archived),
  );
  let collection = $derived(collections.find((item) => item.key === scope));
  let collectionText = $derived(skillCollectionText(collection));
  let isLibrary = $derived(LIBRARY_SCOPES.includes(scope));
  let isArchive = $derived(scope === ARCHIVED_COLLECTION);
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
  let filteredArchived = $derived(
    isArchive ? filterArchivedSkills(archived, searchQuery, agents) : [],
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
    lastOpenedId = null;
    returnScrollTop = null;
    clearSelection();
  }

  // Place -> shown page. Package pages wait for the first inventory; an
  // unknown collection or package corrects the entry to what is shown.
  let placeApplied = Promise.resolve();
  $effect(() => {
    const [collectionKey = '', skillId = ''] = navigation.place;
    const ready = loaded;
    untrack(() => {
      placeApplied = applyPlace(collectionKey, skillId, ready);
    });
  });

  async function applyPlace(collectionKey, skillId, ready) {
    if (!collectionKey) {
      navigation.replace(['all']);
      return;
    }
    if (collectionKey !== scope) {
      if (
        collectionKey !== 'directories' &&
        ready &&
        !collections.some((item) => item.key === collectionKey)
      ) {
        navigation.replace(['all']);
        return;
      }
      const leavingDirectories = scope === 'directories';
      if (collectionKey === 'directories') {
        returnScope = scope;
        showDirectories = true;
      }
      changeScope(collectionKey);
      if (collectionKey === 'directories') {
        await tick();
        directoryEditor?.focusNewDirectory();
      } else if (leavingDirectories) {
        await tick();
        addMenu?.focus();
      }
    }
    if (!skillId) {
      if (selectedId !== null) await returnToCollection(selected);
      return;
    }
    if (!ready || selectedId === skillId) return;
    const entry = inventory.find((item) => item.id === skillId);
    if (entry) await openSkill(entry);
    else navigation.replace([collectionKey]);
  }

  // Opening a package page is a step; its back controls go up to the
  // collection page.
  function showSkill(entry) {
    navigation.navigate([scope, entry.id]);
  }

  function leaveSkill() {
    navigation.up([scope]);
  }

  // Choosing the collection whose package page is open returns to it.
  function selectCollection(next) {
    if (next === scope && selected) leaveSkill();
    else navigation.navigate([next]);
  }

  function changeSearch(next) {
    searchQuery = next;
    page = 0;
    lastOpenedId = null;
  }

  function changeStatus(next) {
    statusFilter = next;
    page = 0;
    lastOpenedId = null;
  }

  function changePage(next) {
    page = next;
    lastOpenedId = null;
    list?.scrollToTop();
  }

  function collectionScroller() {
    return resultsElement?.querySelector('.skills-list, .skills-panel-scroll');
  }

  // The row that opened `entry` on the collection page, if it is still there.
  function collectionRow(entry) {
    const attribute = isLibrary ? 'skillId' : 'itemKey';
    const value = isLibrary ? entry.id : entry.name;
    return [
      ...(resultsElement?.querySelectorAll(
        isLibrary ? '[data-skill-id]' : '[data-item-key]',
      ) ?? []),
    ].find((element) => element.dataset[attribute] === value);
  }

  // Back from a package page: the collection page reappears as it was left.
  async function returnToCollection(entry = selected) {
    const scrollTop = returnScrollTop;
    returnScrollTop = null;
    clearSelection();
    await tick();
    const scroller = collectionScroller();
    if (scroller && scrollTop !== null) scroller.scrollTop = scrollTop;
    const row = entry ? collectionRow(entry) : null;
    (row ?? collectionTitle)?.focus({ preventScroll: true });
  }

  function isTextEntry(target) {
    return Boolean(
      target?.closest?.('input, textarea, select') || target?.isContentEditable,
    );
  }

  // Escape leaves a package page unless something else consumed it (a
  // dialog, menu or picker) or focus is in a text field.
  function handleWindowKeydown(event) {
    if (
      event.key !== 'Escape' ||
      !selected ||
      event.defaultPrevented ||
      isImeComposing(event) ||
      isTextEntry(event.target)
    )
      return;
    if (event.target !== document.body && !viewElement?.contains(event.target))
      return;
    event.preventDefault();
    leaveSkill();
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
    if (!selected) returnScrollTop = collectionScroller()?.scrollTop ?? null;
    if (isLibrary) lastOpenedId = entry.id;
    selectedId = entry.id;
    if (inspected?.id !== entry.id) inspected = null;
    if (focus) {
      contentTab = pendingContentTab ?? 'instructions';
      pendingContentTab = null;
      const request = inspect(entry, false);
      await tick();
      skillPage?.focus();
      await request;
      return;
    }
    await inspect(entry, quiet);
  }

  function openPackage(item) {
    const entry = packageOf(item);
    if (entry) showSkill(entry);
  }

  function openHistory(entry) {
    pendingContentTab = 'history';
    showSkill(entry);
  }

  // The archived packages, searched for one name.
  function openArchived(name) {
    changeSearch(name);
    navigation.navigate([ARCHIVED_COLLECTION]);
  }

  async function loadInventory() {
    const version = ++inventoryVersion;
    loading = true;
    loadError = '';
    try {
      const result = await skillInventory();
      if (disposed || version !== inventoryVersion) return;
      inventory = Array.isArray(result?.skills) ? result.skills : [];
      archived = Array.isArray(result?.archived) ? result.archived : [];
      agents = Array.isArray(result?.agents) ? result.agents : [];
      projects = Array.isArray(result?.projects) ? result.projects : [];
      staleShared = result?.stale_shared ?? [];
      policyDiagnostics = result?.policy_diagnostics ?? [];
      loaded = true;
      if (scope !== 'directories' && !collection) navigation.replace(['all']);
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
        // The install itself was the step; its package page takes that place.
        navigation.replace([scope, installedEntry.id]);
      } else if (selectedId) {
        const entry = inventory.find((item) => item.id === selectedId);
        if (!entry) navigation.replace([scope]);
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

  function openDirectories() {
    navigation.navigate(['directories']);
  }

  function closeDirectories() {
    navigation.up([
      collections.some((item) => item.key === returnScope)
        ? returnScope
        : 'all',
    ]);
  }

  async function installed(result) {
    showInstall = false;
    navigation.navigate([
      result.scope === actions.GLOBAL_SCOPE ? 'global' : 'all',
    ]);
    await tick();
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

  async function editSkill(entry) {
    showSkill(entry);
    await tick();
    await placeApplied;
    if (selectedId === entry.id) await actions.startEdit(entry);
  }

  const menuActions = {
    open: (entry) => showSkill(entry),
    edit: (entry) => void editSkill(entry),
    copyName: (name) => void actions.copyName(name),
    setDisabled: (entry, disabled) => actions.setDisabled(entry, disabled),
    remove: (entry) => actions.requestDelete(entry),
    restore: (item) => void actions.restoreArchived(item),
    purge: (item) => actions.requestPurge(item),
  };

  function packageOf(item) {
    return inventory.find((entry) => entry.id === item.packageId) ?? null;
  }

  function openMenu(event, value) {
    menu = { ...contextMenuAnchor(event), ...value };
  }

  function openLibraryMenu(entry, event) {
    openMenu(event, libraryRowMenu(entry, menuActions));
  }

  function openArchivedMenu(item, event) {
    openMenu(event, archivedRowMenu(item, menuActions));
  }

  function openAgentMenu(item, event, toggle) {
    openMenu(
      event,
      agentRowMenu(
        item,
        { agentName: collection?.label ?? '', entry: packageOf(item), toggle },
        menuActions,
      ),
    );
  }

  function openProjectMenu(groupId, item, event) {
    const project = scopeProject;
    openMenu(
      event,
      projectRowMenu(
        item,
        {
          projectName: collection?.label ?? '',
          entry: packageOf(item),
          toggle: (on) =>
            actions.updateProjectSkills(project, groupId, [item.name], on),
        },
        menuActions,
      ),
    );
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

<svelte:window onkeydown={handleWindowKeydown} />

<section
  class="skills-view view active"
  aria-labelledby={selected ? 'skill-page-title' : 'skills-title'}
  bind:this={viewElement}
>
  <aside class="skills-nav secondary-pane" aria-label={t('skills.collections')}>
    <SkillCollectionNav {collections} {scope} onSelect={selectCollection} />
  </aside>

  <div class="skills-main">
    <div class="skills-mobile-nav">
      <SkillCollectionNav
        variant="dropdown"
        {collections}
        {scope}
        onSelect={selectCollection}
      />
    </div>

    {#if loadError}
      <Banner variant="error" role="alert"
        >{loadError}<Button variant="secondary" onClick={loadInventory}
          >{t('common.retry')}</Button
        ></Banner
      >
    {/if}

    {#if selected}
      <SkillPage
        bind:this={skillPage}
        entry={selected}
        collectionLabel={collection?.label || t('skills.title')}
        {inspected}
        {inspectLoading}
        {inspectError}
        {contentTab}
        {agents}
        {projects}
        {inventory}
        busy={actions.busy}
        onBack={leaveSkill}
        onRetry={() => openSkill(selected, false)}
        onTab={(next) => (contentTab = next)}
        onEdit={actions.startEdit}
        onDelete={actions.requestDelete}
        onSetDisabled={actions.setDisabled}
        onSetPinned={actions.setPinned}
        onRevert={actions.requestRevert}
        onAgentAccess={actions.updateAgentAccess}
        onShare={actions.setSharing}
        onProjectSkills={actions.updateProjectSkills}
      />
    {/if}

    <!-- The collection page stays mounted behind a package page, so its
         filters and list survive the round trip. -->
    <div class="skills-collection-page" hidden={Boolean(selected)}>
      <header class="view-header skills-header">
        <div class="view-header__intro">
          <h2
            id="skills-title"
            class="view-header__title"
            tabindex="-1"
            bind:this={collectionTitle}
          >
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
            onError={(message) => (directoryError = message)}
          />
        {/if}
      </div>
      {#if scope !== 'directories'}
        <div class="skills-toolbar">
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
              placeholder={isLibrary || isArchive
                ? t('skills.searchLibrary')
                : t('skills.panel.filterPlaceholder')}
              ariaLabel={isLibrary || isArchive
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
        <section
          class="skills-results"
          aria-label={t('skills.results')}
          aria-busy={actions.busy}
          bind:this={resultsElement}
        >
          {#if isLibrary}
            <SkillLibraryList
              bind:this={list}
              entries={visibleSkills}
              total={filtered.length}
              currentId={lastOpenedId}
              {loading}
              {loaded}
              filtersActive={Boolean(searchQuery) || statusFilter !== 'all'}
              emptyTitle={collectionText.empty}
              emptyHelp={collectionText.emptyHelp}
              {agents}
              {projects}
              page={currentPage}
              {pageCount}
              busy={actions.busy}
              onOpen={showSkill}
              onEdit={menuActions.edit}
              onDelete={menuActions.remove}
              onSetDisabled={menuActions.setDisabled}
              onContextMenu={openLibraryMenu}
              onPage={changePage}
              onClearFilters={() => {
                searchQuery = '';
                changeStatus('all');
              }}
            />
          {:else if isArchive}
            <SkillArchiveList
              items={filteredArchived}
              {loading}
              {loaded}
              filtersActive={Boolean(searchQuery)}
              emptyTitle={collectionText.empty}
              emptyHelp={collectionText.emptyHelp}
              {agents}
              busy={actions.busy}
              onMenu={openArchivedMenu}
              onRestore={menuActions.restore}
              onPurge={menuActions.purge}
              onClearFilters={() => changeSearch('')}
            />
          {:else if scopeAgent}
            <div class="skills-panel-scroll">
              <LibrarianSection
                agent={scopeAgent}
                {inventory}
                {archived}
                busy={actions.busy}
                onOpenHistory={openHistory}
                onOpenArchived={openArchived}
                onRevertPass={actions.requestRevertPass}
                {onOpenSession}
                {onToast}
              />
              <AgentSkillsPanel
                agent={scopeAgent}
                access={skillAccessOf(scopeAgent)}
                {inventory}
                {agents}
                {projects}
                query={searchQuery}
                onChange={(next) => actions.updateAgentAccess(scopeAgent, next)}
                onOpen={openPackage}
                onContextMenu={openAgentMenu}
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
                onContextMenu={openProjectMenu}
                emptyTitle={collectionText.empty}
                emptyHelp={collectionText.emptyHelp}
              />
            </div>
          {/if}
        </section>
      {/if}
    </div>
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
<ContextMenu {menu} onClose={() => (menu = null)} />
