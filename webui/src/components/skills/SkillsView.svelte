<script>
  // Collection navigation, bounded results, and inspection of one exact Skill.
  // Runtime policy and write scopes remain server-owned.
  import { onMount, onDestroy, tick } from 'svelte';

  import SkillDirectoryEditor from './SkillDirectoryEditor.svelte';
  import {
    agentDisplayName,
    createSkillDocument,
    filterSkills,
    skillCollections,
    skillSourceLabel,
    skillDiagnosticLines,
    skillStatusLabel,
    skillStatusVariant,
    skillInstructionBody,
    SKILL_PAGE_SIZE,
  } from './skillsView.js';
  import Dropdown from '../Dropdown.svelte';
  import TabList from '../ui/TabList.svelte';
  import MarkdownContent from '../chat/MarkdownContent.svelte';
  import CopyButton from '../ui/CopyButton.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import Modal from '../ui/Modal.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import TextField from '../ui/TextField.svelte';
  import Badge from '../ui/Badge.svelte';
  import {
    createSkill as createSkillRequest,
    deleteSkill as deleteSkillRequest,
    listAgents,
    inspectSkill,
    setSkillDisabled,
    shareSkill,
    skillInventory,
    updateSkill,
  } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';

  const GLOBAL_SCOPE = 'global';
  const noop = () => {};

  let {
    settings = null,
    onSettingsCommit = noop,
    onToast = noop,
    skillsRefreshToken = 0,
  } = $props();

  let agents = $state([]);
  let inventory = $state([]);
  let staleShared = $state([]);
  let loading = $state(true);
  let loadError = $state('');
  let busy = $state(false);

  // View-mode state: group by source (origin) or by agent (owner-centric).
  let scope = $state('all');
  let statusFilter = $state('all');
  let page = $state(0);
  let selectedId = $state(null);
  let inspected = $state(null);
  let inspectLoading = $state(false);
  let inspectError = $state('');
  let contentTab = $state('instructions');
  let detailElement = $state();
  let listElement = $state();
  let agentError = $state('');
  let directoryError = $state('');
  let policyDiagnostics = $state([]);
  let inventoryVersion = 0;
  let inspectVersion = 0;
  let disposed = false;
  onDestroy(() => {
    disposed = true;
    inventoryVersion++;
    inspectVersion++;
  });
  let searchQuery = $state('');

  // Create-modal state: a target scope (global pool or an agent's private home)
  // plus the name/content draft.
  let showCreateModal = $state(false);
  let createScope = $state(GLOBAL_SCOPE);
  let newName = $state('');
  let newDescription = $state('');
  let newContent = $state('');

  // Edit-modal state: which entry (scope + name) is open with which content.
  let editing = $state(null); // { scope, name }
  let editContent = $state('');

  // The skill awaiting delete confirmation (null = dialog closed).
  let deleteTarget = $state(null); // { scope, name }

  // Share-modal state: which entry is being shared and which receivers are
  // selected.
  let shareTarget = $state(null); // { owner_id, name }
  let shareReceivers = $state([]); // selected receiver agent ids

  let showDirectories = $state(false);
  let collections = $derived(skillCollections(inventory, agents, t));
  let collection = $derived(collections.find((item) => item.key === scope));
  let filtered = $derived(
    filterSkills(inventory, searchQuery, scope, statusFilter, agents),
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
  let diagnostics = $derived(selected ? skillDiagnosticLines(selected) : []);
  let statusOptions = $derived([
    { value: 'all', label: t('skills.filter.all', 'All statuses') },
    {
      value: 'attention',
      label: t('skills.filter.attention', 'Needs attention'),
    },
    { value: 'disabled', label: t('skills.status.disabled', 'Disabled') },
    { value: 'available', label: t('skills.status.available', 'Available') },
  ]);
  let contentTabs = $derived([
    { id: 'instructions', label: t('skills.instructions', 'Instructions') },
    { id: 'original', label: t('skills.original', 'Original text') },
  ]);

  function clearSelection() {
    selectedId = null;
    inspected = null;
    inspectError = '';
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
    clearSelection();
  }
  function changeStatus(next) {
    statusFilter = next;
    page = 0;
    clearSelection();
  }
  function changePage(next) {
    page = next;
    clearSelection();
    listElement?.scrollTo?.(0, 0);
  }

  async function openSkill(entry, focus = true) {
    selectedId = entry.id;
    if (inspected?.id !== entry.id) inspected = null;
    inspectError = '';
    inspectLoading = true;
    const version = ++inspectVersion;
    if (focus) {
      contentTab = 'instructions';
      await tick();
      detailElement?.focus();
    }
    try {
      const result = await inspectSkill(entry.id);
      if (!disposed && version === inspectVersion) inspected = result;
    } catch (error) {
      if (!disposed && version === inspectVersion) inspectError = error.message;
    } finally {
      if (!disposed && version === inspectVersion) inspectLoading = false;
    }
  }

  async function closeDetail() {
    const id = selectedId;
    clearSelection();
    await tick();
    listElement?.querySelector(`[data-skill-id="${id}"]`)?.focus();
  }

  let createDisabled = $derived(
    busy || !newName.trim() || !newDescription.trim() || !newContent.trim(),
  );
  let scopeOptions = $derived([
    {
      value: GLOBAL_SCOPE,
      label: t('settings.skills.scopeGlobal', 'Global skills'),
    },
    ...agents.map((agent) => ({
      value: `agent:${agent.id}`,
      label: t('settings.skills.scopeAgent', '{name} (private)', {
        name: agent.name || agent.id,
      }),
    })),
  ]);

  onMount(() => {
    void loadAgents();
    void loadInventory();
  });

  // A skills resource event (our own mutations included) bumps the token;
  // refresh server truth without tearing down open drafts.
  let lastSkillsRefreshToken = 0;
  $effect(() => {
    const token = skillsRefreshToken;
    if (token === lastSkillsRefreshToken) {
      return;
    }
    lastSkillsRefreshToken = token;
    void loadInventory();
  });

  async function loadAgents() {
    agentError = '';
    try {
      const result = await listAgents();
      if (!disposed)
        agents = Array.isArray(result?.agents) ? result.agents : [];
    } catch (error) {
      if (!disposed)
        agentError = `${t('skills.agentsError', 'Agents could not be loaded.')} ${error.message}`;
    }
  }

  async function loadInventory() {
    const version = ++inventoryVersion;
    loading = true;
    loadError = '';
    try {
      const result = await skillInventory();
      if (disposed || version !== inventoryVersion) return;
      inventory = Array.isArray(result?.skills) ? result.skills : [];
      staleShared = result?.stale_shared ?? [];
      policyDiagnostics = result?.policy_diagnostics ?? [];
      if (selectedId) {
        const entry = inventory.find((item) => item.id === selectedId);
        if (!entry) clearSelection();
        else if (!editing) void openSkill(entry, false);
      }
    } catch (error) {
      if (!disposed && version === inventoryVersion)
        loadError = `${t('skills.loadError', 'Skills could not be loaded.')} ${error.message}`;
    } finally {
      if (!disposed && version === inventoryVersion) loading = false;
    }
  }

  function scopeForEntry(entry) {
    return entry.editable_scope;
  }

  function openCreateModal() {
    createScope = scope.startsWith('agent:') ? scope : GLOBAL_SCOPE;
    newName = '';
    newDescription = '';
    newContent = '';
    showCreateModal = true;
  }

  function closeCreateModal() {
    showCreateModal = false;
    newName = '';
    newDescription = '';
    newContent = '';
  }

  async function createSkill() {
    if (createDisabled) {
      return;
    }
    busy = true;
    try {
      await createSkillRequest({
        scope: createScope,
        name: newName.trim(),
        content: createSkillDocument(newName, newDescription, newContent),
      });
      onToast({
        title: t('settings.skills.created', 'Skill created.'),
        variant: 'success',
      });
      closeCreateModal();
      await loadInventory();
    } catch (error) {
      onToast({
        title: `${t('settings.skills.createError', 'Skill could not be created.')} ${error.message}`,
        variant: 'error',
      });
    } finally {
      busy = false;
    }
  }

  function startEdit(entry) {
    if (busy || !entry.editable_scope || inspected?.id !== entry.id) return;
    editing = {
      scope: entry.editable_scope,
      name: entry.name,
      shared: entry.shared,
    };
    editContent = inspected.content;
  }

  function closeEditModal() {
    editing = null;
    editContent = '';
  }

  async function saveEdit() {
    if (busy || !editing) {
      return;
    }
    busy = true;
    try {
      await updateSkill({
        scope: editing.scope,
        name: editing.name,
        content: editContent,
      });
      onToast({
        title: t('settings.skills.saved', 'Skill saved.'),
        variant: 'success',
      });
      closeEditModal();
      await loadInventory();
    } catch (error) {
      onToast({
        title: `${t('settings.skills.contentSaveError', 'Skill could not be saved.')} ${error.message}`,
        variant: 'error',
      });
    } finally {
      busy = false;
    }
  }

  async function toggleDisabled(entry) {
    if (busy) {
      return;
    }
    busy = true;
    try {
      await setSkillDisabled(entry.name, !entry.disabled);
      onToast({
        title: entry.disabled
          ? t('skills.enabledToast', 'Skill "{name}" enabled.', {
              name: entry.name,
            })
          : t('skills.disabledToast', 'Skill "{name}" disabled everywhere.', {
              name: entry.name,
            }),
        variant: 'success',
      });
      await loadInventory();
    } catch (error) {
      onToast({
        title: `${t('skills.toggleError', 'The skill could not be changed.')} ${error.message}`,
        variant: 'error',
      });
    } finally {
      busy = false;
    }
  }

  function openShareModal(entry) {
    if (busy || !entry.owner_id) {
      return;
    }
    shareTarget = { owner_id: entry.owner_id, name: entry.name };
    shareReceivers = Array.isArray(entry.shared_with)
      ? [...entry.shared_with]
      : [];
  }

  function closeShareModal() {
    shareTarget = null;
    shareReceivers = [];
  }

  function toggleReceiver(agentId) {
    if (shareReceivers.includes(agentId)) {
      shareReceivers = shareReceivers.filter((id) => id !== agentId);
    } else {
      shareReceivers = [...shareReceivers, agentId];
    }
  }

  let shareableAgents = $derived(
    agents.filter((agent) => agent.id !== shareTarget?.owner_id),
  );
  let shareSaveDisabled = $derived(busy || Boolean(agentError));

  async function saveShare() {
    if (busy || !shareTarget || agentError) {
      return;
    }
    busy = true;
    try {
      await shareSkill(
        shareTarget.owner_id,
        shareTarget.name,
        shareReceivers.length > 0,
        shareReceivers,
      );
      onToast({
        title: t('skills.sharedToast', 'Skill shared with {count} agents.', {
          count: shareReceivers.length,
        }),
        variant: 'success',
      });
      closeShareModal();
      await loadInventory();
    } catch (error) {
      onToast({
        title: `${t('skills.shareError', 'Sharing could not be changed.')} ${error.message}`,
        variant: 'error',
      });
    } finally {
      busy = false;
    }
  }

  function requestDelete(entry) {
    if (busy) {
      return;
    }
    deleteTarget = { scope: scopeForEntry(entry), name: entry.name };
  }

  function cancelDelete() {
    deleteTarget = null;
  }

  async function confirmDelete() {
    const target = deleteTarget;
    deleteTarget = null;
    if (!target || busy) {
      return;
    }
    busy = true;
    try {
      await deleteSkillRequest(target.scope, target.name);
      onToast({
        title: t('settings.skills.deleted', 'Skill deleted.'),
        variant: 'success',
      });
      if (editing?.name === target.name && editing?.scope === target.scope) {
        closeEditModal();
      }
      await loadInventory();
    } catch (error) {
      onToast({
        title: `${t('settings.skills.deleteError', 'Skill could not be deleted.')} ${error.message}`,
        variant: 'error',
      });
    } finally {
      busy = false;
    }
  }
</script>

<section class="skills-view view active" aria-labelledby="skills-title">
  <aside
    class="skills-nav secondary-pane"
    aria-label={t('skills.collections', 'Skill collections')}
  >
    <div class="skills-nav-title">{t('skills.library', 'Library')}</div>
    <nav class="secondary-list">
      {#each ['library', 'agents', 'sources', 'projects'] as section (section)}
        {#if collections.some((item) => item.section === section)}
          {#if section !== 'library'}
            <h3 class="skills-nav-label">{t(`skills.section.${section}`)}</h3>
          {/if}
          {#each collections.filter((item) => item.section === section) as item (item.key)}
            <button
              type="button"
              class="secondary-list__item skills-collection"
              class:active={scope === item.key}
              aria-current={scope === item.key ? 'page' : undefined}
              onclick={() => changeScope(item.key)}
            >
              <span class="skills-collection-name" use:tooltip={item.label}
                >{item.label}</span
              >
              <span class="skills-count">{item.count}</span>
            </button>
          {/each}
        {/if}
      {/each}
    </nav>
    <div class="skills-nav-footer">
      <Button
        variant="secondary"
        onClick={() => {
          showDirectories = true;
          changeScope('directories');
        }}
      >
        {t('skills.locations', 'Skill locations')}
      </Button>
    </div>
  </aside>

  <div class="skills-main">
    <div class="skills-mobile-nav">
      <Dropdown
        value={scope}
        options={[
          ...collections.map((item) => ({
            value: item.key,
            label: `${item.label} (${item.count})`,
          })),
          { value: 'directories', label: t('skills.locations') },
        ]}
        ariaLabel={t('skills.collections')}
        onValueChange={(next) => {
          if (next === 'directories') showDirectories = true;
          changeScope(next);
        }}
      />
    </div>
    <header class="skills-header">
      <div>
        <h2 id="skills-title">
          {scope === 'directories'
            ? t('skills.locations')
            : collection?.label || t('skills.title')}
        </h2>
        <p>
          {scope === 'directories'
            ? t('skills.locationsSubtitle')
            : scope.startsWith('agent:')
              ? t('skills.agentSubtitle', '', { name: collection?.label })
              : scope === 'shared'
                ? t('skills.sharedSubtitle')
                : t('skills.librarySubtitle')}
        </p>
      </div>
      <div class="skills-header-actions">
        <Button
          variant="secondary"
          disabled={loading}
          onClick={() => {
            void loadAgents();
            void loadInventory();
          }}>{t('skills.refresh')}</Button
        >
        {#if scope !== 'directories'}
          <Button variant="primary" disabled={busy} onClick={openCreateModal}
            ><span aria-hidden="true">+</span>
            {t('settings.skills.newSkill')}</Button
          >
        {/if}
      </div>
    </header>

    {#if loadError}
      <Banner variant="error" role="alert"
        >{loadError}<Button variant="secondary" onClick={loadInventory}
          >{t('common.retry', 'Retry')}</Button
        ></Banner
      >
    {/if}
    {#if agentError}
      <Banner variant="warn" role="alert"
        >{agentError}<Button variant="secondary" onClick={loadAgents}
          >{t('common.retry', 'Retry')}</Button
        ></Banner
      >
    {/if}
    {#if staleShared.length || policyDiagnostics.length}
      <details class="skills-notice">
        <summary
          >{t('skills.policyAttention', '', {
            count: staleShared.length + policyDiagnostics.length,
          })}</summary
        >
        <p>{t('skills.staleShared', '', { count: staleShared.length })}</p>
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
          {settings}
          onCommit={onSettingsCommit}
          {onToast}
          onError={(message) => (directoryError = message)}
        />
      {/if}
    </div>
    {#if scope !== 'directories'}
      <div class="skills-toolbar" class:skills-mobile-hidden={selected}>
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
            placeholder={t('skills.searchLibrary')}
            ariaLabel={t('skills.searchLibrary')}
          />
        </div>
        <Dropdown
          value={statusFilter}
          options={statusOptions}
          ariaLabel={t('skills.filter.label')}
          onValueChange={changeStatus}
        />
      </div>
      <div class="skills-workspace" class:skills-workspace--selected={selected}>
        <section
          class="skills-results"
          class:skills-mobile-hidden={selected}
          aria-label={t('skills.results')}
        >
          <div class="skills-results-meta" aria-live="polite">
            <span
              >{t('skills.resultCount', '', { count: filtered.length })}</span
            >
            {#if loading}<span>{t('skills.refreshing')}</span>{/if}
            {#if searchQuery || statusFilter !== 'all'}
              <Button
                variant="secondary"
                onClick={() => {
                  searchQuery = '';
                  changeStatus('all');
                }}>{t('skills.clearFilters')}</Button
              >
            {/if}
          </div>
          <div class="skills-list" bind:this={listElement}>
            {#if loading && !inventory.length}
              <Banner variant="neutral">{t('skills.loading')}</Banner>
            {:else if !filtered.length}
              <EmptyState
                title={t(
                  searchQuery || statusFilter !== 'all'
                    ? 'skills.noMatches'
                    : 'skills.noCollectionSkills',
                )}
                description={t(
                  searchQuery || statusFilter !== 'all'
                    ? 'skills.noMatchesHelp'
                    : 'skills.noCollectionSkillsHelp',
                )}
              />
            {:else}
              {#each visibleSkills as entry (entry.id)}
                <button
                  type="button"
                  class="skills-row"
                  class:skills-row--selected={selectedId === entry.id}
                  class:skills-row--disabled={entry.disabled}
                  data-skill-id={entry.id}
                  aria-pressed={selectedId === entry.id}
                  onclick={() => openSkill(entry)}
                >
                  <span class="skills-row-copy">
                    <span class="skills-row-name">{entry.name}</span>
                    <span class="skills-row-description"
                      >{entry.description || t('skills.noDescription')}</span
                    >
                    <span class="skills-row-source"
                      >{skillSourceLabel(
                        entry,
                        t,
                        agents,
                      )}{#if entry.shared}<span aria-hidden="true"> · </span>{t(
                          'skills.sharedBadge',
                        )}{/if}</span
                    >
                  </span>
                  <span class="skills-row-end">
                    {#if entry.status !== 'available'}<StatusChip
                        variant={skillStatusVariant(entry)}
                        >{skillStatusLabel(entry, t)}</StatusChip
                      >
                    {:else if skillDiagnosticLines(entry).length}<Badge
                        variant="warn">{t('skills.notes')}</Badge
                      >{/if}
                    <span aria-hidden="true" class="skills-row-arrow">›</span>
                  </span>
                </button>
              {/each}
            {/if}
          </div>
          {#if pageCount > 1}
            <div class="skills-pagination">
              <Button
                variant="secondary"
                disabled={currentPage === 0}
                onClick={() => changePage(currentPage - 1)}
                ariaLabel={t('skills.previousPage')}>←</Button
              >
              <span
                >{t('skills.page', '', {
                  page: currentPage + 1,
                  pages: pageCount,
                })}</span
              >
              <Button
                variant="secondary"
                disabled={currentPage + 1 === pageCount}
                onClick={() => changePage(currentPage + 1)}
                ariaLabel={t('skills.nextPage')}>→</Button
              >
            </div>
          {/if}
        </section>
        {#if selected}
          <section
            class="skills-detail"
            tabindex="-1"
            bind:this={detailElement}
            aria-labelledby="skill-detail-name"
          >
            <div class="skills-detail-top">
              <Button
                variant="secondary"
                ariaLabel={t('skills.backToList')}
                onClick={closeDetail}>← {t('skills.backToList')}</Button
              >
              <StatusChip variant={skillStatusVariant(selected)}
                >{skillStatusLabel(selected, t)}</StatusChip
              >
            </div>
            <div class="skills-detail-scroll">
              <header class="skills-detail-header">
                <p class="skills-detail-source">
                  {skillSourceLabel(selected, t, agents)}{#if selected.owner_id}
                    · {t('skills.ownedSkill')}{/if}
                </p>
                <h3 id="skill-detail-name">{selected.name}</h3>
                <p class="skills-description">
                  {selected.description || t('skills.noDescription')}
                </p>
                <div class="skills-detail-actions">
                  {#if selected.editable_scope}<Button
                      variant="primary"
                      disabled={busy ||
                        inspectLoading ||
                        inspected?.id !== selected.id}
                      onClick={() => startEdit(selected)}
                      >{t('skills.editInstructions')}</Button
                    >{/if}
                  {#if selected.owner_id}<Button
                      variant="secondary"
                      disabled={busy || Boolean(agentError)}
                      onClick={() => openShareModal(selected)}
                      >{t('skills.sharing')}</Button
                    >{/if}
                  {#if !selected.editable_scope}<Badge
                      >{t('skills.readOnly')}</Badge
                    >{/if}
                </div>
              </header>
              <div class="skills-access">
                <h4>{t('skills.access')}</h4>
                {#if selected.owner_id}
                  <p>
                    {t('skills.ownerAccess', '', {
                      name: agentDisplayName(selected.owner_id, agents),
                    })}
                  </p>
                  <p>
                    {selected.shared
                      ? t('skills.receivers', '', {
                          names: selected.shared_with
                            .map((id) => agentDisplayName(id, agents))
                            .join(', '),
                        })
                      : t('skills.privateAccess')}
                  </p>
                  {#if selected.shared}<p class="skills-secondary">
                      {t('skills.sharedAccessHelp')}
                    </p>{/if}
                {:else}<p>
                    {t(
                      selected.origin?.startsWith('project:')
                        ? 'skills.projectAccess'
                        : 'skills.poolAccess',
                    )}
                  </p>{/if}
                {#if selected.disabled}<p>{t('skills.disabledEffect')}</p>{/if}
              </div>
              {#if diagnostics.length}
                <details
                  class="skills-diagnostics"
                  open={['invalid', 'unavailable'].includes(selected.status)}
                >
                  <summary
                    >{t('skills.diagnostics', '', {
                      count: diagnostics.length,
                    })}</summary
                  >
                  <ul>
                    {#each diagnostics as line, index (index)}<li>
                        {line}
                      </li>{/each}
                  </ul>
                </details>
              {/if}
              <div class="skills-content-head">
                <TabList
                  items={contentTabs}
                  value={contentTab}
                  idPrefix="skill-content"
                  ariaLabel={t('skills.contentView')}
                  onChange={(next) => (contentTab = next)}
                />
                {#if inspected}<CopyButton
                    text={inspected.content}
                    label={t('skills.copyContent')}
                  />{/if}
              </div>
              <div
                class="skills-content"
                role="tabpanel"
                id={`skill-content-panel-${contentTab}`}
                aria-labelledby={`skill-content-tab-${contentTab}`}
                tabindex="0"
              >
                {#if inspectLoading}<Banner variant="neutral"
                    >{t('skills.loadingContent')}</Banner
                  >
                {:else if inspectError}<Banner variant="error" role="alert"
                    >{inspectError}<Button
                      variant="secondary"
                      onClick={() => openSkill(selected, false)}
                      >{t('common.retry', 'Retry')}</Button
                    ></Banner
                  >
                {:else if inspected}
                  {#if contentTab === 'original'}<pre>{inspected.content}</pre>
                  {:else}<MarkdownContent
                      class="msg-markdown"
                      source={skillInstructionBody(inspected.content)}
                    />{/if}
                {/if}
              </div>
              <details class="skills-management">
                <summary>{t('skills.availabilityAndRemoval')}</summary>
                <p>{t('skills.disableHelp')}</p>
                <Button
                  variant="secondary"
                  disabled={busy}
                  onClick={() => toggleDisabled(selected)}
                  >{t(
                    selected.disabled
                      ? 'skills.enableEverywhere'
                      : 'skills.disableEverywhere',
                  )}</Button
                >
                {#if selected.editable_scope}
                  <div class="skills-delete">
                    <p>{t('skills.deleteHelp')}</p>
                    <Button
                      variant="danger"
                      disabled={busy}
                      onClick={() => requestDelete(selected)}
                      >{t('settings.skills.deleteConfirmTitle')}</Button
                    >
                  </div>
                {/if}
              </details>
            </div>
          </section>
        {/if}
      </div>
    {/if}
  </div>
</section>

{#if showCreateModal}
  <Modal
    title={t('settings.skills.newSkill', 'New skill')}
    class="skills-editor-modal"
    labelledById="skill-create-modal-title"
    closeDisabled={busy}
    onClose={closeCreateModal}
  >
    {#snippet body()}
      <div class="skills-modal-body">
        <div class="skills-field">
          <label class="skills-field-label" for="create-scope">
            {t('skills.createScopeLabel', 'Create in')}
          </label>
          <Dropdown
            id="create-scope"
            value={createScope}
            options={scopeOptions}
            ariaLabel={t('skills.createScopeLabel')}
            onValueChange={(value) => (createScope = value)}
          />
          <p class="skills-secondary">
            {t(
              createScope === 'global'
                ? 'skills.createGlobalHelp'
                : 'skills.createPrivateHelp',
            )}
          </p>
        </div>
        <div class="skills-field">
          <label class="skills-field-label" for="new-skill-name">
            {t('settings.skills.nameLabel', 'Skill name')}
          </label>
          <TextField
            id="new-skill-name"
            value={newName}
            onInput={(next) => (newName = next)}
            placeholder={t('settings.skills.namePlaceholder', 'skill-name')}
          />
        </div>
        <div class="skills-field">
          <label class="skills-field-label" for="new-skill-description">
            {t('skills.descriptionLabel')}
            <InfoHint text={t('skills.descriptionHelp')} />
          </label>
          <TextField
            id="new-skill-description"
            value={newDescription}
            onInput={(value) => (newDescription = value)}
            placeholder={t('skills.descriptionPlaceholder')}
          />
        </div>
        <div class="skills-field">
          <label class="skills-field-label" for="new-skill-content">
            {t('skills.instructions')}
          </label>
          <TextArea
            id="new-skill-content"
            rows="12"
            value={newContent}
            onInput={(value) => (newContent = value)}
            placeholder={t('skills.instructionsPlaceholder')}
          />
        </div>
      </div>
    {/snippet}
    {#snippet footer()}
      <Button variant="secondary" disabled={busy} onClick={closeCreateModal}>
        {t('common.cancel', 'Cancel')}
      </Button>
      <Button variant="primary" disabled={createDisabled} onClick={createSkill}>
        {t('settings.skills.create', 'Create skill')}
      </Button>
    {/snippet}
  </Modal>
{/if}

{#if editing}
  <Modal
    title={t('skills.editTitle', 'Edit {name}', { name: editing.name })}
    class="skills-editor-modal"
    labelledById="skill-edit-modal-title"
    closeDisabled={busy}
    onClose={closeEditModal}
  >
    {#snippet body()}
      <div class="skills-modal-body">
        {#if editing.shared}<Banner variant="info"
            >{t('skills.editSharedHelp')}</Banner
          >{/if}
        <div class="skills-field">
          <label
            class="skills-field-label"
            for={`skill-content-${editing.name}`}
          >
            {t('settings.skills.contentLabel', 'SKILL.md content')}
          </label>
          <TextArea
            id={`skill-content-${editing.name}`}
            code
            rows="16"
            value={editContent}
            onInput={(value) => (editContent = value)}
          />
        </div>
      </div>
    {/snippet}
    {#snippet footer()}
      <Button variant="secondary" disabled={busy} onClick={closeEditModal}>
        {t('common.cancel', 'Cancel')}
      </Button>
      <Button variant="primary" disabled={busy} onClick={saveEdit}>
        {busy ? t('common.saving', 'Saving…') : t('common.save', 'Save')}
      </Button>
    {/snippet}
  </Modal>
{/if}

{#if shareTarget}
  <Modal
    title={t('skills.shareTitle', 'Share {name}', { name: shareTarget.name })}
    labelledById="skill-share-modal-title"
    closeDisabled={busy}
    onClose={closeShareModal}
  >
    {#snippet body()}
      <div class="skills-modal-body">
        {#if agentError}<Banner variant="warn">{agentError}</Banner>{/if}
        <p class="skills-share-desc">
          {t(
            'skills.shareExplanation',
            'Select which agents should have access to this skill. They can activate and co-maintain it.',
          )}
        </p>
        {#if shareableAgents.length === 0}
          <EmptyState
            density="compact"
            description={t(
              'skills.noOtherAgents',
              'No other identity agents exist to share with.',
            )}
          />
        {:else}
          <div class="skills-share-list">
            {#each shareableAgents as agent (agent.id)}
              <button
                type="button"
                class="skills-share-option"
                class:skills-share-option--selected={shareReceivers.includes(
                  agent.id,
                )}
                role="switch"
                aria-checked={shareReceivers.includes(agent.id)}
                aria-label={t('skills.toggleReceiver', 'Share with {name}', {
                  name: agent.name || agent.id,
                })}
                onclick={() => toggleReceiver(agent.id)}
              >
                <span class="skills-receiver-check" aria-hidden="true"
                  >{shareReceivers.includes(agent.id) ? '✓' : ''}</span
                >
                <span class="skills-share-agent-name"
                  >{agent.name || agent.id}</span
                >
                {#if agent.name && agent.name !== agent.id}
                  <span class="skills-share-agent-id">{agent.id}</span>
                {/if}
              </button>
            {/each}
          </div>
        {/if}
      </div>
    {/snippet}
    {#snippet footer()}
      <Button variant="secondary" disabled={busy} onClick={closeShareModal}>
        {t('common.cancel', 'Cancel')}
      </Button>
      <Button
        variant="primary"
        disabled={shareSaveDisabled}
        onClick={saveShare}
      >
        {t('skills.saveShare', 'Save')}
      </Button>
    {/snippet}
  </Modal>
{/if}

{#if deleteTarget}
  <ConfirmDialog
    title={t('settings.skills.deleteConfirmTitle', 'Delete skill')}
    body={t(
      'skills.deletePackageConfirm',
      'Delete skill "{name}" permanently? The skill file is removed from disk.',
      { name: deleteTarget.name },
    )}
    confirmLabel={t('common.delete', 'Delete')}
    onConfirm={confirmDelete}
    onCancel={cancelDelete}
  />
{/if}

<style>
  .skills-view {
    display: flex;
    flex-direction: row;
    height: 100%;
    min-height: 0;
    overflow: hidden;
  }
  .skills-nav-title,
  .skills-nav-label {
    font: 500 var(--fs-mono-xs)/1.4 var(--font-mono);
    text-transform: uppercase;
    letter-spacing: 0.07em;
    color: var(--text-med);
  }
  .skills-nav-title {
    padding: 20px 24px 8px;
  }
  .skills-nav nav {
    overflow-y: auto;
    min-height: 0;
    flex: 1;
  }
  .skills-nav-label {
    margin: 24px 12px 8px;
  }
  .skills-collection {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 12px;
    color: var(--text-med);
    text-align: left;
    cursor: pointer;
    font: 400 var(--fs-body-sm)/1.4 var(--font-ui);
  }
  .skills-collection.active {
    color: var(--text-hi);
  }
  .skills-collection-name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .skills-count {
    font: 400 var(--fs-mono-xs)/1.4 var(--font-mono);
  }
  .skills-nav-footer {
    padding: 14px 24px;
    border-top: 1px solid var(--border);
  }
  .skills-main {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-width: 0;
    min-height: 0;
    gap: 14px;
    padding: 24px 28px 20px;
    max-width: calc(var(--content-max-wide) + 56px);
    margin: 0 auto;
  }
  .skills-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 20px;
    flex-shrink: 0;
  }
  .skills-header h2 {
    margin: 0;
    color: var(--text-hi);
    font: 600 var(--fs-heading-lg)/1.3 var(--font-ui);
    overflow-wrap: anywhere;
  }
  .skills-header p {
    margin: 6px 0 0;
    max-width: 62ch;
    color: var(--text-med);
    font: 400 var(--fs-body-sm)/1.5 var(--font-ui);
  }
  .skills-header-actions {
    display: flex;
    gap: 8px;
    flex-shrink: 0;
  }
  .skills-toolbar {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .skills-search {
    position: relative;
    min-width: 0;
    flex: 1;
  }
  .skills-search svg {
    position: absolute;
    left: 12px;
    top: 50%;
    transform: translateY(-50%);
    color: var(--text-med);
    pointer-events: none;
  }
  .skills-search :global(input) {
    width: 100%;
    padding-left: 36px;
    font-family: var(--font-ui);
  }
  .skills-toolbar :global(.dropdown) {
    min-width: 160px;
  }
  .skills-workspace {
    display: flex;
    flex: 1;
    min-height: 0;
    min-width: 0;
    border-top: 1px solid var(--border);
  }
  .skills-results {
    display: flex;
    flex: 1;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
  }
  .skills-results-meta {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    min-height: 44px;
    color: var(--text-med);
    font: 400 var(--fs-body-sm)/1.4 var(--font-ui);
    padding-right: 12px;
  }
  .skills-list {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    overscroll-behavior: contain;
  }
  .skills-row {
    display: flex;
    align-items: center;
    gap: 14px;
    width: 100%;
    padding: 14px 12px;
    text-align: left;
    border: 0;
    border-bottom: 1px solid var(--border);
    border-left: 2px solid transparent;
    background: transparent;
    cursor: pointer;
  }
  .skills-row:hover {
    background: var(--surface);
  }
  .skills-row--selected {
    background: var(--accent-06);
    border-left-color: var(--accent);
  }
  .skills-row:focus-visible {
    outline: 1px solid var(--accent);
    outline-offset: -2px;
    background: var(--surface);
  }
  .skills-row-copy {
    display: grid;
    gap: 4px;
    flex: 1;
    min-width: 0;
  }
  .skills-row-name {
    font: 500 var(--fs-mono-body)/1.5 var(--font-mono);
    color: var(--text-hi);
    overflow-wrap: anywhere;
  }
  .skills-row-description {
    font: 400 var(--fs-body-sm)/1.5 var(--font-ui);
    color: var(--text-med);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .skills-row-source {
    font: 400 var(--fs-body-sm)/1.4 var(--font-ui);
    color: var(--text-med);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .skills-row--disabled {
    border-left-style: dashed;
  }
  .skills-row-end {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-shrink: 0;
  }
  .skills-row-arrow {
    color: var(--text-med);
    font-size: var(--fs-heading-md);
  }
  .skills-pagination {
    display: flex;
    flex-shrink: 0;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 12px 12px 0 0;
    border-top: 1px solid var(--border);
    color: var(--text-med);
    font: 400 var(--fs-body-sm)/1.4 var(--font-ui);
  }
  .skills-workspace--selected .skills-results {
    flex: 0 0 40%;
  }
  .skills-detail {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-width: 0;
    min-height: 0;
    margin-left: 20px;
    border-left: 1px solid var(--border);
    padding-left: 20px;
    outline: none;
  }
  .skills-detail:focus-visible {
    box-shadow: inset 2px 0 var(--accent-40);
  }
  .skills-detail-top {
    min-height: 44px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    padding: 8px 0;
  }
  .skills-detail-scroll {
    overflow-y: auto;
    min-height: 0;
    flex: 1;
    overscroll-behavior: contain;
    padding: 14px 12px 20px 0;
  }
  .skills-detail-header h3 {
    margin: 8px 0;
    font: 500 var(--fs-heading-md)/1.4 var(--font-mono);
    color: var(--text-hi);
    overflow-wrap: anywhere;
  }
  .skills-detail-source {
    margin: 0;
    font: 400 var(--fs-body-sm)/1.5 var(--font-ui);
    color: var(--text-med);
    overflow-wrap: anywhere;
  }
  .skills-description {
    color: var(--text-hi);
    margin: 0;
    font: 400 var(--fs-body-lg)/1.6 var(--font-ui);
    overflow-wrap: anywhere;
  }
  .skills-detail-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    margin-top: 20px;
  }
  .skills-access {
    margin: 24px 0;
    padding-left: 14px;
    border-left: 2px solid var(--border-2);
  }
  .skills-access h4 {
    margin: 0 0 8px;
    font: 500 var(--fs-body-md)/1.4 var(--font-ui);
    color: var(--text-hi);
  }
  .skills-access p,
  .skills-management p,
  .skills-notice p {
    margin: 6px 0;
    font: 400 var(--fs-body-sm)/1.6 var(--font-ui);
    color: var(--text-hi);
    overflow-wrap: anywhere;
  }
  .skills-secondary,
  .skills-access .skills-secondary {
    margin: 0;
    color: var(--text-med);
    font: 400 var(--fs-body-sm)/1.5 var(--font-ui);
  }
  .skills-content-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
  }
  .skills-content {
    padding: 16px 0;
    overflow-wrap: anywhere;
    font: 400 var(--fs-body-lg)/1.65 var(--font-ui);
  }
  .skills-content :global(.msg-markdown) {
    color: var(--text-hi);
  }
  .skills-content pre {
    margin: 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font: 400 var(--fs-mono-body)/1.6 var(--font-mono);
    color: var(--text-hi);
  }
  .skills-content :global(pre) {
    max-width: 100%;
    overflow-x: auto;
  }
  .skills-management {
    margin-top: 24px;
    padding-top: 14px;
    border-top: 1px solid var(--border);
  }
  .skills-management summary,
  .skills-diagnostics summary,
  .skills-notice summary {
    cursor: pointer;
    color: var(--text-med);
    font: 500 var(--fs-body-sm)/1.5 var(--font-ui);
    padding: 8px 0;
  }
  .skills-management p {
    margin: 12px 0;
  }
  .skills-delete {
    border-top: 1px solid var(--border);
    margin-top: 20px;
    padding-top: 8px;
  }
  .skills-diagnostics {
    margin-bottom: 20px;
  }
  .skills-diagnostics ul,
  .skills-notice ul {
    padding-left: 20px;
    color: var(--text-med);
    font: 400 var(--fs-body-sm)/1.6 var(--font-ui);
    overflow-wrap: anywhere;
  }
  .skills-notice {
    border-bottom: 1px solid var(--border);
  }
  .skills-directories {
    overflow-y: auto;
  }
  .skills-mobile-nav {
    display: none;
  }
  :global(.skills-editor-modal.modal) {
    width: 760px;
  }
  .skills-modal-body {
    padding: 20px;
    max-height: calc(100dvh - 160px);
    overflow-y: auto;
  }
  .skills-modal-body,
  .skills-field {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .skills-field {
    gap: 8px;
  }
  .skills-field-label {
    display: flex;
    align-items: center;
    gap: 8px;
    font: 500 var(--fs-body-sm)/1.4 var(--font-ui);
    color: var(--text-hi);
  }
  .skills-share-desc {
    margin: 0;
    font: 400 var(--fs-body-md)/1.6 var(--font-ui);
    color: var(--text-hi);
  }
  .skills-share-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
    max-height: 320px;
    overflow-y: auto;
  }
  .skills-share-option {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: var(--surface);
    color: var(--text-hi);
    cursor: pointer;
    text-align: left;
  }
  .skills-share-option--selected {
    background: var(--accent-06);
    border-color: var(--accent-30);
  }
  .skills-share-option:focus-visible {
    outline: 1px solid var(--accent);
  }
  .skills-share-agent-name {
    flex: 1;
    font: 400 var(--fs-body-md)/1.4 var(--font-ui);
  }
  .skills-share-agent-id {
    font: 400 var(--fs-mono-xs)/1.4 var(--font-mono);
    color: var(--text-med);
  }
  .skills-receiver-check {
    width: 18px;
    height: 18px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-sm);
    color: var(--accent);
    text-align: center;
    line-height: 16px;
  }
  @media (max-width: 1280px) {
    .skills-main {
      padding: 20px;
    }
    .skills-header {
      flex-wrap: wrap;
      gap: 12px;
    }
    .skills-detail {
      margin-left: 14px;
      padding-left: 14px;
    }
  }
  @media (max-width: 960px) {
    .skills-nav {
      display: none;
    }
    .skills-mobile-nav {
      display: block;
    }
    .skills-mobile-hidden {
      display: none;
    }
    .skills-detail {
      margin: 0;
      padding: 0;
      border: 0;
    }
    .skills-header {
      flex-wrap: nowrap;
    }
  }
  @media (max-width: 640px) {
    .skills-main {
      padding: 14px;
      gap: 12px;
    }
    .skills-header {
      flex-wrap: wrap;
      gap: 12px;
    }
    .skills-header p {
      display: none;
    }
    .skills-header-actions {
      width: 100%;
      justify-content: space-between;
    }
    .skills-toolbar {
      flex-wrap: wrap;
    }
    .skills-search {
      flex-basis: 100%;
    }
    .skills-row {
      min-height: 76px;
    }
    .skills-view :global(.btn-secondary),
    .skills-view :global(.btn-tertiary),
    .skills-view :global(.btn-primary) {
      min-height: 40px;
    }
    .skills-detail-scroll {
      padding-right: 0;
    }
    .skills-management summary,
    .skills-diagnostics summary {
      min-height: 40px;
    }
  }
</style>
