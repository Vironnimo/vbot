<script>
  import { t } from '$lib/i18n.js';
  import Button from '../ui/Button.svelte';
  import { tooltip } from '$lib/tooltip.js';
  import Toggle from '../ui/Toggle.svelte';
  import Dropdown from '../Dropdown.svelte';
  import Banner from '../ui/Banner.svelte';
  import SkillDirectoryEditor from './SkillDirectoryEditor.svelte';
  import TextField from '../ui/TextField.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import {
    skillStatusVariant,
    skillStatusLabel,
    skillDiagnosticLines,
    skillSourceLabel,
    agentDisplayName,
    skillInstructionBody,
    filterSkills,
    skillCollections,
    SKILL_PAGE_SIZE,
  } from './skillsView.js';
  import Badge from '../ui/Badge.svelte';
  import TabList from '../ui/TabList.svelte';
  import CopyButton from '../ui/CopyButton.svelte';
  import MarkdownContent from '../chat/MarkdownContent.svelte';
  import Modal from '../ui/Modal.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import { onMount, onDestroy, tick } from 'svelte';
  import { listAgents, inspectSkill, skillInventory } from '$lib/api.js';
  import { createSkillActions } from './actions.svelte.js';
  import './skills.css';

  // Collection navigation, bounded results, and inspection of one exact Skill.
  // Runtime policy and write scopes remain server-owned.

  const noop = () => {};

  let {
    settings = null,
    onSettingsCommit = noop,
    onToast = noop,
    skillsRefreshToken = 0,
  } = $props();
  const actions = createSkillActions({
    get agents() {
      return agents;
    },
    get scope() {
      return scope;
    },
    get onToast() {
      return onToast;
    },
    get loadInventory() {
      return loadInventory;
    },
    get inspected() {
      return inspected;
    },
    get agentError() {
      return agentError;
    },
  });

  let agents = $state([]);
  let inventory = $state([]);
  let staleShared = $state([]);
  let loading = $state(true);
  let loadError = $state('');

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

  // selected receiver agent ids

  let showDirectories = $state(false);
  let directoryEditor = $state();
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
        else if (!actions.editing) void openSkill(entry, false);
      }
    } catch (error) {
      if (!disposed && version === inventoryVersion)
        loadError = `${t('skills.loadError', 'Skills could not be loaded.')} ${error.message}`;
    } finally {
      if (!disposed && version === inventoryVersion) loading = false;
    }
  }

  async function openDirectories(focusAdd = false) {
    actions.createScope = scope.startsWith('agent:')
      ? scope
      : actions.GLOBAL_SCOPE;
    showDirectories = true;
    changeScope('directories');
    if (focusAdd) {
      await tick();
      directoryEditor?.focusNewDirectory();
    }
  }
</script>

