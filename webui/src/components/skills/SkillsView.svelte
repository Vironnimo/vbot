<script>
  // The dedicated Skills management view: one grouped control plane over every
  // Skill source — origin-grouped inventory, per-Skill status/diagnostics, the
  // policy disable switch, sharing of private Agent Skills, and the existing
  // global/private create-edit-delete UX.
  import { onMount } from 'svelte';

  import SkillDirectoryEditor from './SkillDirectoryEditor.svelte';
  import {
    agentDisplayName,
    filterSkills,
    groupInventoryByAgent,
    groupInventorySkills,
    skillDiagnosticLines,
    skillStatusLabel,
    skillStatusVariant,
    skillSupportsEditAndDelete,
  } from './skillsView.js';
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
    readSkills,
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
  let viewMode = $state('source');
  let searchQuery = $state('');

  // Create-modal state: a target scope (global pool or an agent's private home)
  // plus the name/content draft.
  let showCreateModal = $state(false);
  let createScope = $state(GLOBAL_SCOPE);
  let newName = $state('');
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

  // The entry whose diagnostics disclosure is expanded (unique per scope+name).
  let expandedKey = $state(null);

  // Collapsible scan-directories section.
  let showDirectories = $state(false);

  let filtered = $derived(filterSkills(inventory, searchQuery));
  let groups = $derived(
    viewMode === 'agent'
      ? groupInventoryByAgent(filtered, t, agents)
      : groupInventorySkills(filtered, t, agents),
  );
  let createDisabled = $derived(busy || !newName.trim() || !newContent.trim());
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
    try {
      const result = await listAgents();
      agents = Array.isArray(result?.agents) ? result.agents : [];
    } catch {
      // Non-fatal: without the agent list the create selector just offers Global.
      agents = [];
    }
  }

  async function loadInventory() {
    loading = true;
    loadError = '';
    try {
      const result = await skillInventory();
      inventory = Array.isArray(result?.skills) ? result.skills : [];
      staleShared = Array.isArray(result?.stale_shared)
        ? result.stale_shared
        : [];
    } catch (error) {
      loadError = `${t('skills.loadError', 'Skills could not be loaded.')} ${error.message}`;
      inventory = [];
      staleShared = [];
    } finally {
      loading = false;
    }
  }

  function scopeForEntry(entry) {
    return entry.owner_id ? `agent:${entry.owner_id}` : GLOBAL_SCOPE;
  }

  function entryKey(entry, groupKey, entryIndex) {
    return `${groupKey}:${entry.origin ?? GLOBAL_SCOPE}:${scopeForEntry(entry)}:${entry.name}:${entryIndex}`;
  }

  function openCreateModal() {
    createScope = GLOBAL_SCOPE;
    newName = '';
    newContent = '';
    showCreateModal = true;
  }

  function closeCreateModal() {
    showCreateModal = false;
    newName = '';
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
        content: newContent,
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

  async function startEdit(entry) {
    const scope = scopeForEntry(entry);
    busy = true;
    try {
      const result = await readSkills(scope);
      const match = (result?.skills ?? []).find(
        (skill) => skill.name === entry.name,
      );
      editing = { scope, name: entry.name };
      editContent = match?.content ?? '';
    } catch (error) {
      onToast({
        title: `${t('skills.editLoadError', 'Skill content could not be loaded.')} ${error.message}`,
        variant: 'error',
      });
    } finally {
      busy = false;
    }
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
  let shareSaveDisabled = $derived(busy || shareReceivers.length === 0);

  async function saveShare() {
    if (busy || !shareTarget || shareReceivers.length === 0) {
      return;
    }
    busy = true;
    try {
      await shareSkill(
        shareTarget.owner_id,
        shareTarget.name,
        true,
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

  async function unshareSkill(entry) {
    if (busy || !entry.owner_id) {
      return;
    }
    busy = true;
    try {
      await shareSkill(entry.owner_id, entry.name, false, []);
      onToast({
        title: t('skills.unsharedToast', 'Sharing stopped.', {}),
        variant: 'success',
      });
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
      if (editing?.name === target.name) {
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
  <div class="skills-layout">
    <div class="skills-scroll view-frame">
      <header class="skills-header view-header">
        <div class="view-header__intro">
          <p class="skills-eyebrow view-header__eyebrow">
            {t('skills.eyebrow', 'Configure')}
          </p>
          <h2 id="skills-title" class="skills-title view-header__title">
            {t('skills.title', 'Skills')}
          </h2>
          <p class="skills-subtitle view-header__subtitle">
            {t(
              'skills.subtitle',
              'Every skill from every source — manage availability, sharing, and editing.',
            )}
          </p>
        </div>
        <div class="view-toolbar__actions">
          <Button
            variant="secondary"
            disabled={loading}
            onClick={() => loadInventory()}
          >
            {t('skills.refresh', 'Refresh')}
          </Button>
        </div>
      </header>

      {#if loadError}
        <Banner variant="error" role="alert">{loadError}</Banner>
      {:else if loading && inventory.length === 0}
        <Banner variant="neutral"
          >{t('skills.loading', 'Loading skills…')}</Banner
        >
      {:else}
        <div class="skills-toolbar">
          <div class="skills-search">
            <svg
              class="skills-search-icon"
              viewBox="0 0 16 16"
              aria-hidden="true"
            >
              <circle cx="7" cy="7" r="4.5" />
              <path d="m10.5 10.5 3 3" />
            </svg>
            <input
              type="search"
              class="skills-search-input"
              placeholder={t('skills.searchPlaceholder', 'Search skills…')}
              value={searchQuery}
              oninput={(e) => (searchQuery = e.currentTarget.value)}
              aria-label={t('skills.searchPlaceholder', 'Search skills…')}
            />
          </div>
          <div class="skills-toolbar-right">
            <div
              class="skills-view-toggle"
              role="group"
              aria-label={t('skills.viewModeLabel', 'Group by')}
            >
              <button
                type="button"
                class="skills-view-toggle-btn"
                class:skills-view-toggle-btn--active={viewMode === 'source'}
                aria-pressed={viewMode === 'source'}
                onclick={() => (viewMode = 'source')}
              >
                {t('skills.viewBySource', 'By source')}
              </button>
              <button
                type="button"
                class="skills-view-toggle-btn"
                class:skills-view-toggle-btn--active={viewMode === 'agent'}
                aria-pressed={viewMode === 'agent'}
                onclick={() => (viewMode = 'agent')}
              >
                {t('skills.viewByAgent', 'By agent')}
              </button>
            </div>
            <Button variant="primary" disabled={busy} onClick={openCreateModal}>
              <span aria-hidden="true">+</span>
              {t('settings.skills.newSkill', 'New skill')}
            </Button>
          </div>
        </div>

        {#if staleShared.length > 0}
          <Banner variant="warn">
            {t(
              'skills.staleShared',
              '{count} shared-skill entries point at a missing agent or skill and are ignored.',
              { count: staleShared.length },
            )}
          </Banner>
        {/if}

        {#if groups.length === 0}
          <EmptyState
            density="compact"
            description={t('skills.empty', 'No skills found.{suffix}', {
              suffix: searchQuery ? ' Try a different search.' : '',
            })}
          />
        {:else}
          {#each groups as group (group.key)}
            <section
              class="skills-group"
              data-testid={`skill-group-${group.key}`}
            >
              <h3 class="skills-group-title">
                <span>{group.label}</span>
                <span class="skills-group-count">{group.skills.length}</span>
              </h3>
              {#each group.skills as entry, entryIndex (entryKey(entry, group.key, entryIndex))}
                {@const diagnostics = skillDiagnosticLines(entry)}
                {@const currentEntryKey = entryKey(
                  entry,
                  group.key,
                  entryIndex,
                )}
                {@const isExpanded = expandedKey === currentEntryKey}
                <article
                  class="skills-card"
                  class:skills-card--disabled={entry.disabled}
                  class:skills-card--expanded={isExpanded}
                >
                  <div class="skills-card-main">
                    <div class="skills-card-info">
                      <div class="skills-card-header">
                        <span
                          class="skills-card-name tooltip-anchor"
                          use:tooltip={entry.description ||
                            t('skills.noDescription', 'No description')}
                          >{entry.name}</span
                        >
                        {#if entry.shared}
                          <Badge variant="info">
                            {t('skills.sharedBadge', 'Shared')}
                          </Badge>
                          {#if entry.shared_with?.length > 0}
                            <span class="skills-card-receivers">
                              {t('skills.sharedWith', 'with {names}', {
                                names: entry.shared_with
                                  .map((id) => agentDisplayName(id, agents))
                                  .join(', '),
                              })}
                            </span>
                          {/if}
                        {/if}
                        <StatusChip variant={skillStatusVariant(entry)}>
                          {skillStatusLabel(entry, t)}
                        </StatusChip>
                      </div>
                      {#if entry.owner_id && viewMode === 'source'}
                        <span class="skills-card-owner">
                          {agentDisplayName(entry.owner_id, agents)}
                        </span>
                      {/if}
                    </div>
                    <div class="skills-card-actions">
                      <Button
                        variant="secondary"
                        disabled={busy}
                        onClick={() => toggleDisabled(entry)}
                      >
                        {entry.disabled
                          ? t('skills.enable', 'Enable')
                          : t('skills.disable', 'Disable')}
                      </Button>
                      {#if entry.owner_id}
                        {#if entry.shared}
                          <Button
                            variant="secondary"
                            disabled={busy}
                            onClick={() => openShareModal(entry)}
                          >
                            {t('skills.manageShare', 'Manage')}
                          </Button>
                          <Button
                            variant="secondary"
                            disabled={busy}
                            onClick={() => unshareSkill(entry)}
                          >
                            {t('skills.unshare', 'Unshare')}
                          </Button>
                        {:else}
                          <Button
                            variant="secondary"
                            disabled={busy}
                            onClick={() => openShareModal(entry)}
                          >
                            {t('skills.share', 'Share')}
                          </Button>
                        {/if}
                      {/if}
                      {#if skillSupportsEditAndDelete(entry)}
                        <Button
                          variant="tertiary"
                          icon
                          ariaLabel={t('common.edit', 'Edit')}
                          tooltip={t('common.edit', 'Edit')}
                          disabled={busy}
                          onClick={() => startEdit(entry)}
                        >
                          <svg
                            viewBox="0 0 16 16"
                            width="14"
                            height="14"
                            aria-hidden="true"
                            fill="none"
                            stroke="currentColor"
                            stroke-linecap="round"
                            stroke-linejoin="round"
                            stroke-width="1.35"
                          >
                            <path d="m10.9 2.2 2.9 2.9" />
                            <path
                              d="m2.6 13.4.7-3.2L10.9 2.6a1.4 1.4 0 0 1 2 0l.5.5a1.4 1.4 0 0 1 0 2l-7.6 7.6-3.2.7Z"
                            />
                            <path d="m9.8 3.7 2.5 2.5" />
                          </svg>
                        </Button>
                        <Button
                          variant="danger"
                          disabled={busy}
                          onClick={() => requestDelete(entry)}
                        >
                          {t('common.delete', 'Delete')}
                        </Button>
                      {/if}
                      {#if diagnostics.length > 0}
                        <button
                          type="button"
                          class="skills-card-details-btn"
                          aria-expanded={isExpanded}
                          onclick={() =>
                            (expandedKey = isExpanded ? null : currentEntryKey)}
                        >
                          {isExpanded
                            ? t('skills.hideDetails', 'Hide details')
                            : t('skills.showDetails', 'Details')}
                        </button>
                      {/if}
                    </div>
                  </div>
                  {#if isExpanded && diagnostics.length > 0}
                    <ul class="skills-diagnostics">
                      {#each diagnostics as line (line)}
                        <li>{line}</li>
                      {/each}
                    </ul>
                  {/if}
                </article>
              {/each}
            </section>
          {/each}
        {/if}

        <details class="skills-directories-section" bind:open={showDirectories}>
          <summary>
            <span class="skills-eyebrow">
              {t('skills.directoriesTitle', 'Scan directories')}
            </span>
          </summary>
          {#if showDirectories}
            <SkillDirectoryEditor
              {settings}
              onCommit={onSettingsCommit}
              {onToast}
              onError={(message) => (loadError = message)}
            />
          {/if}
        </details>
      {/if}
    </div>
  </div>
</section>

{#if showCreateModal}
  <Modal
    title={t('settings.skills.newSkill', 'New skill')}
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
          <select
            id="create-scope"
            class="skills-select"
            value={createScope}
            onchange={(e) => (createScope = e.currentTarget.value)}
          >
            {#each scopeOptions as option (option.value)}
              <option value={option.value}>{option.label}</option>
            {/each}
          </select>
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
          <label class="skills-field-label" for="new-skill-content">
            {t('settings.skills.contentLabel', 'SKILL.md content')}
            <InfoHint
              text={t(
                'settings.skills.newSkillHelp',
                'A skill is a Markdown playbook: a header with a name and a short description, followed by the instructions.\n\nThe description matters most — it is what the agent reads to decide when to apply the skill, so state clearly what task it is for.',
              )}
            />
          </label>
          <TextArea
            id="new-skill-content"
            code
            rows="12"
            value={newContent}
            onInput={(value) => (newContent = value)}
            placeholder={t(
              'settings.skills.contentPlaceholder',
              '---\nname: skill-name\ndescription: When to use this skill.\n---\n\n# Overview',
            )}
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
    labelledById="skill-edit-modal-title"
    closeDisabled={busy}
    onClose={closeEditModal}
  >
    {#snippet body()}
      <div class="skills-modal-body">
        <div class="skills-edit-name">
          {editing.name}
        </div>
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
        <p class="skills-share-desc">
          {t(
            'skills.shareDescription',
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
      'settings.skills.deleteConfirm',
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
    flex-direction: column;
    min-height: 0;
    height: 100%;
    overflow-x: hidden;
    overflow-y: auto;
    overscroll-behavior: contain;
    scrollbar-gutter: stable;
  }

  .skills-layout {
    display: flex;
    width: 100%;
    min-height: 100%;
    height: auto;
    justify-content: center;
  }

  .skills-scroll {
    width: 100%;
    max-width: var(--content-max-narrow);
    overflow: visible;
    padding: var(--space-lg);
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
  }

  .skills-header {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: var(--space-sm);
  }

  .skills-eyebrow,
  .skills-group-title {
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    letter-spacing: 0.07em;
    text-transform: uppercase;
    color: var(--text-lo);
  }

  .skills-title {
    margin: 0;
    font-size: var(--fs-heading-lg);
    font-weight: 600;
    letter-spacing: -0.02em;
    color: var(--text-hi);
  }

  .skills-subtitle {
    margin: 0;
    font-size: var(--fs-body-sm);
    color: var(--text-med);
  }

  /* Toolbar: search + view toggle + new skill button */
  .skills-toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-sm);
    flex-wrap: wrap;
  }

  .skills-search {
    position: relative;
    flex: 1 1 220px;
    max-width: 360px;
  }

  .skills-search-icon {
    position: absolute;
    left: var(--space-sm);
    top: 50%;
    transform: translateY(-50%);
    width: 14px;
    height: 14px;
    color: var(--text-lo);
    pointer-events: none;
  }

  .skills-search-input {
    width: 100%;
    padding: var(--space-sm) var(--space-sm) var(--space-sm) 28px;
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    color: var(--text-hi);
    font-size: var(--fs-body-sm);
    font-family: var(--font-mono);
  }

  .skills-search-input::placeholder {
    color: var(--text-lo);
  }

  .skills-search-input:focus-visible {
    outline: none;
    border-color: var(--accent);
    box-shadow: var(--focus-ring);
  }

  .skills-toolbar-right {
    display: flex;
    align-items: center;
    gap: var(--space-sm);
  }

  .skills-view-toggle {
    display: flex;
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    overflow: hidden;
  }

  .skills-view-toggle-btn {
    padding: var(--space-xs) var(--space-sm);
    border: none;
    background: none;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
    cursor: pointer;
    transition:
      background 0.15s,
      color 0.15s;
  }

  .skills-view-toggle-btn:hover {
    color: var(--text-hi);
  }

  .skills-view-toggle-btn--active {
    background: var(--surface-3);
    color: var(--text-hi);
  }

  /* Group headers */
  .skills-group {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
  }

  .skills-group-title {
    margin: var(--space-sm) 0 0;
    display: flex;
    align-items: center;
    gap: var(--space-sm);
  }

  .skills-group-count {
    font-size: var(--fs-mono-xs);
    color: var(--text-lo);
    background: var(--surface-2);
    border-radius: 99px;
    padding: 1px 7px;
  }

  /* Skill cards */
  .skills-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    padding: var(--space-sm) var(--space-md);
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
    transition: border-color 0.15s;
  }

  .skills-card:hover {
    border-color: var(--border-2);
  }

  .skills-card--disabled {
    opacity: 0.55;
  }

  .skills-card--expanded {
    border-color: var(--border-2);
  }

  .skills-card-main {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-md);
    flex-wrap: wrap;
  }

  .skills-card-info {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
    flex: 1 1 200px;
  }

  .skills-card-header {
    display: flex;
    align-items: center;
    gap: var(--space-sm);
    flex-wrap: wrap;
  }

  .skills-card-name {
    font-family: var(--font-mono);
    font-size: var(--fs-mono-body);
    color: var(--text-hi);
    font-weight: 500;
    cursor: help;
  }

  .skills-card-owner {
    font-size: var(--fs-mono-xs);
    color: var(--text-lo);
  }

  .skills-card-actions {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
    flex-wrap: wrap;
    flex-shrink: 0;
  }

  .skills-card-details-btn {
    padding: var(--space-xs) var(--space-sm);
    border: none;
    background: none;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
    cursor: pointer;
    text-decoration: underline;
    text-underline-offset: 2px;
  }

  .skills-card-details-btn:hover {
    color: var(--text-hi);
  }

  .skills-diagnostics {
    margin: 0;
    padding: var(--space-sm) var(--space-md);
    border-left: 2px solid var(--border-2);
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
    color: var(--text-med);
  }

  /* Scan directories collapsible */
  .skills-directories-section {
    margin-top: var(--space-lg);
    border-top: 1px solid var(--border);
    padding-top: var(--space-md);
  }

  .skills-directories-section > summary {
    cursor: pointer;
    list-style: none;
    padding: var(--space-xs) 0;
  }

  .skills-directories-section > summary::-webkit-details-marker {
    display: none;
  }

  .skills-directories-section > summary::before {
    content: '▸';
    display: inline-block;
    margin-right: var(--space-sm);
    color: var(--text-lo);
    transition: transform 0.15s;
  }

  .skills-directories-section[open] > summary::before {
    transform: rotate(90deg);
  }

  /* Modals */
  .skills-modal-body {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
  }

  .skills-edit-name {
    font-family: var(--font-mono);
    font-size: var(--fs-mono-body);
    color: var(--text-hi);
    font-weight: 500;
  }

  .skills-field {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
  }

  .skills-field-label {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
    font-size: var(--fs-body-sm);
    color: var(--text-med);
  }

  .skills-select {
    padding: var(--space-sm);
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    color: var(--text-hi);
    font-size: var(--fs-body-sm);
    font-family: var(--font-mono);
  }

  .skills-select:focus-visible {
    outline: none;
    border-color: var(--accent);
    box-shadow: var(--focus-ring);
  }

  .skills-card-receivers {
    font-size: var(--fs-mono-xs);
    color: var(--text-lo);
  }

  .skills-share-desc {
    margin: 0 0 var(--space-md);
    font-size: var(--fs-body-sm);
    color: var(--text-med);
  }

  .skills-share-list {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
    max-height: 320px;
    overflow-y: auto;
  }

  .skills-share-option {
    display: flex;
    align-items: center;
    gap: var(--space-sm);
    padding: var(--space-sm);
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    color: var(--text-med);
    cursor: pointer;
    transition:
      border-color 0.15s,
      background 0.15s,
      color 0.15s;
  }

  .skills-share-option:hover {
    border-color: var(--border-2);
    color: var(--text-hi);
  }

  .skills-share-option--selected {
    background: var(--accent-10);
    border-color: var(--accent-30);
    color: var(--accent);
  }

  .skills-share-option--selected:hover {
    background: var(--accent-16);
    color: var(--accent);
  }

  .skills-share-agent-name {
    font-size: var(--fs-body-sm);
    color: var(--text-hi);
  }

  .skills-share-agent-id {
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    color: var(--text-lo);
  }

  @media (max-width: 640px) {
    .skills-scroll {
      padding: var(--space-md);
    }

    .skills-card-main {
      flex-direction: column;
    }

    .skills-card-actions {
      width: 100%;
    }
  }
</style>