{#snippet skillActions(entry)}
  <div
    class="skills-actions"
    role="group"
    aria-label={t('skills.actionsFor', '', { name: entry.name })}
  >
    {#if entry.owner_id}
      <Button
        variant="tertiary"
        icon
        disabled={actions.busy || Boolean(agentError)}
        ariaLabel={t('skills.shareNamed', '', { name: entry.name })}
        tooltip={t('skills.sharing')}
        onClick={() => actions.openShareModal(entry)}
      >
        <svg
          width="16"
          height="16"
          viewBox="0 0 16 16"
          fill="none"
          stroke="currentColor"
          aria-hidden="true"
          ><circle cx="4" cy="8" r="2" /><circle cx="12" cy="3" r="2" /><circle
            cx="12"
            cy="13"
            r="2"
          /><path d="m6 7 4-3M6 9l4 3" /></svg
        >
      </Button>
    {/if}
    {#if entry.editable_scope}
      <Button
        variant="danger"
        icon
        disabled={actions.busy}
        ariaLabel={t('skills.deleteNamed', '', { name: entry.name })}
        tooltip={t('common.delete')}
        onClick={() => actions.requestDelete(entry)}
      >
        <svg
          width="16"
          height="16"
          viewBox="0 0 16 16"
          fill="none"
          stroke="currentColor"
          aria-hidden="true"
          ><path d="M2 4h12M6 4V2h4v2M4 4l1 10h6l1-10M7 6v6M9 6v6" /></svg
        >
      </Button>
    {/if}
    <span class="skills-enable" use:tooltip={t('skills.disableHelp')}>
      <Toggle
        checked={!entry.disabled}
        disabled={actions.busy}
        ariaLabel={t('skills.enabledNamed', '', { name: entry.name })}
        onChange={() => actions.toggleDisabled(entry)}
      />
    </span>
  </div>
{/snippet}

<section class="skills-view view active" aria-labelledby="skills-title">
  <aside
    class="skills-nav secondary-pane"
    aria-label={t('skills.collections', 'Skill collections')}
  >
    <nav class="secondary-list">
      {#each ['library', 'agents', 'projects'] as section (section)}
        {#if collections.some((item) => item.section === section)}
          <h3 class="skills-nav-label">
            {section === 'library'
              ? t('skills.library')
              : t(`skills.section.${section}`)}
          </h3>
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
          {#if section === 'library'}
            <button
              type="button"
              class="secondary-list__item skills-collection"
              class:active={scope === 'directories'}
              aria-current={scope === 'directories' ? 'page' : undefined}
              onclick={() => openDirectories()}
            >
              <span class="skills-collection-name">{t('skills.locations')}</span
              >
              <svg
                width="16"
                height="16"
                viewBox="0 0 16 16"
                fill="none"
                stroke="currentColor"
                aria-hidden="true"><path d="M2 4V3h4l2 2h6v8H2V4Z" /></svg
              >
            </button>
          {/if}
        {/if}
      {/each}
    </nav>
  </aside>

  <div class="skills-main">
    <div class="skills-mobile-nav">
      <Dropdown
        value={scope}
        options={[
          ...collections.slice(0, 4).map((item) => ({
            value: item.key,
            label: `${item.label} (${item.count})`,
          })),
          { value: 'directories', label: t('skills.locations') },
          ...collections.slice(4).map((item) => ({
            value: item.key,
            label: `${item.label} (${item.count})`,
          })),
        ]}
        ariaLabel={t('skills.collections')}
        onValueChange={(next) => {
          if (next === 'directories') void openDirectories();
          else changeScope(next);
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
          bind:this={directoryEditor}
          {settings}
          onCommit={(nextSettings) => {
            onSettingsCommit(nextSettings);
            void loadInventory();
          }}
          {onToast}
          onError={(message) => (directoryError = message)}
        />
        <div class="skills-create-secondary">
          <Button
            variant="tertiary"
            disabled={actions.busy}
            onClick={actions.openCreateModal}>{t('skills.createCustom')}</Button
          >
        </div>
      {/if}
    </div>
    {#if scope !== 'directories'}
      <div class="skills-toolbar" class:skills-mobile-hidden={selected}>
        <Button
          variant="primary"
          icon
          ariaLabel={t('skills.addSkills')}
          tooltip={t('skills.addSkills')}
          onClick={() => openDirectories(true)}
        >
          <svg
            width="18"
            height="18"
            viewBox="0 0 18 18"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
            aria-hidden="true"><path d="M9 3v12M3 9h12" /></svg
          >
        </Button>
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
                <div
                  class="skills-row"
                  class:skills-row--selected={selectedId === entry.id}
                  class:skills-row--disabled={entry.disabled}
                >
                  <button
                    type="button"
                    class="skills-row-open"
                    data-skill-id={entry.id}
                    aria-pressed={selectedId === entry.id}
                    aria-label={entry.name}
                    use:tooltip={entry.description || t('skills.noDescription')}
                    onclick={() => openSkill(entry)}
                  >
                    <span class="skills-row-copy">
                      <span class="skills-row-title">
                        <span class="skills-row-name">{entry.name}</span>
                        {#if entry.status !== 'available'}<StatusChip
                            variant={skillStatusVariant(entry)}
                            >{skillStatusLabel(entry, t)}</StatusChip
                          >
                        {:else if skillDiagnosticLines(entry).length}<Badge
                            variant="warn">{t('skills.notes')}</Badge
                          >{/if}
                      </span>
                      <span class="skills-row-source">
                        {skillSourceLabel(entry, t, agents)}{#if entry.shared}
                          <span class="skills-source-divider" aria-hidden="true"
                            >·</span
                          >{t('skills.sharedBadge')}{/if}
                      </span>
                    </span>
                  </button>
                  {@render skillActions(entry)}
                </div>
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
            <header class="skills-detail-header">
              <h3
                id="skill-detail-name"
                use:tooltip={selected.description || t('skills.noDescription')}
              >
                {selected.name}
              </h3>
              <p class="skills-detail-source">
                {skillSourceLabel(selected, t, agents)}
              </p>
              {@render skillActions(selected)}
            </header>
            <div class="skills-detail-scroll">
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
                <div class="skills-content-actions">
                  {#if selected.editable_scope}<Button
                      variant="tertiary"
                      disabled={actions.busy ||
                        inspectLoading ||
                        inspected?.id !== selected.id}
                      onClick={() => actions.startEdit(selected)}
                      >{t('skills.editInstructions')}</Button
                    >{:else}<Badge>{t('skills.readOnly')}</Badge>{/if}
                  {#if inspected}<CopyButton
                      text={inspected.content}
                      label={t('skills.copyContent')}
                    />{/if}
                </div>
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
            </div>
          </section>
        {/if}
      </div>
    {/if}
  </div>
</section>

{#if actions.showCreateModal}
  <Modal
    title={t('settings.skills.newSkill', 'New skill')}
    class="skills-editor-modal"
    labelledById="skill-create-modal-title"
    closeDisabled={actions.busy}
    onClose={actions.closeCreateModal}
  >
    {#snippet body()}
      <div class="skills-modal-body">
        <div class="skills-field">
          <label class="skills-field-label" for="create-scope">
            {t('skills.createScopeLabel', 'Create in')}
          </label>
          <Dropdown
            id="create-scope"
            value={actions.createScope}
            options={actions.scopeOptions}
            ariaLabel={t('skills.createScopeLabel')}
            onValueChange={(value) => (actions.createScope = value)}
          />
          <p class="skills-secondary">
            {t(
              actions.createScope === 'global'
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
            value={actions.newName}
            onInput={(next) => (actions.newName = next)}
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
            value={actions.newDescription}
            onInput={(value) => (actions.newDescription = value)}
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
            value={actions.newContent}
            onInput={(value) => (actions.newContent = value)}
            placeholder={t('skills.instructionsPlaceholder')}
          />
        </div>
      </div>
    {/snippet}
    {#snippet footer()}
      <Button
        variant="secondary"
        disabled={actions.busy}
        onClick={actions.closeCreateModal}
      >
        {t('common.cancel', 'Cancel')}
      </Button>
      <Button
        variant="primary"
        disabled={actions.createDisabled}
        onClick={actions.createSkill}
      >
        {t('settings.skills.create', 'Create skill')}
      </Button>
    {/snippet}
  </Modal>
{/if}

{#if actions.editing}
  <Modal
    title={t('skills.editTitle', 'Edit {name}', { name: actions.editing.name })}
    class="skills-editor-modal"
    labelledById="skill-edit-modal-title"
    closeDisabled={actions.busy}
    onClose={actions.closeEditModal}
  >
    {#snippet body()}
      <div class="skills-modal-body">
        {#if actions.editing.shared}<Banner variant="info"
            >{t('skills.editSharedHelp')}</Banner
          >{/if}
        <div class="skills-field">
          <label
            class="skills-field-label"
            for={`skill-content-${actions.editing.name}`}
          >
            {t('settings.skills.contentLabel', 'SKILL.md content')}
          </label>
          <TextArea
            id={`skill-content-${actions.editing.name}`}
            code
            rows="16"
            value={actions.editContent}
            onInput={(value) => (actions.editContent = value)}
          />
        </div>
      </div>
    {/snippet}
    {#snippet footer()}
      <Button
        variant="secondary"
        disabled={actions.busy}
        onClick={actions.closeEditModal}
      >
        {t('common.cancel', 'Cancel')}
      </Button>
      <Button
        variant="primary"
        disabled={actions.busy}
        onClick={actions.saveEdit}
      >
        {actions.busy
          ? t('common.saving', 'Saving…')
          : t('common.save', 'Save')}
      </Button>
    {/snippet}
  </Modal>
{/if}

{#if actions.shareTarget}
  <Modal
    title={t('skills.shareTitle', 'Share {name}', {
      name: actions.shareTarget.name,
    })}
    labelledById="skill-share-modal-title"
    closeDisabled={actions.busy}
    onClose={actions.closeShareModal}
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
        {#if actions.shareableAgents.length === 0}
          <EmptyState
            density="compact"
            description={t(
              'skills.noOtherAgents',
              'No other identity agents exist to share with.',
            )}
          />
        {:else}
          <div class="skills-share-list">
            {#each actions.shareableAgents as agent (agent.id)}
              <button
                type="button"
                class="skills-share-option"
                class:skills-share-option--selected={actions.shareReceivers.includes(
                  agent.id,
                )}
                role="switch"
                aria-checked={actions.shareReceivers.includes(agent.id)}
                aria-label={t('skills.toggleReceiver', 'Share with {name}', {
                  name: agent.name || agent.id,
                })}
                onclick={() => actions.toggleReceiver(agent.id)}
              >
                <span class="skills-receiver-check" aria-hidden="true"
                  >{actions.shareReceivers.includes(agent.id) ? '✓' : ''}</span
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
      <Button
        variant="secondary"
        disabled={actions.busy}
        onClick={actions.closeShareModal}
      >
        {t('common.cancel', 'Cancel')}
      </Button>
      <Button
        variant="primary"
        disabled={actions.shareSaveDisabled}
        onClick={actions.saveShare}
      >
        {t('skills.saveShare', 'Save')}
      </Button>
    {/snippet}
  </Modal>
{/if}

{#if actions.deleteTarget}
  <ConfirmDialog
    title={t('settings.skills.deleteConfirmTitle', 'Delete skill')}
    body={t(
      'skills.deletePackageConfirm',
      'Delete skill "{name}" permanently? The skill file is removed from disk.',
      { name: actions.deleteTarget.name },
    )}
    confirmLabel={t('common.delete', 'Delete')}
    onConfirm={actions.confirmDelete}
    onCancel={actions.cancelDelete}
  />
{/if}
